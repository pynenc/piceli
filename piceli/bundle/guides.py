"""``INSTALL.md`` and ``UNINSTALL.md`` of a client bundle.

Every command is in a fenced ``sh`` block under a numbered heading, written
to be run as it is, in order, in one shell, from the bundle's directory. The
only values the client chooses are the variables of the first block
(``NAMESPACE``, ``REGISTRY``; defaults from the environment), so a test can
run the guide itself. Importing this module is side-effect free.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from piceli.bundle.build import Bundle

_ROLLOUT = {
    "Deployment": "deployment",
    "StatefulSet": "statefulset",
    "DaemonSet": "daemonset",
}


def _block(lines: list[str]) -> str:
    return "```sh\n" + "\n".join(lines) + "\n```\n"


def _checksums() -> list[str]:
    return [
        "if command -v sha256sum >/dev/null 2>&1; then sha256sum -c SHA256SUMS; "
        "else shasum -a 256 -c SHA256SUMS; fi"
    ]


def install(bundle: Bundle) -> str:
    from piceli.bundle.build import OVERLAY, REGISTRY_PLACEHOLDER

    built = [item for item in bundle.images if item.source == "receipt"]
    external = [item for item in bundle.images if item.source == "external"]
    parts = [
        f"# Install {bundle.name} {bundle.version}\n",
        "Everything installs into one namespace of your cluster; nothing of it is "
        "reachable from outside the cluster and nothing calls back to us. "
        "`kubectl` uses your current context: check it with "
        "`kubectl config current-context` first.\n",
        "Needs: `kubectl` 1.27 or later (kustomize 5), `openssl` and a POSIX "
        "`sh` (for `prepare.sh`)"
        + (", and `skopeo` to copy the images into your registry" if built else "")
        + ". Run every block from this directory, in order, in one shell.\n",
        "## 1. Check the files\n",
        _block(_checksums()),
        "## 2. Choose the namespace" + (" and your registry\n" if built else "\n"),
        "The namespace must not exist yet."
        + (
            " `REGISTRY` is the registry path the images go to "
            "(`registry.example.com/team`); set `SKOPEO_FLAGS` for its "
            "credentials (`--dest-creds user:password`) or, for a plain-HTTP "
            "test registry, `--dest-tls-verify=false`.\n"
            if built
            else "\n"
        ),
        _block(
            [
                f'NAMESPACE="${{NAMESPACE:-{bundle.namespace}}}"',
                *(
                    [
                        'REGISTRY="${REGISTRY:?set REGISTRY to your registry path}"',
                        'SKOPEO_FLAGS="${SKOPEO_FLAGS:-}"',
                    ]
                    if built
                    else []
                ),
            ]
        ),
    ]
    step = 3
    if built:
        parts += [
            f"## {step}. Copy the images into your registry\n",
            "Each archive is an OCI image layout; `--preserve-digests` keeps "
            "the digest the overlay names. skopeo reads the containers policy "
            "its package installs (`/etc/containers/policy.json`). (`crane "
            "push` or `oras cp` of the extracted layout work too.)\n",
            _block(
                [
                    f"skopeo copy $SKOPEO_FLAGS --all --preserve-digests "
                    f"oci-archive:images/{item.name}.oci.tar "
                    f'"docker://$REGISTRY/{item.name}:{bundle.version}"'
                    for item in built
                ]
            ),
        ]
        step += 1
    configure = [
        f'sed -i.orig "s#^  namespace: .*#  namespace: $NAMESPACE#" overlays/{OVERLAY}/namespace.yaml',
        f"rm overlays/{OVERLAY}/namespace.yaml.orig",
    ]
    if built:
        configure += [
            f'sed -i.orig "s#{REGISTRY_PLACEHOLDER}#$REGISTRY#g" overlays/{OVERLAY}/images.yaml',
            f"rm overlays/{OVERLAY}/images.yaml.orig",
        ]
    parts += [
        f"## {step}. Point the overlay at your namespace"
        + (" and registry\n" if built else "\n"),
        f"`overlays/{OVERLAY}/` holds what you may change: `namespace.yaml`, "
        + ("`images.yaml` (registry and digest of each image), " if built else "")
        + "`resources.yaml` (requests and limits)"
        + (
            " and `storage.yaml` (storage class and sizes)"
            if _has_storage(bundle)
            else ""
        )
        + ". These commands set the namespace"
        + (" and the registry" if built else "")
        + ":\n",
        _block(configure),
    ]
    step += 1
    if bundle.inputs:
        parts += [
            f"## {step}. Values you provide\n",
            "`prepare.sh` reads these values from files you name (it never "
            "prints them):\n",
            _block(
                [
                    f'export {item["variable"]}="${{{item["variable"]}:?path to the file holding {item["generator"]}}}"'
                    for item in bundle.inputs
                ]
            ),
        ]
        step += 1
    rollout = []
    for item in bundle.workloads:
        if item.kind in _ROLLOUT:
            rollout.append(
                f'kubectl -n "$NAMESPACE" rollout status {_ROLLOUT[item.kind]}/{item.name} --timeout=300s'
            )
        elif item.kind == "Job":
            rollout.append(
                f'kubectl -n "$NAMESPACE" wait --for=condition=complete job/{item.name} --timeout=300s'
            )
    parts += [
        f"## {step}. Install\n",
        "`prepare.sh` creates the Secrets in your cluster (random tokens"
        + (", a private CA and its certificates" if _has_tls(bundle) else "")
        + "); their values never leave it. It is safe to run twice.\n",
        _block(
            [
                'kubectl create namespace "$NAMESPACE"',
                './prepare.sh "$NAMESPACE"',
                f"kubectl apply -k overlays/{OVERLAY}",
                *rollout,
            ]
        ),
    ]
    step += 1
    if bundle.forward is not None:
        forward = bundle.forward
        parts += [
            f"## {step}. Open it\n",
            f"Then open http://127.0.0.1:{forward.local}/ (Ctrl-C stops the forward).\n",
            _block(
                [
                    f'kubectl -n "$NAMESPACE" port-forward svc/{forward.service} '
                    f"{forward.local}:{forward.remote}"
                ]
            ),
        ]
    parts.append(_what(bundle, external))
    return "\n".join(parts)


def _has_storage(bundle: Bundle) -> bool:
    return any(
        item.kind == "PersistentVolumeClaim"
        or (
            item.kind == "StatefulSet"
            and (item.manifest.get("spec") or {}).get("volumeClaimTemplates")
        )
        for item in bundle.objects
    )


def _has_tls(bundle: Bundle) -> bool:
    return any(
        str(getattr(spec, "type", "")).startswith("tls")
        for spec in bundle.generators.values()
    )


def _what(bundle: Bundle, external: list) -> str:
    lines = ["## What this bundle puts in your cluster\n"]
    namespaced = [item for item in bundle.objects if not item.cluster]
    cluster = [item for item in bundle.objects if item.cluster]
    lines.append(
        f"- {len(namespaced)} object(s) in the namespace, each labelled "
        f"`app.kubernetes.io/part-of={bundle.name}`: "
        + ", ".join(f"{item.kind}/{item.name}" for item in namespaced)
        + "."
    )
    if cluster:
        lines.append(
            "- Cluster-scoped: "
            + ", ".join(f"{item.kind}/{item.name}" for item in cluster)
            + " (read-only; removed by UNINSTALL.md)."
        )
    if bundle.secrets:
        lines.append(
            "- Secrets made by `prepare.sh`: "
            + ", ".join(
                f"{item.name} ({', '.join(key for key, _ in item.keys)})"
                for item in bundle.secrets
            )
            + "."
        )
    lines.append(
        f"- NetworkPolicy `{bundle.name}-default-deny`: ingress only from this "
        "namespace; egress only to it and to the cluster DNS."
    )
    for item in bundle.egress:
        lines.append(
            f"- Egress the app declares (policy `{item['policy']}`): to "
            f"{_short(item['to'])} on {_short(item['ports'])}."
        )
    lines.append(
        "- Every container runs as a non-root user with a read-only root "
        "filesystem, no privilege escalation, and cpu and memory requests and "
        "limits; no Service opens a node port."
    )
    for item in bundle.exceptions:
        lines.append(
            f"- Exception: {item.kind}/{item.name} does not follow `{item.rule}`: "
            f"{item.reason}"
        )
    for image in external:
        lines.append(
            f"- Image pulled as is (not in `images/`): `{image.reference}`; "
            "copy it into your registry and change the reference in the overlay "
            "if your nodes cannot pull it."
        )
    return "\n".join(lines) + "\n"


def _short(value: object) -> str:
    import json

    return value if isinstance(value, str) else json.dumps(value, sort_keys=True)


def uninstall(bundle: Bundle) -> str:
    label = f"app.kubernetes.io/part-of={bundle.name}"
    commands = [
        f'NAMESPACE="${{NAMESPACE:-{bundle.namespace}}}"',
        'kubectl delete namespace "$NAMESPACE" --wait=true',
    ]
    kinds = bundle.cluster_kinds
    if kinds:
        commands.append(
            f"kubectl delete {','.join(kinds)} -l {label} --ignore-not-found"
        )
    parts = [
        f"# Uninstall {bundle.name} {bundle.version}\n",
        "Deleting the namespace removes every namespaced object, the Secrets "
        "`prepare.sh` made and the volumes' claims"
        + (
            "; the second command removes the cluster-scoped objects by their label"
            if kinds
            else ""
        )
        + "."
        + (
            " Volumes of a storage class that retains them (`reclaimPolicy: "
            "Retain`) outlive their claims: `kubectl get pv` lists them."
            if _has_storage(bundle)
            else ""
        )
        + "\n",
        "## 1. Remove\n",
        _block(commands),
        "## 2. Check that nothing is left\n",
        "The first command prints nothing; the second `No resources found`.\n",
        _block(
            [
                'kubectl get namespace "$NAMESPACE" --ignore-not-found',
                f"kubectl get {','.join(kinds or ['clusterrole'])} -l {label}",
            ]
        ),
    ]
    built = [item for item in bundle.images if item.source == "receipt"]
    if built:
        parts.append(
            "## 3. Delete the images\n\nDelete these repositories from your "
            "registry: "
            + ", ".join(f"`$REGISTRY/{item.name}`" for item in built)
            + ". Node image caches expire on their own.\n"
        )
    return "\n".join(parts)
