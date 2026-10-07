"""Host builds as a Kubernetes Job on a labelled builder node.

``run_build_job(pipeline, commit, ...)`` runs the pipeline's host builds
(:mod:`piceli.artifacts.host_build`) inside the cluster instead of on a
laptop: one Job on a node that carries the builder labels (``amd64`` by
default), which

1. fetches the source at exactly ``commit`` (a Git fetch; the credentials come
   from a Secret through ``GIT_ASKPASS`` and are never printed),
2. builds for every requested platform (an ``arm64`` target is cross-built
   with the declared tools, for example ``cargo-zigbuild``),
3. pushes each image by digest to the node registry
   (:class:`~piceli.artifacts.registry_delivery.RegistryDelivery`, only blobs
   the registry lacks are sent), and
4. prints one receipt line that the caller reads back from the pod log.

The build cache lives on a claim named after the branch (``cache_key``) **and**
the page sizes built for, so a branch keeps its incremental state and builds
for 4 KiB and 16 KiB pages never share it (inside it, each architecture and
page size also has its own target directory).

The returned receipt has the shape of a local host build's receipt
(``outputs.images.<name>`` with ``digest``, ``image_id`` ...) for the first
platform, plus additive keys: ``delivered`` (per platform and image, the pull
reference by digest), ``platform_receipts`` (the receipts of the other
platforms) and ``job`` (Job name, commit, cache claim, seconds).

The plan (:func:`plan_build_job`) is the Job and claim manifests and their
hash; nothing in it is secret (only the Secret's *name*). The caller approves
that hash (``approve=``); a controller applies its own policy.

Importing this module runs nothing.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from piceli.artifacts.node_facts import NodeFacts, NodeFactsError
from piceli.pipeline.errors import PipelineError
from piceli.pipeline.prerollout import scrub
from piceli.pipeline.prerollout_cluster import (
    Outcome,
    PreRolloutCluster,
    Unreadable,
    _raw,
)

if TYPE_CHECKING:
    from piceli.pipeline.model import Pipeline

RECEIPT_MARKER = "PICELI-BUILD-RECEIPT "
BUILD_LABEL = "piceli.io/build"
CACHE_LABEL = "piceli.io/build-cache"
DEFAULT_SELECTOR = {"piceli.io/builder": "true", "kubernetes.io/arch": "amd64"}
DEFAULT_PLATFORMS = ("linux/arm64", "linux/amd64")
_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_PLATFORM = re.compile(r"linux/(?:amd64|arm64)")
_DNS = re.compile(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?")
_CACHE_DIR = "/cache"
_WORK_DIR = "/work"


def slug(value: str, limit: int = 40) -> str:
    """A DNS-label-safe form of a branch name (``wp/Fix_1`` -> ``wp-fix-1``).

    A name that had to change or be shortened gets a short hash suffix, so two
    branches never share a slug.
    """
    plain = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    digest = hashlib.sha256(value.encode()).hexdigest()[:6]
    if not plain:
        return digest
    if plain != value or len(plain) > limit:
        return f"{plain[: limit - 7].rstrip('-')}-{digest}"
    return plain


def cache_claim_name(prefix: str, cache_key: str, page_sizes: Sequence[int]) -> str:
    """The cache claim of one branch and one set of page sizes."""
    sizes = "-".join(f"{size // 1024}k" for size in sorted(set(page_sizes)))
    return f"{prefix}-{slug(cache_key, 32)}-{sizes}"[:63].rstrip("-")


@dataclass(frozen=True)
class ClusterBuildConfig:
    """How and where the build Job runs.

    :param image: The builder image (pinned by digest), holding ``piceli``
        and the declared host tools.
    :param repo: The Git remote (``https://`` or ``ssh://`` URL without
        credentials in it).
    :param git_secret: A Secret with keys ``username`` and ``password`` (a
        token) used to fetch ``repo``.
    :param selector: The builder node's labels (default: ``piceli.io/builder``
        and ``kubernetes.io/arch: amd64``).
    :param tolerations: Tolerations of the Job's pod.
    :param namespace: Where the Job and the cache claims live (default: the
        pipeline target's namespace).
    :param storage: Size of a cache claim.
    :param storage_class: Its storage class (default: the cluster's).
    :param cache_prefix: Name prefix of the cache claims.
    :param timeout_seconds: The Job's deadline.
    :param registry_url: ``oci://host[:port]/prefix`` the Job pushes to;
        default from the pipeline's delivery strategy.
    :param node_registry: ``host[:port]`` the nodes pull from, when different.
    :param host_network: Run the pod on the node's network, so a registry on
        the node's loopback is reachable (default: only for a loopback
        registry).
    :param spec_paths: ``{host-build name: path of its host-build.toml in the
        repository}``; default the build's path relative to ``repo_root``.
    :param repo_root: The repository's root on the machine that declares the
        pipeline (default: the pipeline's directory).
    :param resources: The pod's container resources (``requests``/``limits``).
    """

    image: str
    repo: str
    git_secret: str = "piceli-build-git"
    selector: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_SELECTOR))
    tolerations: Sequence[Mapping[str, Any]] = ()
    namespace: str | None = None
    storage: str = "20Gi"
    storage_class: str | None = None
    cache_prefix: str = "piceli-build-cache"
    timeout_seconds: int = 3600
    registry_url: str | None = None
    node_registry: str | None = None
    host_network: bool | None = None
    spec_paths: Mapping[str, str] = field(default_factory=dict)
    repo_root: Path | None = None
    resources: Mapping[str, Any] | None = None
    # Optional, so existing CLI plans and their hashes remain unchanged.
    service_account: str | None = None

    def __post_init__(self) -> None:
        if self.service_account is not None and not _DNS.fullmatch(
            self.service_account
        ):
            raise _invalid("build service account must be a DNS label")
        image, sep, digest = self.image.partition("@")
        if not image or not sep or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise _invalid("the builder image must be pinned (name@sha256:<digest>)")
        if re.match(r"[a-z+]+://[^/]*@", self.repo) or not re.match(
            r"(?:https?|ssh|git)://[^\s]+$", self.repo
        ):
            raise _invalid(
                "repo must be an https://, ssh:// or git:// URL without credentials"
            )
        if not self.selector or not _DNS.fullmatch(self.git_secret):
            raise _invalid(
                "a builder selector and a valid git secret name are required"
            )
        if self.timeout_seconds <= 0:
            raise _invalid("timeout_seconds must be positive")


@dataclass(frozen=True)
class BuildJobPlan:
    """What ``run_build_job`` would create. Holds no secret value."""

    job: dict[str, Any]
    claim: dict[str, Any]
    namespace: str
    commit: str
    cache_key: str
    platforms: tuple[str, ...]
    facts: dict[str, dict[str, Any]]
    git_secret: str
    plan_hash: str

    def preview(self) -> dict[str, Any]:
        """The printable plan: what runs where, from which commit, into which cache."""
        pod = self.job["spec"]["template"]["spec"]
        return {
            "plan_hash": self.plan_hash,
            "namespace": self.namespace,
            "job": self.job["metadata"]["name"],
            "commit": self.commit,
            "platforms": list(self.platforms),
            "node_selector": pod["nodeSelector"],
            "image": pod["containers"][0]["image"],
            "cache_claim": self.claim["metadata"]["name"],
            "git_secret": self.git_secret,
            "node_facts": self.facts,
        }


def _invalid(message: str) -> PipelineError:
    return PipelineError("cluster-build-invalid", message)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


# ------------------------------------------------------------------ planning
def plan_build_job(
    pipeline: Pipeline,
    commit: str,
    *,
    cache_key: str,
    platforms: Sequence[str] = DEFAULT_PLATFORMS,
    config: ClusterBuildConfig,
    facts: Mapping[str, NodeFacts] | None = None,
    backend: Any = None,
) -> BuildJobPlan:
    """The Job and cache claim for building ``pipeline`` at ``commit``.

    Reads node facts (declared on the build, from the API, or from the cache
    of the last successful read) and nothing else; changes nothing.

    :raises PipelineError: ``cluster-build-invalid`` for a bad commit,
        platform, key, registry or spec path; ``node-facts-unavailable`` when
        a platform's facts cannot be had.
    """
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
        raise _invalid("commit must be a full lowercase Git commit id")
    platforms = tuple(dict.fromkeys(platforms))
    if not platforms or any(not _PLATFORM.fullmatch(item) for item in platforms):
        raise _invalid("platforms must be linux/amd64 and/or linux/arm64")
    if not cache_key or len(cache_key) > 200:
        raise _invalid("a cache key (the branch) of 1-200 characters is required")
    builds = [item for item in pipeline.builds if item.builder == "host"]
    if not builds:
        raise _invalid("the pipeline declares no host build")
    used = dict(facts or {}) or _platform_facts(pipeline, builds, platforms, backend)
    missing = [item for item in platforms if item not in used]
    if missing:
        raise _invalid(f"no node facts for {', '.join(missing)}")
    namespace = config.namespace or pipeline.target.namespace
    claim = _claim(config, namespace, cache_key, [used[p].page_size for p in platforms])
    url, node_registry, host_network = _registry(pipeline, config)
    specs = _spec_paths(pipeline, builds, config)
    facts_json = {platform: used[platform].to_dict() for platform in platforms}
    job = _job(
        config,
        namespace,
        commit,
        claim["metadata"]["name"],
        platforms,
        facts_json,
        specs,
        url,
        node_registry,
        host_network,
        cache_key,
    )
    digest = "sha256:" + hashlib.sha256(_canonical([job, claim]).encode()).hexdigest()
    return BuildJobPlan(
        job=job,
        claim=claim,
        namespace=namespace,
        commit=commit,
        cache_key=cache_key,
        platforms=platforms,
        facts=facts_json,
        git_secret=config.git_secret,
        plan_hash=digest,
    )


def _platform_facts(
    pipeline: Pipeline,
    builds: Sequence[Any],
    platforms: Sequence[str],
    backend: Any,
) -> dict[str, NodeFacts]:
    """Facts per platform: the build's declared node facts, the target node's
    (API, else the cache of the last read) when its architecture matches, else
    the platform's defaults (4 KiB pages)."""
    from piceli.artifacts.node_facts import cached_facts, store_facts

    node_facts: NodeFacts | None = None
    build = builds[0]
    if build.node_facts is not None:
        node_facts = build.node_facts
    elif pipeline.target.nodes:
        _, node = pipeline.target.node(build.node)
        try:
            if backend is None:
                from piceli.pipeline.backend import Backend

                backend = Backend()
            node_facts = backend.node_facts(pipeline.target, node.name)
            store_facts(pipeline.state_dir, node_facts)
        except NodeFactsError as error:
            raise PipelineError(error.code, str(error)) from None
        except Exception:
            found = cached_facts(pipeline.state_dir, node.name)
            if found is None:
                raise PipelineError(
                    "node-facts-unavailable",
                    f"could not read node {node.name!r} and no cached facts exist; "
                    "declare them with Build.spec(node_facts=...)",
                ) from None
            node_facts = found[0]
    result: dict[str, NodeFacts] = {}
    for platform in platforms:
        if node_facts is not None and node_facts.platform == platform:
            result[platform] = node_facts
        else:
            result[platform] = NodeFacts.for_platform(platform)
    return result


def _registry(pipeline: Pipeline, config: ClusterBuildConfig) -> tuple[str, str, bool]:
    from piceli.pipeline.model import NodeLoopbackRegistry, Registry

    strategy = pipeline.deliver
    if config.registry_url is not None:
        url = config.registry_url
        node = config.node_registry or re.sub(r"^oci://([^/]+).*$", r"\1", url)
    elif isinstance(strategy, NodeLoopbackRegistry):
        prefix = strategy.repository or pipeline.app.name
        url = f"oci://127.0.0.1:{strategy.port}/{prefix}"
        node = f"127.0.0.1:{strategy.port}"
    elif isinstance(strategy, Registry):
        if strategy.credentials is not None:
            raise _invalid(
                "a cluster build pushes without credentials files: use the node "
                "registry or set registry_url"
            )
        url = strategy.url
        node = strategy.node_registry or re.sub(r"^oci://([^/]+).*$", r"\1", url)
    else:
        raise _invalid("a cluster build pushes to a Registry or NodeLoopbackRegistry")
    loopback = bool(re.match(r"oci://(?:127\.|localhost|\[::1\])", url))
    host_network = config.host_network if config.host_network is not None else loopback
    return url, node, host_network


def _spec_paths(
    pipeline: Pipeline, builds: Sequence[Any], config: ClusterBuildConfig
) -> list[str]:
    root = (config.repo_root or pipeline.base or Path.cwd()).absolute()
    paths = []
    for build in builds:
        name = build.load().name if config.spec_paths else ""
        declared = config.spec_paths.get(name)
        if declared is None:
            try:
                declared = build.path.absolute().relative_to(root).as_posix()
            except (AttributeError, ValueError):
                raise _invalid(
                    f"host build {name!r} is outside the repository root; set "
                    "spec_paths or repo_root"
                ) from None
        if declared.startswith("/") or ".." in Path(declared).parts:
            raise _invalid("spec paths are relative to the repository root")
        paths.append(declared)
    return paths


def _claim(
    config: ClusterBuildConfig,
    namespace: str,
    cache_key: str,
    page_sizes: Sequence[int],
) -> dict[str, Any]:
    name = cache_claim_name(config.cache_prefix, cache_key, page_sizes)
    spec: dict[str, Any] = {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": config.storage}},
    }
    if config.storage_class is not None:
        spec["storageClassName"] = config.storage_class
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "piceli",
                CACHE_LABEL: slug(cache_key, 50),
            },
        },
        "spec": spec,
    }


#: Fetches exactly the commit; the credentials reach git only through
#: ``GIT_ASKPASS`` reading the environment, never a URL, argument or log line.
_SCRIPT = f"""set -eu
export HOME={_WORK_DIR}/home GIT_TERMINAL_PROMPT=0
mkdir -p "$HOME" {_WORK_DIR}/src {_WORK_DIR}/out
cat > {_WORK_DIR}/askpass <<'ASK'
#!/bin/sh
case "$1" in Username*) printf %s "$GIT_USERNAME" ;; *) printf %s "$GIT_PASSWORD" ;; esac
ASK
chmod 700 {_WORK_DIR}/askpass
export GIT_ASKPASS={_WORK_DIR}/askpass
cd {_WORK_DIR}/src
git init -q .
git remote add origin "$PICELI_BUILD_REPO"
git fetch -q --depth 1 origin "$PICELI_BUILD_COMMIT"
git checkout -q --detach FETCH_HEAD
unset GIT_USERNAME GIT_PASSWORD
exec piceli build job-run --source {_WORK_DIR}/src --cache {_CACHE_DIR} \\
  --out {_WORK_DIR}/out --commit "$PICELI_BUILD_COMMIT" \\
  --registry-url "$PICELI_BUILD_REGISTRY" --node-registry "$PICELI_BUILD_NODE_REGISTRY" \\
  --timeout "$PICELI_BUILD_TIMEOUT" "$@"
"""


def _job(
    config: ClusterBuildConfig,
    namespace: str,
    commit: str,
    claim: str,
    platforms: Sequence[str],
    facts: Mapping[str, Any],
    specs: Sequence[str],
    registry_url: str,
    node_registry: str,
    host_network: bool,
    cache_key: str,
) -> dict[str, Any]:
    key = hashlib.sha256(
        _canonical([commit, claim, list(platforms), facts, list(specs)]).encode()
    ).hexdigest()[:10]
    secret = config.git_secret
    env: list[dict[str, Any]] = [
        {"name": "PICELI_BUILD_REPO", "value": config.repo},
        {"name": "PICELI_BUILD_COMMIT", "value": commit},
        {"name": "PICELI_BUILD_REGISTRY", "value": registry_url},
        {"name": "PICELI_BUILD_NODE_REGISTRY", "value": node_registry},
        {"name": "PICELI_BUILD_TIMEOUT", "value": str(config.timeout_seconds)},
        {"name": "PICELI_BUILD_FACTS", "value": _canonical(facts)},
        {
            "name": "GIT_USERNAME",
            "valueFrom": {"secretKeyRef": {"name": secret, "key": "username"}},
        },
        {
            "name": "GIT_PASSWORD",
            "valueFrom": {"secretKeyRef": {"name": secret, "key": "password"}},
        },
    ]
    arguments: list[str] = []
    for spec in specs:
        arguments += ["--spec", spec]
    container: dict[str, Any] = {
        "name": "build",
        "image": config.image,
        "command": ["sh", "-c", _SCRIPT, "piceli-build", *arguments],
        "env": env,
        "volumeMounts": [
            {"name": "cache", "mountPath": _CACHE_DIR},
            {"name": "work", "mountPath": _WORK_DIR},
        ],
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
        },
    }
    if config.resources:
        container["resources"] = dict(config.resources)
    pod: dict[str, Any] = {
        "restartPolicy": "Never",
        "nodeSelector": dict(config.selector),
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 10001,
            "runAsGroup": 10001,
            "fsGroup": 10001,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [container],
        "volumes": [
            {"name": "cache", "persistentVolumeClaim": {"claimName": claim}},
            {"name": "work", "emptyDir": {}},
        ],
    }
    if config.service_account is not None:
        pod["serviceAccountName"] = config.service_account
        pod["automountServiceAccountToken"] = False
    if config.tolerations:
        pod["tolerations"] = [dict(item) for item in config.tolerations]
    if host_network:
        pod["hostNetwork"] = True
        pod["dnsPolicy"] = "ClusterFirstWithHostNet"
    labels = {
        "app.kubernetes.io/managed-by": "piceli",
        BUILD_LABEL: "true",
        CACHE_LABEL: slug(cache_key, 50),
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": f"piceli-build-{key}",
            "namespace": namespace,
            "labels": labels,
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": config.timeout_seconds,
            "template": {"metadata": {"labels": labels}, "spec": pod},
        },
    }


# ------------------------------------------------------------------- running
class BuildCluster(PreRolloutCluster):
    """The Job runner, reading the receipt line the build prints last.

    :meth:`run_build` keeps a failed build Job (and its pod, whose log holds
    the failing command's output) until the next build of the same key
    replaces it; a passed Job is removed as before.
    """

    receipt_text: str | None = None
    log_access: str = "not-attempted"
    _keeping: bool = False
    _job_failed: bool = False
    kept_job: str | None = None

    def run_build(
        self,
        job: Mapping[str, Any],
        key: Mapping[str, str],
        *,
        keep_earlier: bool = False,
    ) -> Outcome:
        """Run a build Job; keep it when it fails (``outcome.extra["kept_job"]``).

        ``key`` are the labels of one build key (a branch's cache): failed
        Jobs kept by earlier attempts with those labels are removed first, or
        with ``keep_earlier`` (0.17.0) only once this build passed, so the
        failed attempts' Jobs and logs stay while it is retried.
        """
        if not keep_earlier:
            self.remove_kept(key)
        self.kept_job = None
        self._job_failed = False
        self._keeping = True
        try:
            outcome = self.run_job(job)
        finally:
            self._keeping = False
        if self.kept_job is not None:
            outcome.extra["kept_job"] = self.kept_job
        if keep_earlier and outcome.state == "passed":
            self.remove_kept(key)
        return outcome

    def remove_kept(self, key: Mapping[str, str]) -> list[str]:
        """Delete the failed Jobs with labels ``key``; their names."""
        selector = ",".join(f"{k}={v}" for k, v in sorted(key.items()))
        try:
            found = self._get(self._ns_jobs(), labelSelector=selector) or {}
        except Unreadable:
            return []
        removed = []
        for item in found.get("items") or ():
            conditions = (item.get("status") or {}).get("conditions") or ()
            failed = any(
                c.get("type") == "Failed" and c.get("status") == "True"
                for c in conditions
            )
            name = (item.get("metadata") or {}).get("name")
            if failed and isinstance(name, str) and super().delete_job(name):
                removed.append(name)
        return removed

    def _ns_jobs(self) -> str:
        return f"/apis/batch/v1/namespaces/{self.namespace}/jobs"

    def _wait(self, name: str, allowed: float, started: float) -> Outcome:
        outcome = super()._wait(name, allowed, started)
        # Only a Job that ran and failed is kept; a stuck or overdue one goes.
        self._job_failed = outcome.state == "failed" or (
            outcome.reason == "job-deadline-exceeded"
        )
        return outcome

    def delete_job(self, name: str) -> bool:
        if self._keeping and self._job_failed:
            self.kept_job = name
            return False
        return super().delete_job(name)

    def _logs(self, job: str, redact: Any) -> str:
        from kubernetes.client.exceptions import ApiException

        pod = self._pod(job)
        if pod is None:
            self.log_access = "pod-not-found"
            return ""
        self.receipt_text = None
        texts: list[str] = []
        statuses: list[int] = []
        # 0.17.0: 80 lines of evidence (60 before).
        for lines, limit in ((80, 32768), (2, 16 * 1024 * 1024)):
            try:
                response = self.client.call_api(
                    self._ns("pods", pod["metadata"]["name"], "log"),
                    "GET",
                    query_params=[
                        ("container", "build"),
                        ("tailLines", lines),
                        ("limitBytes", limit),
                    ],
                    header_params={"Accept": "*/*"},
                    auth_settings=["BearerToken"],
                    _preload_content=False,
                    _request_timeout=self.request_seconds,
                )
            except ApiException as error:
                statuses.append(error.status)
                texts.append("")
                continue
            texts.append(_raw(response).data.decode(errors="replace"))
        self.log_access = (
            "read" if any(texts) else f"http-{statuses[-1]}" if statuses else "empty"
        )
        for line in reversed(texts[1].splitlines() or texts[0].splitlines()):
            if line.startswith(RECEIPT_MARKER):
                self.receipt_text = line[len(RECEIPT_MARKER) :]
                break
        kept = [x for x in texts[0].splitlines() if not x.startswith(RECEIPT_MARKER)]
        return scrub("\n".join(kept), redact)

    def ensure_claim(self, claim: Mapping[str, Any]) -> bool:
        """Create the cache claim when it does not exist; ``True`` when created."""
        name = claim["metadata"]["name"]
        if self._get(self._ns("persistentvolumeclaims", name)) is not None:
            return False
        self._send(
            self._ns("persistentvolumeclaims"),
            "POST",
            dict(claim),
            fieldManager="piceli-build",
        )
        return True


def build_key(job: Mapping[str, Any]) -> dict[str, str]:
    """The labels of a build Job's key (its branch cache), for :meth:`BuildCluster.run_build`."""
    labels = job["metadata"].get("labels") or {}
    return {
        key: str(labels[key]) for key in (BUILD_LABEL, CACHE_LABEL) if key in labels
    }


def encode_receipt(receipt: Mapping[str, Any]) -> str:
    """One printable line: the receipt as gzip + base64."""
    raw = gzip.compress(_canonical(receipt).encode(), mtime=0)
    return RECEIPT_MARKER + base64.b64encode(raw).decode()


def decode_receipt(text: str) -> dict[str, Any]:
    """The receipt of :func:`encode_receipt` (without the marker)."""
    value = json.loads(gzip.decompress(base64.b64decode(text, validate=True)))
    if not isinstance(value, dict):
        raise ValueError("receipt is not an object")
    return value


def run_build_job(
    pipeline: Pipeline,
    commit: str,
    *,
    cache_key: str,
    platforms: Sequence[str] = DEFAULT_PLATFORMS,
    config: ClusterBuildConfig,
    approve: str | None = None,
    facts: Mapping[str, NodeFacts] | None = None,
    backend: Any = None,
    cluster: BuildCluster | None = None,
    say: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Build ``pipeline`` at ``commit`` in the cluster; return the build receipt.

    :param cache_key: The branch: the cache claim is named after it and the
        page sizes built for.
    :param platforms: ``linux/arm64`` and/or ``linux/amd64``; the first one's
        images are ``outputs.images`` of the receipt.
    :param approve: The plan hash the caller approved
        (:meth:`BuildJobPlan.plan_hash`); ``None`` when the caller is the
        approver (a controller applying its policy).
    :raises PipelineError: ``cluster-build-invalid`` (inputs, or an approved
        hash that no longer matches the plan), ``cluster-build-failed`` (the
        Job failed, timed out or printed no receipt; ``details`` holds the
        scrubbed log tail), ``node-facts-unavailable``.
    """
    say = say or (lambda _line: None)
    plan = plan_build_job(
        pipeline,
        commit,
        cache_key=cache_key,
        platforms=platforms,
        config=config,
        facts=facts,
        backend=backend,
    )
    if approve is not None and approve != plan.plan_hash:
        raise _invalid(
            "the approved hash is not the build job's current plan; plan again"
        )
    owned = cluster is None
    if cluster is None:
        if backend is None:
            from piceli.pipeline.backend import Backend

            backend = Backend()
        target = pipeline.target
        cluster = BuildCluster(
            backend._api(target),
            plan.namespace,
            request_seconds=target.request_seconds,
            poll_seconds=float(pipeline.execution.get("poll_seconds", 2.0)),
        )
    try:
        try:
            keys = cluster.read_object("Secret", plan.git_secret)
        except Unreadable:
            keys = {"username", "password"}  # cannot read it; the Job will say
        if keys is None or not {"username", "password"} <= keys:
            raise _invalid(
                f"the Git secret {plan.git_secret!r} must exist with keys "
                "username and password"
            )
        if cluster.ensure_claim(plan.claim):
            say(f"[build] created cache claim {plan.claim['metadata']['name']}")
        say(
            f"[build] Job {plan.job['metadata']['name']}: {commit[:12]} for "
            f"{', '.join(plan.platforms)}"
        )
        outcome = cluster.run_build(plan.job, build_key(plan.job))
    finally:
        if owned:
            cluster.close()
    if outcome.state != "passed" or cluster.receipt_text is None:
        kept = outcome.extra.get("kept_job")
        raise PipelineError(
            "cluster-build-failed",
            f"the build Job ended {outcome.state}"
            + (f" ({outcome.reason})" if outcome.reason else "")
            + (f"; Job {kept} is kept until the next build" if kept else ""),
            failed=True,
            details={
                "outcome": outcome.public(),
                "log_access": cluster.log_access,
                **({"kept_job": kept} if kept else {}),
            },
        )
    try:
        receipt = decode_receipt(cluster.receipt_text)
    except (ValueError, OSError):
        raise PipelineError(
            "cluster-build-failed",
            "the build Job printed no readable receipt",
            failed=True,
            details={"outcome": outcome.public(), "log_access": cluster.log_access},
        ) from None
    receipt["job"] = {
        "name": plan.job["metadata"]["name"],
        "namespace": plan.namespace,
        "commit": commit,
        "cache_claim": plan.claim["metadata"]["name"],
        "plan_hash": plan.plan_hash,
        "seconds": round(outcome.seconds, 1),
    }
    return receipt


# ---------------------------------------------------------- inside the Job
def job_run(
    *,
    source: Path,
    specs: Sequence[str],
    cache: Path,
    out: Path,
    registry_url: str,
    node_registry: str | None,
    facts: Mapping[str, NodeFacts],
    commit: str,
    timeout_seconds: int = 3600,
    say: Callable[[str], None] = lambda _line: None,
    runner: Any = None,
) -> dict[str, Any]:
    """Build every spec for every platform and push by digest; return the receipt.

    This is what the Job runs (``piceli build job-run``); it is the local host
    build (:meth:`HostBuildSpec.run`) plus delivery of each archive.
    """
    from piceli.artifacts.delivery import ArchiveSource, DeliveryGrant
    from piceli.artifacts.host_build import HostBuildGrant, HostBuildSpec
    from piceli.artifacts.registry import RegistryTarget
    from piceli.artifacts.registry_delivery import RegistryDelivery

    prefix = registry_url.rstrip("/")
    receipts: dict[str, dict[str, Any]] = {}
    delivered: dict[str, dict[str, Any]] = {}
    for platform, node in facts.items():
        merged: dict[str, Any] | None = None
        for relative in specs:
            spec = HostBuildSpec.from_toml(source / relative)
            spec = spec.for_node(node).with_cache_dir(cache)
            plan = spec.plan()
            grant = HostBuildGrant(plan.plan_hash, time.time() + timeout_seconds + 600)
            directory = out / platform.replace("/", "-") / spec.name
            say(f"[build] {spec.name} for {platform} ({node.page_size} byte pages)")
            receipt = spec.run(grant, directory, runner=runner, progress=say).to_dict()
            for name, entry in receipt["outputs"]["images"].items():
                url = f"{prefix}/{name}"
                target = RegistryTarget.parse(url)
                result = RegistryDelivery().deliver(
                    ArchiveSource((directory / entry["archive"]).absolute()),
                    target,
                    DeliveryGrant(entry["image_id"], url, time.time() + 3600),
                    node_registry=node_registry,
                )
                if result.get("state") != "succeeded":
                    raise PipelineError(
                        "cluster-build-failed",
                        f"delivery of {name!r} ended {result.get('result')} "
                        f"({result.get('reason')})",
                        failed=True,
                    )
                entry["delivered"] = {
                    "repository": target.repository,
                    "manifest_digest": result["image"]["manifest_digest"],
                    "pull_ref": result["pull_ref"],
                    "result": result["result"],
                }
                delivered.setdefault(platform, {})[name] = entry["delivered"]
            if merged is None:
                merged = receipt
            else:
                merged["outputs"]["images"].update(receipt["outputs"]["images"])
                merged.setdefault("builds", {})[spec.name] = receipt
        assert merged is not None
        receipts[platform] = merged
    first, *rest = list(receipts)
    result_receipt = dict(receipts[first])
    result_receipt["platforms"] = list(receipts)
    result_receipt["delivered"] = delivered
    result_receipt["commit"] = commit
    if rest:
        result_receipt["platform_receipts"] = {name: receipts[name] for name in rest}
    return result_receipt
