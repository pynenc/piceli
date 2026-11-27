"""A storage probe that runs before a client chart installs (0.94.0).

A claim in a storage class the cluster's nodes cannot attach (a disk type a
machine type does not support, a class bound to another zone, an exhausted
quota) leaves the app Pending with no message. Helm's ``lookup`` is empty
under ``helm template`` (Argo CD), so the check runs in the cluster, as Helm
hooks ``pre-install``:

- a ServiceAccount, a Role (create, get and delete claims and pods, list
  events), its RoleBinding, a ClusterRole that only reads storage classes and
  its ClusterRoleBinding (named after the release namespace and name), a
  NetworkPolicy that lets the probe reach the API server, and
- a Job running :data:`SCRIPT` (POSIX ``sh`` and ``curl``, the way
  ``app.cluster_identity`` reads the cluster): it creates a claim of
  ``size`` in ``storageClass`` and a pod that mounts it, waits until the pod
  runs, and deletes both. When the pod does not start in time it prints one
  line with the newest warning event and the other storage classes, and
  fails the install.

Every object, and the pod the script creates, passes the bundle's safety
gate. Chart only: a kustomize bundle has no hook to run it before the app.

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from piceli.bundle.build import PART_OF, BundleError

#: The component label of the probe Job's pod and of the pod it creates.
COMPONENT = "app.kubernetes.io/component"
JOB_COMPONENT = "storage-probe"
VOLUME_COMPONENT = "storage-probe-volume"
#: The user the probe runs as when the app's claim workload names none.
NOBODY = 65534
#: Placeholders the script replaces in :func:`pod_json` and :func:`claim_json`.
NAME_TOKEN = "@NAME@"
IMAGE_TOKEN = "@IMAGE@"
CLASS_TOKEN = "@CLASS@"
PULL_TOKEN = "@PULL@"
#: Hook order: the RBAC and the policy before the Job.
WEIGHTS = {
    "ServiceAccount": "-30",
    "Role": "-25",
    "ClusterRole": "-25",
    "RoleBinding": "-20",
    "ClusterRoleBinding": "-20",
    "NetworkPolicy": "-15",
    "Job": "-10",
}
HOOK_ANNOTATIONS = {
    "helm.sh/hook": "pre-install",
    "helm.sh/hook-delete-policy": "before-hook-creation,hook-succeeded",
}
_DIGEST = re.compile(r"@sha256:[a-f0-9]{64}$")
_SIZE = re.compile(r"^[0-9]+(Ki|Mi|Gi|Ti|Pi|k|M|G|T|P)?$")
_SMALL = {
    "requests": {"cpu": "10m", "memory": "16Mi"},
    "limits": {"cpu": "100m", "memory": "64Mi"},
}
_CONTAINER_SECURITY = {
    "allowPrivilegeEscalation": False,
    "capabilities": {"drop": ["ALL"]},
    "readOnlyRootFilesystem": True,
    "runAsNonRoot": True,
    "seccompProfile": {"type": "RuntimeDefault"},
}

#: The Job's script. Environment: ``PROBE_NAME`` (the claim and pod),
#: ``PROBE_IMAGE``, ``PROBE_CLASS`` (empty: the default class),
#: ``PROBE_TIMEOUT`` and ``PROBE_POLL`` (seconds), ``PROBE_CLAIM`` and
#: ``PROBE_POD`` (JSON with the placeholders above), ``PROBE_PULL_SECRETS``
#: (a JSON list); ``SERVICE_ACCOUNT_DIR`` for tests.
SCRIPT = r"""set -u
sa="${SERVICE_ACCOUNT_DIR:-/var/run/secrets/kubernetes.io/serviceaccount}"
host="${KUBERNETES_SERVICE_HOST:?not in a pod}"
case "$host" in *:*) host="[$host]" ;; esac
base="https://$host:${KUBERNETES_SERVICE_PORT:-443}"
token="$(cat "$sa/token")"
ns="$(cat "$sa/namespace")"
name="${PROBE_NAME:?}"
class="${PROBE_CLASS:-}"
timeout="${PROBE_TIMEOUT:-180}"
poll="${PROBE_POLL:-2}"
pods="/api/v1/namespaces/$ns/pods"
claims="/api/v1/namespaces/$ns/persistentvolumeclaims"
nl='
'
default=""

# api METHOD PATH [BODY]: sets $code (HTTP status; 000: unreachable) and $body (one line)
api() {
  if [ $# -ge 3 ]; then
    out="$(curl -sS --max-time 20 --cacert "$sa/ca.crt" -H "Authorization: Bearer $token" \
      -H "Content-Type: application/json" -X "$1" --data "$3" -w '\n%{http_code}' "$base$2")"
  else
    out="$(curl -sS --max-time 20 --cacert "$sa/ca.crt" -H "Authorization: Bearer $token" \
      -X "$1" -w '\n%{http_code}' "$base$2")"
  fi
  code="${out##*"$nl"}"
  body="$(printf '%s' "${out%"$nl"*}" | tr -d '\n')"
}

# value KEY JSON: the last string value of KEY in JSON (one line), unescaped
value() {
  printf '%s\n' "$2" | sed -nE 's/.*"'"$1"'": *"(([^"\\]|\\.)*)".*/\1/p' |
    sed 's/\\n/ /g; s/\\"/"/g; s/\\\\/\\/g'
}

# items JSON: one line per object of a JSON list, each from its "metadata"
items() {
  printf '%s\n' "$1" | sed 's/"metadata": */\
"metadata":/g'
}

# stamp EVENT: its newest time as YYYYMMDDHHMMSS (0: none)
stamp() {
  for key in lastTimestamp lastObservedTime eventTime firstTimestamp; do
    at="$(value "$key" "$1" | sed -n 's/^\([0-9]*\)-\([0-9]*\)-\([0-9]*\)T\([0-9]*\):\([0-9]*\):\([0-9]*\).*/\1\2\3\4\5\6/p')"
    if [ -n "$at" ]; then echo "$at"; return; fi
  done
  echo 0
}

# newest EVENTS: "REASON: MESSAGE" of the newest Warning event
newest() {
  best="" best_at=-1
  while IFS= read -r item; do
    [ "$(value type "$item")" = Warning ] || continue
    at="$(stamp "$item")"
    [ "$at" -ge "$best_at" ] || continue
    best_at="$at"
    best="$(value reason "$item"): $(value message "$item")"
  done <<EOF
$(items "$1")
EOF
  printf '%s' "$best"
}

# classes: $names, every storage class; $default, the one marked default
classes() {
  names=""
  api GET /apis/storage.k8s.io/v1/storageclasses
  case "$code" in 2??) ;; *) return 0 ;; esac
  while IFS= read -r item; do
    n="$(printf '%s\n' "$item" | sed -n 's/^"metadata": *{"name": *"\([^"]*\)".*/\1/p')"
    [ -n "$n" ] || continue
    names="$names $n"
    case "$item" in
      *'is-default-class":"true"'*|*'is-default-class": "true"'*) default="${default:-$n}" ;;
    esac
  done <<EOF
$(items "$body")
EOF
}

what() {
  if [ -n "$class" ]; then printf 'storage class "%s"' "$class"
  elif [ -n "$default" ]; then printf 'the default storage class "%s"' "$default"
  else printf 'the default storage class'
  fi
}

elapsed() { echo $(( $(date +%s) - start )); }

cleanup() {
  api DELETE "$pods/$name?gracePeriodSeconds=0"
  api DELETE "$claims/$name"
}

# create PATH JSON WHAT: create an object, waiting out one an earlier run left
create() {
  while :; do
    api POST "$1" "$2"
    case "$code" in
      2??) return 0 ;;
      409) ;;
      000|"") printf '%s\n' "storage probe: cannot reach the API server at $base"; exit 1 ;;
      *) printf '%s\n' "storage probe: the cluster refused the $3 (HTTP $code: $(value message "$body"))"; exit 1 ;;
    esac
    if [ "$(elapsed)" -ge "$timeout" ]; then
      printf '%s\n' "storage probe: the $3 $name of an earlier run is still being deleted"
      exit 1
    fi
    sleep "$poll"
  done
}

start="$(date +%s)"
cleanup 2>/dev/null
trap 'cleanup 2>/dev/null' EXIT
trap 'exit 1' HUP INT TERM

if [ -n "$class" ]; then
  claim="$(printf '%s' "$PROBE_CLAIM" | sed "s|@NAME@|$name|g; s|@CLASS@|$class|g")"
else
  claim="$(printf '%s' "$PROBE_CLAIM" | sed "s|@NAME@|$name|g; s|,\"storageClassName\":\"@CLASS@\"||")"
fi
pod="$(printf '%s' "$PROBE_POD" | sed "s|@NAME@|$name|g; s|@IMAGE@|$PROBE_IMAGE|g; s|\"@PULL@\"|${PROBE_PULL_SECRETS:-[]}|")"

create "$claims" "$claim" claim
[ -n "$class" ] || default="$(value storageClassName "$body")"
create "$pods" "$pod" pod
phase=Pending
while :; do
  api GET "$pods/$name"
  phase="$(value phase "$body")"
  waiting="$(printf '%s\n' "$body" | sed -n 's/.*"waiting": *{"reason": *"\([A-Za-z]*\)".*/\1/p')"
  case "$phase" in
    Running|Succeeded)
      printf '%s\n' "storage probe: $(what) attached on these nodes in $(elapsed)s"
      exit 0 ;;
    Failed) break ;;
  esac
  case "$waiting" in
    ""|ContainerCreating|PodInitializing) ;;
    *) printf '%s\n' "storage probe: $(what) attached on these nodes in $(elapsed)s (the probe container did not start: $waiting)"
       exit 0 ;;
  esac
  [ "$(elapsed)" -lt "$timeout" ] || break
  sleep "$poll"
done

api GET "/api/v1/namespaces/$ns/events?fieldSelector=involvedObject.name%3D$name"
cause="$(newest "$body")"
[ -n "$cause" ] || cause="no warning event: the probe pod stayed ${phase:-Pending} for ${timeout}s"
classes
chosen="${class:-$default}"
others=""
for n in $names; do
  [ "$n" = "$chosen" ] || others="${others:+$others, }$n"
done
if [ -n "$others" ]; then
  hint="set storageClass to one of: $others"
else
  hint="ask the cluster's administrator for a class these nodes can attach"
fi
printf '%s\n' "$(what) cannot attach on these nodes ($cause): $hint"
exit 1
"""


@dataclass(frozen=True)
class StorageProbe:
    """Check before install that the chart's storage class attaches (0.94.0).

    ``HelmChart(pipeline, storage_probe=StorageProbe(image=...))`` adds Helm
    ``pre-install`` hooks: a Job that creates a claim of ``size`` in the
    chart's ``storageClass`` and a pod mounting it, waits up to
    ``timeout_seconds`` for the pod to run, deletes both, and otherwise
    fails the install with one line naming the newest warning event and the
    storage classes to try. The chart values ``storageProbe.enabled`` and
    ``storageProbe.timeoutSeconds`` turn it off and change the wait. Only for
    an app with at least one claim.

    :param image: An image with ``sh`` and ``curl``, pinned by digest like
        every image of a chart (usually the app's own).
    :param timeout_seconds: The default wait for the pod, 10 to 3600.
    :param size: The probe claim's size (a whole quantity, ``1Gi``).

    Example::

        chart = HelmChart(pipeline, storage_probe=StorageProbe(image=WEB_IMAGE))
    """

    image: str
    timeout_seconds: int = 180
    size: str = "1Gi"

    def checked(self) -> StorageProbe:
        """``self``, or :class:`~piceli.bundle.build.BundleError` ``bundle-invalid``."""
        if not isinstance(self.image, str) or not _DIGEST.search(self.image):
            raise BundleError(
                "bundle-invalid",
                "storage_probe image must be pinned by digest in a registry "
                "(repository@sha256:...), like every image of a chart",
            )
        timeout = self.timeout_seconds
        if (
            not isinstance(timeout, int)
            or isinstance(timeout, bool)
            or not (10 <= timeout <= 3600)
        ):
            raise BundleError(
                "bundle-invalid", "storage_probe timeout_seconds: 10 to 3600"
            )
        if not isinstance(self.size, str) or not _SIZE.fullmatch(self.size):
            raise BundleError(
                "bundle-invalid",
                f"storage_probe size {self.size!r}: a quantity like 1Gi",
            )
        return self


# ------------------------------------------------------------ the app's claims


def _pod(manifest: Mapping[str, Any]) -> Mapping[str, Any] | None:
    from piceli.pipeline.compose import pod_specs

    found = pod_specs(manifest)
    return found[0] if found else None


def claim_workload(objects: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The pod spec of the first workload that mounts a claim, if any."""
    for manifest in objects:
        pod = _pod(manifest)
        if pod is None:
            continue
        templates = (manifest.get("spec") or {}).get("volumeClaimTemplates")
        claims = any(
            isinstance(volume, Mapping) and "persistentVolumeClaim" in volume
            for volume in pod.get("volumes") or ()
        )
        if templates or claims:
            return pod
    return None


def first_claim(objects: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The spec of the first claim (an object or a StatefulSet template)."""
    for manifest in objects:
        if manifest.get("kind") == "PersistentVolumeClaim":
            return manifest.get("spec") or {}
        for template in (manifest.get("spec") or {}).get("volumeClaimTemplates") or ():
            if isinstance(template, Mapping):
                return template.get("spec") or {}
    return None


def _user(pod: Mapping[str, Any] | None) -> tuple[int, int]:
    """``(user, group)`` of the claim workload, else ``nobody``."""
    if pod is None:
        return NOBODY, NOBODY
    contexts = [pod.get("securityContext")] + [
        container.get("securityContext")
        for container in pod.get("containers") or ()
        if isinstance(container, Mapping)
    ]
    for context in contexts:
        if isinstance(context, Mapping):
            user = context.get("runAsUser")
            if isinstance(user, int) and not isinstance(user, bool) and user > 0:
                group = context.get("runAsGroup")
                if isinstance(group, int) and not isinstance(group, bool) and group > 0:
                    return user, group
                return user, user
    return NOBODY, NOBODY


def scheduling(pod: Mapping[str, Any] | None) -> dict[str, Any]:
    """Where the claim workload runs: its node selector, tolerations and node
    affinity (never pod affinity: the app's pods do not exist yet)."""
    if pod is None:
        return {}
    result: dict[str, Any] = {}
    for key in ("nodeSelector", "tolerations"):
        if pod.get(key):
            result[key] = json.loads(json.dumps(pod[key]))
    node = (pod.get("affinity") or {}).get("nodeAffinity")
    if node:
        result["affinity"] = {"nodeAffinity": json.loads(json.dumps(node))}
    return result


# ------------------------------------------------------------------- objects


def _pod_security(user: int, group: int) -> dict[str, Any]:
    return {
        "runAsNonRoot": True,
        "runAsUser": user,
        "runAsGroup": group,
        "seccompProfile": {"type": "RuntimeDefault"},
    }


def _labels(bundle: str, component: str) -> dict[str, str]:
    return {PART_OF: bundle, COMPONENT: component}


def pod_manifest(
    bundle: str, annotation: Mapping[str, str], pod: Mapping[str, Any] | None
) -> dict[str, Any]:
    """The pod the script creates, with the placeholders it replaces."""
    user, group = _user(pod)
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": NAME_TOKEN,
            "labels": _labels(bundle, VOLUME_COMPONENT),
            "annotations": dict(annotation),
        },
        "spec": {
            "automountServiceAccountToken": False,
            "containers": [
                {
                    "command": ["/bin/sh", "-c", "test -d /probe"],
                    "image": IMAGE_TOKEN,
                    "name": "probe",
                    "resources": json.loads(json.dumps(_SMALL)),
                    "securityContext": json.loads(json.dumps(_CONTAINER_SECURITY)),
                    "volumeMounts": [{"mountPath": "/probe", "name": "probe"}],
                }
            ],
            "imagePullSecrets": PULL_TOKEN,
            "restartPolicy": "Never",
            "securityContext": _pod_security(user, group),
            "volumes": [
                {"name": "probe", "persistentVolumeClaim": {"claimName": NAME_TOKEN}}
            ],
            **scheduling(pod),
        },
    }


def claim_manifest(
    bundle: str, annotation: Mapping[str, str], size: str
) -> dict[str, Any]:
    """The claim the script creates (``storageClassName`` last in its JSON)."""
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": NAME_TOKEN,
            "labels": _labels(bundle, VOLUME_COMPONENT),
            "annotations": dict(annotation),
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": size}},
            "storageClassName": CLASS_TOKEN,
        },
    }


def as_json(manifest: Mapping[str, Any]) -> str:
    """Compact JSON, keys sorted (the claim's ``storageClassName`` comes last)."""
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class Names:
    """The values the hook objects hold: literals for the safety gate, Helm
    template sites for the chart."""

    name: str  # the claim, the pod, and the namespaced objects
    cluster: str  # the ClusterRole and its binding
    namespace: str  # the RoleBinding's subjects
    image: str
    timeout: Any  # the script's PROBE_TIMEOUT (a string)
    deadline: Any  # the Job's activeDeadlineSeconds (an integer)
    storage_class: str
    pull_secrets: str


def hook_objects(
    bundle: str,
    annotation: Mapping[str, str],
    probe: StorageProbe,
    objects: Sequence[Mapping[str, Any]],
    names: Names,
) -> list[dict[str, Any]]:
    """ServiceAccount, Role, ClusterRole, their bindings, the NetworkPolicy
    and the Job, in hook order."""
    pod = claim_workload(objects)
    user, group = _user(pod)

    def meta(kind: str, name: str) -> dict[str, Any]:
        return {
            "name": name,
            "labels": _labels(bundle, JOB_COMPONENT),
            "annotations": {
                **annotation,
                **HOOK_ANNOTATIONS,
                "helm.sh/hook-weight": WEIGHTS[kind],
            },
        }

    rbac = "rbac.authorization.k8s.io/v1"
    account = {
        "kind": "ServiceAccount",
        "name": names.name,
        "namespace": names.namespace,
    }
    job_meta = meta("Job", names.name)
    job_meta["annotations"]["helm.sh/hook-output-log-policy"] = "hook-failed"
    claim = claim_manifest(bundle, annotation, probe.size)
    probe_pod = pod_manifest(bundle, annotation, pod)
    return [
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": meta("ServiceAccount", names.name),
            "automountServiceAccountToken": True,
        },
        {
            "apiVersion": rbac,
            "kind": "Role",
            "metadata": meta("Role", names.name),
            "rules": [
                {
                    "apiGroups": [""],
                    "resources": ["persistentvolumeclaims", "pods"],
                    "verbs": ["create", "delete", "get"],
                },
                {"apiGroups": [""], "resources": ["events"], "verbs": ["list"]},
            ],
        },
        {
            "apiVersion": rbac,
            "kind": "ClusterRole",
            "metadata": meta("ClusterRole", names.cluster),
            "rules": [
                {
                    "apiGroups": ["storage.k8s.io"],
                    "resources": ["storageclasses"],
                    "verbs": ["get", "list"],
                }
            ],
        },
        {
            "apiVersion": rbac,
            "kind": "RoleBinding",
            "metadata": meta("RoleBinding", names.name),
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "Role",
                "name": names.name,
            },
            "subjects": [dict(account)],
        },
        {
            "apiVersion": rbac,
            "kind": "ClusterRoleBinding",
            "metadata": meta("ClusterRoleBinding", names.cluster),
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "ClusterRole",
                "name": names.cluster,
            },
            "subjects": [dict(account)],
        },
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": meta("NetworkPolicy", names.name),
            "spec": {
                "podSelector": {"matchLabels": _labels(bundle, JOB_COMPONENT)},
                "policyTypes": ["Egress"],
                "egress": [
                    {
                        "ports": [
                            {"port": 443, "protocol": "TCP"},
                            {"port": 6443, "protocol": "TCP"},
                        ]
                    }
                ],
            },
        },
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": job_meta,
            "spec": {
                "activeDeadlineSeconds": names.deadline,
                "backoffLimit": 0,
                "template": {
                    "metadata": {"labels": _labels(bundle, JOB_COMPONENT)},
                    "spec": {
                        "automountServiceAccountToken": True,
                        "containers": [
                            {
                                "command": ["/bin/sh", "-c", SCRIPT],
                                "env": [
                                    {"name": "PROBE_NAME", "value": names.name},
                                    {"name": "PROBE_IMAGE", "value": names.image},
                                    {
                                        "name": "PROBE_CLASS",
                                        "value": names.storage_class,
                                    },
                                    {"name": "PROBE_TIMEOUT", "value": names.timeout},
                                    {
                                        "name": "PROBE_PULL_SECRETS",
                                        "value": names.pull_secrets,
                                    },
                                    {"name": "PROBE_CLAIM", "value": as_json(claim)},
                                    {"name": "PROBE_POD", "value": as_json(probe_pod)},
                                ],
                                "image": probe.image,
                                "name": "probe",
                                "resources": json.loads(json.dumps(_SMALL)),
                                "securityContext": json.loads(
                                    json.dumps(_CONTAINER_SECURITY)
                                ),
                                "terminationMessagePolicy": "FallbackToLogsOnError",
                                "volumeMounts": [{"mountPath": "/tmp", "name": "tmp"}],
                            }
                        ],
                        "restartPolicy": "Never",
                        "securityContext": _pod_security(user, group),
                        "serviceAccountName": names.name,
                        "volumes": [
                            {
                                "name": "tmp",
                                "emptyDir": {"medium": "Memory", "sizeLimit": "1Mi"},
                            }
                        ],
                        **scheduling(pod),
                    },
                },
            },
        },
    ]


def literal_names(bundle: str, namespace: str, probe: StorageProbe) -> Names:
    """The names of a release named like the bundle, for the safety gate."""
    return Names(
        name=f"{bundle}-storage-probe",
        cluster=f"{namespace}:{bundle}:storage-probe",
        namespace=namespace,
        image=probe.image,
        timeout=str(probe.timeout_seconds),
        deadline=probe.timeout_seconds + 60,
        storage_class="",
        pull_secrets="[]",
    )


def gate(
    bundle: str,
    namespace: str,
    annotation: Mapping[str, str],
    probe: StorageProbe,
    objects: Sequence[Mapping[str, Any]],
) -> None:
    """Every hook object and the pod the script creates pass the safety gate.

    :raises BundleError: ``bundle-unsafe-<rule>`` with the violations.
    """
    from piceli.bundle.safety import check, first_code

    names = literal_names(bundle, namespace, probe)
    manifests = hook_objects(bundle, annotation, probe, objects, names)
    manifests.append(pod_manifest(bundle, annotation, claim_workload(objects)))
    violations = check(manifests)
    if violations:
        raise BundleError(
            first_code(violations),
            f"the storage probe breaks the safety gate: {violations[0].object} "
            f"breaks {violations[0].rule} ({violations[0].detail})",
            violations,
        )
