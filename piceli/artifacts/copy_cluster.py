"""Run ``piceli artifacts copy`` inside the cluster, as a Job (0.83.0).

A copy from the cluster registry used to run on the client machine through a
hand-made ``kubectl port-forward``; a dropped forward killed the copy. With
``--in-cluster`` the copy runs where the registry is:

1. The client writes the set and the ledger into ConfigMap
   ``piceli-copy-<ledger>`` and creates one ``batch/v1`` Job that runs
   ``piceli artifacts copy-job-run`` (the same image the controller builds
   with), with ``backoffLimit: 0`` and the deadline of ``--timeout``.
2. The Job reaches the cluster registry by its Service name
   (``piceli-registry.<namespace>.svc:5000``, plain HTTP in the cluster) and
   runs :func:`~piceli.artifacts.copy_set.run_copy_set`: the same retries,
   ledger and skipping as on a laptop. Registry credentials come from a
   Secret into environment variables, never into arguments or the plan.
3. The Job prints its ledger after every record and its report last, as one
   gzip and base64 line each; the client reads them from the pod log, saves
   the ledger and prints the report. The Job's pod needs no Kubernetes API
   access (``automountServiceAccountToken: false``).

A killed client leaves the Job running. The same command with the same ledger
finds the Job by its name (a hash of the set), waits for it and reads its
result; a Job that already ended is read the same way. Importing this module
runs nothing.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from piceli.artifacts.cluster_build import slug
from piceli.artifacts.copy_set import (
    LEDGER_REVISION,
    ConfigMapLedger,
    CopyItem,
    CopyOptions,
    CopyReport,
    CopySetError,
    ItemResult,
    LedgerStore,
    check_items,
    ledger_records,
    prepare_copies,
    resumed_receipt,
    run_copy_set,
    validate_name,
)
from piceli.artifacts.image_copy import CopyError, CopySource
from piceli.artifacts.registry import RegistryCredentials, RegistryTarget
from piceli.pipeline.prerollout import DEADLINE_SLACK_SECONDS, scrub
from piceli.pipeline.prerollout_cluster import (
    Outcome,
    PreRolloutCluster,
    Unreadable,
    _raw,
)

LEDGER_MARKER = "PICELI-COPY-LEDGER "
RESULT_MARKER = "PICELI-COPY-RESULT "
COPY_LABEL = "piceli.io/copy"
SET_KEY = "set.json"
SET_REVISION = "piceli.copy-job-set.v1"
SET_DIR = "/var/run/piceli-copy-set"
REGISTRY_SERVICE = "piceli-registry"
REGISTRY_PORT = 5000
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
#: Secret keys: ``source-username``, ``source-password``, ``source-token`` and the same for ``target``.
SECRET_FIELDS = ("username", "password", "token")
SECRET_ROLES = ("source", "target")
_NAMESPACE = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")


def registry_service(namespace: str) -> str:
    """``piceli-registry.<namespace>.svc:5000``: the cluster registry, from a pod."""
    if _NAMESPACE.fullmatch(namespace) is None:
        raise CopySetError("copy-invalid", "invalid namespace")
    return f"{REGISTRY_SERVICE}.{namespace}.svc:{REGISTRY_PORT}"


def _rewrite(reference: str, namespace: str) -> str:
    """``oci://127.0.0.1:5000/shop/api…`` -> ``oci://piceli-registry.<ns>.svc:5000/shop/api…``.

    Only a loopback host changes: the client's port-forward address is the
    cluster registry as the Job sees it. An explicit ``?tls=true`` is kept."""
    text = reference if reference.startswith("oci://") else f"oci://{reference}"
    try:
        found = CopySource.parse(text) if "@" in text else None
        target = None if found else RegistryTarget.parse(text)
    except (CopyError, ValueError):
        return reference  # the plan refuses it before this point
    repository = found.repository if found else target
    assert repository is not None
    if repository.host not in LOOPBACK:
        return reference
    path = repository.repository
    if found is not None:
        path += f"@{found.digest}"
    elif repository.tag:
        path += f":{repository.tag}"
    query = "?tls=true" if repository.tls else ""
    return f"oci://{registry_service(namespace)}/{path}{query}"


def rewrite_for_cluster(item: CopyItem, namespace: str) -> CopyItem:
    """The item the Job runs: loopback registries named by the cluster Service,
    and no credentials path (the Job reads its Secret)."""
    return CopyItem(_rewrite(item.source, namespace), _rewrite(item.target, namespace))


# ---------------------------------------------------------------- the lines
def encode(document: Mapping[str, Any]) -> str:
    """One printable line: the document as gzip and base64."""
    body = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return base64.b64encode(gzip.compress(body, mtime=0)).decode()


def decode(text: str) -> dict[str, Any]:
    value = json.loads(gzip.decompress(base64.b64decode(text.strip(), validate=True)))
    if not isinstance(value, dict):
        raise ValueError("not an object")
    return value


def last_line(log: str, marker: str) -> dict[str, Any] | None:
    """The document of the last ``marker`` line of ``log``, or ``None``."""
    for line in reversed(log.splitlines()):
        if line.startswith(marker):
            try:
                return decode(line[len(marker) :])
            except (ValueError, OSError, EOFError):
                continue
    return None


def merge_ledgers(
    local: Mapping[str, Any], remote: Mapping[str, Any]
) -> dict[str, Any]:
    """Per item, a ``done`` record wins; otherwise the Job's (the newer)."""
    first, second = ledger_records(local), ledger_records(remote)
    merged: dict[str, Any] = {}
    for key in {**first, **second}:
        old, new = first.get(key), second.get(key)
        if old is not None and old.get("state") == "done":
            merged[key] = old
        else:
            merged[key] = new if new is not None else old
    return {
        "revision": LEDGER_REVISION,
        "updated_at": remote.get("updated_at") or local.get("updated_at"),
        "items": merged,
    }


# ------------------------------------------------------------------ the Job
def job_name(ledger: str, items: Sequence[CopyItem]) -> str:
    """``piceli-copy-<ledger>-<hash of the set>``: the same set and ledger name
    the same Job, so a rerun finds it."""
    keys = "".join(item.key for item in items)
    return f"piceli-copy-{slug(ledger, 28)}-{hashlib.sha256(keys.encode()).hexdigest()[:10]}"


def job_manifest(
    *,
    name: str,
    namespace: str,
    image: str,
    ledger: str,
    configmap: str,
    credentials_secret: str | None,
    attempts: int,
    deadline: int,
) -> dict[str, Any]:
    """The Job. Nothing secret is in it: the credentials are Secret references."""
    env: list[dict[str, Any]] = [
        {"name": "HOME", "value": "/tmp"},
        {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
    ]
    if credentials_secret is not None:
        for role in SECRET_ROLES:
            for field in SECRET_FIELDS:
                env.append(
                    {
                        "name": f"PICELI_COPY_{role.upper()}_{field.upper()}",
                        "valueFrom": {
                            "secretKeyRef": {
                                "name": credentials_secret,
                                "key": f"{role}-{field}",
                                "optional": True,
                            }
                        },
                    }
                )
    labels = {
        "app.kubernetes.io/managed-by": "piceli",
        COPY_LABEL: slug(ledger, 50),
    }
    container = {
        "name": "copy",
        "image": image,
        "command": ["piceli", "artifacts", "copy-job-run"],
        "args": [
            "--dir",
            SET_DIR,
            "--ledger",
            ledger,
            "--attempts",
            str(attempts),
        ],
        "env": env,
        "volumeMounts": [
            {"name": "set", "mountPath": SET_DIR, "readOnly": True},
            {"name": "tmp", "mountPath": "/tmp"},
        ],
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
        },
    }
    pod = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 10001,
            "runAsGroup": 10001,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [container],
        "volumes": [
            {"name": "set", "configMap": {"name": configmap}},
            {"name": "tmp", "emptyDir": {}},
        ],
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": deadline,
            "template": {"metadata": {"labels": labels}, "spec": pod},
        },
    }


def secret_data(
    source: RegistryCredentials | None, target: RegistryCredentials | None
) -> dict[str, str]:
    """The keys of the credentials Secret (values are the secrets: never print)."""
    data: dict[str, str] = {}
    for role, credentials in (("source", source), ("target", target)):
        if credentials is None:
            continue
        for field in SECRET_FIELDS:
            value = getattr(credentials, field)
            if value is not None:
                data[f"{role}-{field}"] = value
    return data


def credentials_from_env(
    environ: Mapping[str, str], role: str
) -> RegistryCredentials | None:
    """The ``source`` or ``target`` credentials the Secret put in the environment."""
    values = {
        field: environ.get(f"PICELI_COPY_{role.upper()}_{field.upper()}") or None
        for field in SECRET_FIELDS
    }
    if not any(values.values()):
        return None
    return RegistryCredentials(**values)


# --------------------------------------------------------- the cluster calls
class CopyCluster(PreRolloutCluster):
    """The reads and writes of one copy Job over an API client."""

    def job_path(self, name: str = "") -> str:
        base = f"/apis/batch/v1/namespaces/{self.namespace}/jobs"
        return f"{base}/{name}" if name else base

    def get_job(self, name: str) -> dict[str, Any] | None:
        found = self._get(self.job_path(name))
        return found if isinstance(found, dict) else None

    def create_job(self, job: Mapping[str, Any]) -> None:
        from kubernetes.client.exceptions import ApiException

        try:
            self._send(self.job_path(), "POST", dict(job), fieldManager="piceli-copy")
        except ApiException as error:
            raise CopyError(
                "copy-failed",
                f"the API refused to create the copy Job (HTTP {error.status})",
            ) from None

    def outcome(self, name: str, memo: dict[str, Any]) -> Outcome | None:
        """One look at Job ``name``: how it ended, or ``None`` while it runs."""
        return self._check(name, memo)

    @staticmethod
    def finished(job: Mapping[str, Any]) -> bool:
        return any(
            c.get("type") in {"Complete", "Failed"} and c.get("status") == "True"
            for c in (job.get("status") or {}).get("conditions") or ()
        )

    def logs(self, name: str) -> str:
        """The last lines of the Job's pod log (the ledger and report lines)."""
        from kubernetes.client.exceptions import ApiException

        pod = self._pod(name)
        if pod is None:
            return ""
        try:
            response = self.client.call_api(
                self._ns("pods", pod["metadata"]["name"], "log"),
                "GET",
                query_params=[
                    ("container", "copy"),
                    ("tailLines", 30),
                    ("limitBytes", 16 * 1024 * 1024),
                ],
                header_params={"Accept": "*/*"},
                auth_settings=["BearerToken"],
                _preload_content=False,
                _request_timeout=self.request_seconds,
            )
        except ApiException:
            return ""
        return _raw(response).data.decode(errors="replace")

    def apply_secret(self, name: str, data: Mapping[str, str]) -> None:
        """Create or replace Secret ``name`` (the values are never printed)."""
        from kubernetes.client.exceptions import ApiException

        body: dict[str, Any] = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": name,
                "namespace": self.namespace,
                "labels": {
                    "app.kubernetes.io/managed-by": "piceli",
                    COPY_LABEL: "true",
                },
            },
            "type": "Opaque",
            "stringData": dict(data),
        }
        path = self._ns("secrets", name)
        try:
            current = self._get(path)
            if current is None:
                self._send(
                    self._ns("secrets"), "POST", body, fieldManager="piceli-copy"
                )
            else:
                body["metadata"]["resourceVersion"] = current["metadata"][
                    "resourceVersion"
                ]
                self._send(path, "PUT", body, fieldManager="piceli-copy")
        except (ApiException, Unreadable) as error:
            status = getattr(error, "status", "refused")
            raise CopyError(
                "copy-failed", f"the API refused the credentials Secret (HTTP {status})"
            ) from None

    def put_configmap(self, ledger: str, keys: Mapping[str, str]) -> None:
        """Set ``keys`` of the ledger's ConfigMap (the Job mounts it)."""
        ConfigMapLedger(self.client, self.namespace).put(ledger, keys)

    def controller_image(self) -> str:
        """The image the controller builds with (it holds ``piceli``)."""
        return _controller_image(self.client, self.namespace)

    def delete_secret(self, name: str) -> None:
        from kubernetes.client.exceptions import ApiException

        try:
            self._send(self._ns("secrets", name), "DELETE", {})
        except ApiException:
            pass


def _controller_image(kube: Any, namespace: str) -> str:
    """The image the controller builds with (it holds ``piceli``)."""
    from piceli.gitops.state import ConfigMapChannel

    try:
        status = ConfigMapChannel(kube, namespace).read_status() or {}
    except Exception:
        status = {}
    image = (status.get("controller") or {}).get("builder_image")
    if not isinstance(image, str) or not image:
        raise CopySetError(
            "copy-invalid",
            "the controller publishes no image; name the Job's image with --image",
        )
    return image


def _from_ledger(
    items: Sequence[CopyItem],
    ledger: str,
    document: Mapping[str, Any],
    reason: str,
    *,
    resumed: bool = False,
) -> CopyReport:
    """A report from the ledger alone (the Job printed no report, or, with
    ``resumed``, every copy was done before this run)."""
    records = ledger_records(document)
    report = CopyReport(ledger)
    broke = False
    for item in items:
        record = records.get(item.key) or {}
        state = record.get("state")
        result = ItemResult(
            item.key,
            item.source,
            item.target,
            "done" if state == "done" else "pending",
            attempts=int(record.get("attempts") or 0),
            reason=record.get("reason"),
            receipt_digest=record.get("receipt_digest"),
        )
        if state == "done":  # 0.87.0: what the done copy copied, from the ledger
            result.skipped = resumed
            result.receipt = resumed_receipt(record)
        if state != "done" and not broke:
            result.state, broke = "failed", True
            result.reason = record.get("reason") or reason
        report.items.append(result)
    return report


def copy_in_cluster(
    items: Sequence[CopyItem],
    *,
    ledger: str,
    namespace: str,
    credentials_secret: str | None,
    kube: Any,
    attempts: int = 5,
    deadline: float = 1800.0,
    image: str | None = None,
    options: CopyOptions | None = None,
    store: LedgerStore | None = None,
    cluster: CopyCluster | None = None,
    say: Callable[[str], None] = lambda _line: None,
) -> CopyReport:
    """Copy ``items`` with a Job in ``namespace``; return the report.

    :param items: As typed (a loopback registry is renamed to the cluster
        Service; credentials paths are ignored: see ``credentials_secret``).
    :param ledger: The ledger name (the Job and ConfigMap are named after it).
    :param credentials_secret: An existing Secret with keys
        ``source-username``, ``source-password``, ``source-token`` and the same
        for ``target`` (each optional), or ``None`` for anonymous.
    :param kube: A Kubernetes ``ApiClient`` for an explicit kubeconfig.
    :param attempts: The Job's whole-copy retries per item.
    :param deadline: Seconds the Job may run (``activeDeadlineSeconds``).
    :param image: The Job's image; default the controller's.
    :param store: Where the ledger is kept; default ConfigMap ``piceli-copy-<ledger>``.
    :raises CopySetError: ``copy-invalid`` (inputs, a missing Secret or image).
    :raises CopyError: ``copy-failed`` (the API refused a write).
    """
    validate_name(ledger)
    found = check_items(items)
    if isinstance(attempts, bool) or attempts < 1 or deadline <= 0:
        raise CopySetError("copy-invalid", "--attempts and --timeout must be positive")
    options = options or CopyOptions(timeout=deadline)
    owned = cluster is None
    cluster = cluster or CopyCluster(kube, namespace)
    store = store or ConfigMapLedger(kube, namespace)
    try:
        return _run(
            found,
            ledger=ledger,
            namespace=namespace,
            credentials_secret=credentials_secret,
            attempts=attempts,
            deadline=deadline,
            image=image,
            options=options,
            store=store,
            cluster=cluster,
            say=say,
        )
    finally:
        if owned:
            cluster.close()


def _run(
    items: list[CopyItem],
    *,
    ledger: str,
    namespace: str,
    credentials_secret: str | None,
    attempts: int,
    deadline: float,
    image: str | None,
    options: CopyOptions,
    store: LedgerStore,
    cluster: CopyCluster,
    say: Callable[[str], None],
) -> CopyReport:
    sent = [rewrite_for_cluster(item, namespace) for item in items]
    name = job_name(ledger, sent)
    started = cluster.clock()
    job = cluster.get_job(name)
    attached = job is not None
    if job is not None and cluster.finished(job):
        say(f"[copy] Job {name} already ended; reading its result")
        ended = cluster.outcome(name, {}) or Outcome("passed")
        report = _harvest(cluster, store, ledger, sent, name, ended, attached)
        cluster.delete_job(name)
        if report.ok:
            return report
        job = None
        attached = False
    if job is None:
        if credentials_secret is not None:
            keys = cluster.read_object("Secret", credentials_secret)
            if keys is None:
                raise CopySetError(
                    "copy-invalid",
                    f"the credentials Secret {credentials_secret!r} does not exist",
                )
        document = store.load(ledger)
        records = ledger_records(document)
        if all((records.get(item.key) or {}).get("state") == "done" for item in sent):
            say("[copy] every copy in the ledger is done")
            return _from_ledger(sent, ledger, document, "copy-failed", resumed=True)
        image = image or cluster.controller_image()
        configmap = ConfigMapLedger.configmap(ledger)
        setting = {
            "revision": SET_REVISION,
            "items": [item.to_dict() for item in sent],
            "options": options.to_dict(),
            "ledger": document,
        }
        cluster.put_configmap(ledger, {SET_KEY: json.dumps(setting, sort_keys=True)})
        cluster.create_job(
            job_manifest(
                name=name,
                namespace=namespace,
                image=image,
                ledger=ledger,
                configmap=configmap,
                credentials_secret=credentials_secret,
                attempts=attempts,
                deadline=int(deadline),
            )
        )
        say(f"[copy] Job {name} created in {namespace}")
    else:
        say(f"[copy] Job {name} is running; attaching to it")
    memo: dict[str, Any] = {}
    while True:
        outcome = cluster.outcome(name, memo)
        if outcome is not None:
            break
        if cluster.clock() - started >= deadline + DEADLINE_SLACK_SECONDS:
            outcome = Outcome("timeout", reason="deadline")
            break
        cluster.sleep(cluster.poll_seconds)
    outcome.seconds = cluster.clock() - started
    report = _harvest(cluster, store, ledger, sent, name, outcome, attached)
    if report.ok:
        cluster.delete_job(name)
    else:
        say(f"[copy] Job {name} is kept until the next run (its log has the detail)")
    return report


def _harvest(
    cluster: CopyCluster,
    store: LedgerStore,
    ledger: str,
    items: Sequence[CopyItem],
    name: str,
    outcome: Outcome,
    attached: bool,
) -> CopyReport:
    """Read the Job's ledger and report from its log; save the ledger."""
    log = cluster.logs(name)
    printed = last_line(log, LEDGER_MARKER)
    if printed is not None:
        store.save(ledger, merge_ledgers(store.load(ledger), printed))
    result = last_line(log, RESULT_MARKER)
    report: CopyReport | None = None
    if result is not None:
        try:
            report = CopyReport.from_dict(result)
        except (KeyError, TypeError, ValueError):
            report = None
    if report is None:
        reason = (
            outcome.reason
            if outcome.state != "passed" and outcome.reason
            else "copy-failed"
        )
        report = _from_ledger(items, ledger, store.load(ledger), reason)
    report.job = {
        "name": name,
        "namespace": cluster.namespace,
        "outcome": outcome.state,
        "attached": attached,
        "seconds": round(outcome.seconds, 1),
    }
    if result is None and log:
        report.job["log_tail"] = scrub(log, ())[-2000:]
    return report


# ---------------------------------------------------------- inside the Job
class LogLedger:
    """The Job's ledger: the one in the mounted ConfigMap to start with, and
    every save printed as a ledger line for the client to read."""

    def __init__(self, initial: Mapping[str, Any], emit: Callable[[str], None]) -> None:
        self.document: dict[str, Any] = dict(initial)
        self.emit = emit

    def load(self, name: str) -> dict[str, Any]:
        return json.loads(json.dumps(self.document))

    def save(self, name: str, data: dict[str, Any]) -> None:
        self.document = json.loads(json.dumps(data))
        self.emit(LEDGER_MARKER + encode(data))


def run_job(
    directory: Any,
    *,
    ledger: str,
    attempts: int,
    environ: Mapping[str, str],
    emit: Callable[[str], None],
    say: Callable[[str], None] = lambda _line: None,
    sleep: Callable[[float], None] = time.sleep,
    copy: Callable[[CopyItem], dict[str, Any]] | None = None,
) -> CopyReport:
    """What the Job runs: read the set the client mounted, copy it, print the
    report as the last line (``emit`` writes a line to stdout)."""
    from pathlib import Path

    try:
        setting = json.loads((Path(directory) / SET_KEY).read_text())
        if setting.get("revision") != SET_REVISION:
            raise ValueError("revision")
        items = check_items([CopyItem.from_dict(entry) for entry in setting["items"]])
        options = CopyOptions.from_dict(setting.get("options") or {})
    except (OSError, ValueError, KeyError, TypeError):
        raise CopySetError("copy-invalid", "cannot read the mounted set") from None
    store = LogLedger(setting.get("ledger") or {}, emit)
    report = run_copy_set(
        items,
        ledger=ledger,
        store=store,
        attempts=attempts,
        sleep=sleep,
        say=say,
        copy=copy
        or prepare_copies(
            items,
            options,
            source_credentials=credentials_from_env(environ, "source"),
            target_credentials=credentials_from_env(environ, "target"),
        ),
    )
    emit(RESULT_MARKER + encode(report.to_dict()))
    return report


__all__ = [
    "CopyCluster",
    "LogLedger",
    "copy_in_cluster",
    "credentials_from_env",
    "job_manifest",
    "job_name",
    "registry_service",
    "rewrite_for_cluster",
    "run_job",
    "secret_data",
]
