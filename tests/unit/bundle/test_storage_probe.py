"""0.94.0: the storage probe a client chart runs before install.

The chart side (values, schema, README, the hook objects and the safety
gate) is checked on the files ``build_helm`` writes, and with ``helm
template`` when helm is on PATH (``PICELI_COMPARISONS=require`` turns a
missing helm into a failure). The Job's script runs with ``sh`` against a
fake ``curl`` on PATH that answers the API calls from canned JSON; since
0.97.0 the Python script (the default image's) runs against a local HTTP
server giving the same answers, and every script test runs both.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml

from piceli import App, PodDefaults, Resources, Security
from piceli.bundle.build import BundleError, build_bundle
from piceli.bundle.helm import HelmChart, StorageProbe, build_helm, write_helm
from piceli.bundle.probe import (
    DEFAULT_IMAGE,
    PYTHON_SCRIPT,
    SCRIPT,
    as_json,
    claim_manifest,
    hook_objects,
    literal_names,
    pod_manifest,
    scheduling,
)
from piceli.bundle.safety import check
from tests.unit.bundle import helm_app

HELM = shutil.which("helm")
REQUIRE = os.environ.get("PICELI_COMPARISONS") == "require"
TEMPLATE = "templates/piceli-storage-probe.yaml"
PROBE = StorageProbe(image=helm_app.AGENT_IMAGE, timeout_seconds=120)
NAME = "agent-storage-probe"
PODS = f"/api/v1/namespaces/edge/pods/{NAME}"
CLAIMS = f"/api/v1/namespaces/edge/persistentvolumeclaims/{NAME}"


@pytest.fixture
def helm() -> str:
    if HELM is None:
        if REQUIRE:
            pytest.fail("PICELI_COMPARISONS=require but helm is not on PATH")
        pytest.skip("needs helm on PATH")
    return HELM


def _components() -> list[dict[str, Any]]:
    from piceli.k8s.cli.render import _render_pipeline

    def refuse(code: str, message: str) -> Any:
        raise AssertionError(f"{code}: {message}")

    return _render_pipeline(helm_app.pipeline, None, None, refuse)[1]


def _bundle() -> Any:
    return build_bundle(
        _components(),
        name="agent",
        version="1.0.0",
        namespace="agent",
        generators=helm_app.secrets.generators,
    )


def _chart(probe: StorageProbe | None = PROBE, *, scheduling: bool = False) -> Any:
    return build_helm(
        _bundle(),
        declaration=HelmChart(
            helm_app.pipeline, storage_probe=probe, scheduling=scheduling
        ),
    )


# ------------------------------------------------------------------ chart


def test_the_values_schema_and_readme_offer_the_probe() -> None:
    chart = _chart()
    values = yaml.safe_load(chart.files["values.yaml"])
    assert values["storageProbe"] == {"enabled": True, "timeoutSeconds": 120}
    schema = json.loads(chart.files["values.schema.json"])["properties"]
    node = schema["storageProbe"]
    assert node["additionalProperties"] is False
    assert node["properties"]["enabled"]["type"] == "boolean"
    assert node["properties"]["timeoutSeconds"]["type"] == "integer"
    assert node["properties"]["timeoutSeconds"]["minimum"] == 10
    readme = chart.files["README.md"].decode()
    assert "- `storageProbe.enabled` (true or false, default true)" in readme
    assert "- `storageProbe.timeoutSeconds` (default 120)" in readme
    assert chart.public()["storage_probe"] == {"timeout_seconds": 120, "size": "1Gi"}
    helpers = chart.files["templates/_helpers.tpl"].decode()
    assert 'define "agent.storageProbe"' in helpers
    assert "trunc 63" in helpers


def test_the_hooks_run_before_install_only() -> None:
    text = _chart().files[TEMPLATE].decode()
    assert text.count("helm.sh/hook: pre-install") == 7
    assert "pre-upgrade" not in text
    assert (
        text.count("helm.sh/hook-delete-policy: before-hook-creation,hook-succeeded")
        == 7
    )
    assert text.count("helm.sh/hook-output-log-policy: hook-failed") == 1
    assert "terminationMessagePolicy: FallbackToLogsOnError" in text
    assert "backoffLimit: 0" in text
    assert "($.Values.storageProbe | default dict).enabled" in text
    assert text.rstrip().endswith("{{- end }}")
    for weight in ("'-30'", "'-25'", "'-20'", "'-15'", "'-10'"):
        assert f"helm.sh/hook-weight: {weight}" in text


def test_without_a_probe_the_chart_has_none() -> None:
    chart = _chart(None)
    assert TEMPLATE not in chart.files
    assert "storageProbe" not in yaml.safe_load(chart.files["values.yaml"])
    assert "storage_probe" not in chart.public()


def test_the_hook_objects_and_the_probe_pod_pass_the_safety_gate() -> None:
    manifests = [dict(item.manifest) for item in _bundle().objects]
    objects = hook_objects(
        "agent", {}, PROBE, manifests, literal_names("agent", "agent", PROBE)
    )
    kinds = [item["kind"] for item in objects]
    assert kinds == [
        "ServiceAccount",
        "Role",
        "ClusterRole",
        "RoleBinding",
        "ClusterRoleBinding",
        "NetworkPolicy",
        "Job",
    ]
    pod = pod_manifest("agent", {}, None)
    assert check([*objects, pod]) == []
    by_kind = {item["kind"]: item for item in objects}
    assert by_kind["ClusterRole"]["metadata"]["name"] == "agent:agent:storage-probe"
    assert by_kind["ClusterRole"]["rules"] == [
        {
            "apiGroups": ["storage.k8s.io"],
            "resources": ["storageclasses"],
            "verbs": ["get", "list"],
        }
    ]
    job = by_kind["Job"]
    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["activeDeadlineSeconds"] == 180
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    # The app's StatefulSet runs as 10001: the probe does too.
    assert job["spec"]["template"]["spec"]["securityContext"]["runAsUser"] == 10001


def test_the_probe_pod_runs_where_the_claim_workload_runs() -> None:
    workload = {
        "nodeSelector": {"disk": "ssd"},
        "tolerations": [{"key": "storage", "operator": "Exists"}],
        "affinity": {
            "nodeAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": []},
            "podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": []},
        },
        "securityContext": {"runAsUser": 2000, "runAsGroup": 3000},
    }
    assert scheduling(workload) == {
        "nodeSelector": {"disk": "ssd"},
        "tolerations": [{"key": "storage", "operator": "Exists"}],
        "affinity": {
            "nodeAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": []}
        },
    }
    pod = pod_manifest("agent", {}, workload)["spec"]
    assert pod["nodeSelector"] == {"disk": "ssd"}
    assert (
        pod["securityContext"]["runAsUser"],
        pod["securityContext"]["runAsGroup"],
    ) == (
        2000,
        3000,
    )


def test_an_app_without_a_claim_is_refused() -> None:
    from piceli.app.render import rendered

    web = App(
        "web",
        pod_defaults=PodDefaults(
            security=Security.restricted(user=10001, read_only_root_filesystem=True)
        ),
    )
    web.deployment(
        "web",
        image=helm_app.AGENT_IMAGE,
        resources=Resources(
            cpu="10m", memory="16Mi", cpu_limit="100m", memory_limit="32Mi"
        ),
    )
    bundle = build_bundle(
        rendered(web.render("web"), {}),
        name="web",
        version="1.0.0",
        namespace="web",
        generators={},
    )
    with pytest.raises(BundleError, match="needs a claim") as error:
        build_helm(bundle, declaration=HelmChart(web, storage_probe=PROBE))
    assert error.value.code == "bundle-invalid"


@pytest.mark.parametrize(
    ("probe", "match"),
    [
        (StorageProbe(image="registry.example/acme/agent:1.0"), "pinned by digest"),
        (StorageProbe(image=helm_app.AGENT_IMAGE, timeout_seconds=5), "10 to 3600"),
        (StorageProbe(image=helm_app.AGENT_IMAGE, size="lots"), "a quantity"),
    ],
)
def test_a_bad_probe_is_refused(probe: StorageProbe, match: str) -> None:
    with pytest.raises(BundleError, match=match) as error:
        _chart(probe)
    assert error.value.code == "bundle-invalid"


# ------------------------------------------------------------------ helm


def _render(
    helm: str, tmp_path: Path, *args: str, chart: Any = None
) -> dict[tuple[str, str], Any]:
    out = tmp_path / "chart"
    if not out.exists():
        write_helm(out, chart or _chart())
    result = subprocess.run(
        [helm, "template", "agent", str(out), "-n", "edge",
         "--set", "apiToken=token-1", *args],
        capture_output=True, text=True, timeout=120,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    return {
        (item["kind"], item["metadata"]["name"]): item
        for item in yaml.safe_load_all(result.stdout)
        if item
    }


def _probe_env(objects: dict[tuple[str, str], Any]) -> dict[str, str]:
    job = objects[("Job", NAME)]
    container = job["spec"]["template"]["spec"]["containers"][0]
    return {entry["name"]: entry["value"] for entry in container["env"]}


def test_helm_renders_the_hooks(helm: str, tmp_path: Path) -> None:
    objects = _render(helm, tmp_path)
    for kind in ("ServiceAccount", "Role", "RoleBinding", "NetworkPolicy", "Job"):
        annotations = objects[(kind, NAME)]["metadata"]["annotations"]
        assert annotations["helm.sh/hook"] == "pre-install"
        assert (
            annotations["helm.sh/hook-delete-policy"]
            == "before-hook-creation,hook-succeeded"
        )
    for kind in ("ClusterRole", "ClusterRoleBinding"):
        assert (kind, "edge:agent:storage-probe") in objects
    binding = objects[("RoleBinding", NAME)]
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": NAME, "namespace": "edge"}
    ]
    job = objects[("Job", NAME)]
    assert job["spec"]["activeDeadlineSeconds"] == 180
    assert job["metadata"]["annotations"]["helm.sh/hook-output-log-policy"] == (
        "hook-failed"
    )
    env = _probe_env(objects)
    assert env["PROBE_NAME"] == NAME
    assert env["PROBE_TIMEOUT"] == "120"
    assert env["PROBE_CLASS"] == ""
    assert env["PROBE_IMAGE"] == helm_app.AGENT_IMAGE
    assert env["PROBE_PULL_SECRETS"] == "[]"
    assert json.loads(env["PROBE_CLAIM"])["spec"]["resources"] == {
        "requests": {"storage": "1Gi"}
    }
    assert json.loads(env["PROBE_POD"])["metadata"]["name"] == "@NAME@"


def test_helm_values_reach_the_probe(helm: str, tmp_path: Path) -> None:
    objects = _render(
        helm, tmp_path,
        "--set", "storageProbe.timeoutSeconds=30",
        "--set", "storageClass=fast",
        "--set", "image.registry=mirror.example/m",
        "--set", "image.pullSecrets={pull-1}",
    )  # fmt: skip
    env = _probe_env(objects)
    assert env["PROBE_TIMEOUT"] == "30"
    assert env["PROBE_CLASS"] == "fast"
    assert env["PROBE_IMAGE"].startswith("mirror.example/m/acme/agent@sha256:")
    assert json.loads(env["PROBE_PULL_SECRETS"]) == [{"name": "pull-1"}]
    assert objects[("Job", NAME)]["spec"]["activeDeadlineSeconds"] == 90


def test_helm_with_the_probe_disabled_renders_none(helm: str, tmp_path: Path) -> None:
    objects = _render(helm, tmp_path, "--set", "storageProbe.enabled=false")
    assert not [key for key in objects if "storage-probe" in key[1]]
    assert ("StatefulSet", "agent") in objects


# ---------------------------------------------------------------- the script

#: The fake API server's answers: ``answer(state, method, path, data,
#: headers) -> (status, body)`` from ``state/scenario.json``, each call
#: logged to ``state/calls.jsonl``.
FAKE_API = r"""
import json
import os


def answer(state, method, path, data, headers):
    with open(os.path.join(state, "scenario.json")) as handle:
        scenario = json.load(handle)
    with open(os.path.join(state, "calls.jsonl"), "a") as log:
        log.write(json.dumps({"method": method, "path": path, "data": data,
                              "headers": headers}) + "\n")
    code, body = 200, "{}"
    if method == "POST" and path.endswith("/persistentvolumeclaims"):
        code = scenario.get("claim_status", 201)
        if code >= 400:
            body = json.dumps({"kind": "Status", "status": "Failure",
                               "message": scenario["claim_error"], "code": code},
                              separators=(",", ":"))
        else:
            claim = json.loads(data)
            if scenario.get("claim_class"):
                claim["spec"]["storageClassName"] = scenario["claim_class"]
            body = json.dumps(claim, separators=(",", ":"))
    elif method == "POST":
        code, body = 201, data
    elif method == "GET" and "/pods/" in path:
        counter = os.path.join(state, "pod_gets")
        seen = int(open(counter).read()) if os.path.exists(counter) else 0
        with open(counter, "w") as handle:
            handle.write(str(seen + 1))
        phases = scenario["phases"]
        status = {"phase": phases[min(seen, len(phases) - 1)]}
        if scenario.get("waiting"):
            status["containerStatuses"] = [
                {"name": "probe", "state": {"waiting": {"reason": scenario["waiting"],
                                                        "message": "back-off"}}}
            ]
        body = json.dumps({"metadata": {"name": "x"}, "status": status},
                          separators=(",", ":"))
    elif method == "GET" and "/events" in path:
        body = scenario["events"]
    elif method == "GET" and path.endswith("/storageclasses"):
        body = scenario["classes"]
    return code, body
"""

FAKE_CURL = (
    FAKE_API
    + r"""
import sys

args = sys.argv[1:]
method, data, form, headers, url = "GET", None, "", [], ""
index = 0
while index < len(args):
    arg = args[index]
    if arg in ("--max-time", "--cacert", "-H", "-X", "--data", "-w"):
        value = args[index + 1]
        if arg == "-H":
            headers.append(value)
        elif arg == "-X":
            method = value
        elif arg == "--data":
            data = value
        elif arg == "-w":
            form = value
        index += 2
        continue
    if not arg.startswith("-"):
        url = arg
    index += 1
path = "/" + url.split("://", 1)[1].split("/", 1)[1]
code, body = answer(os.environ["FAKE_DIR"], method, path, data, headers)
sys.stdout.write(body + form.replace("\\n", "\n").replace("%{http_code}", str(code)))
"""
)


def _event(reason: str, message: str, kind: str, **times: str | None) -> dict[str, Any]:
    """An event in the API server's field order (metadata first)."""
    return {
        "metadata": {
            "name": f"{NAME}.{reason.lower()}",
            "namespace": "edge",
            "uid": f"uid-{reason}",
            "resourceVersion": "1",
            "creationTimestamp": "2026-11-27T10:00:00Z",
        },
        "involvedObject": {
            "kind": "Pod",
            "namespace": "edge",
            "name": NAME,
            "uid": "uid-pod",
            "apiVersion": "v1",
        },
        "reason": reason,
        "message": message,
        "source": {"component": "attachdetach-controller"},
        "firstTimestamp": times.get("last"),
        "lastTimestamp": times.get("last"),
        "count": 1,
        "type": kind,
        "eventTime": times.get("event_time"),
        "reportingComponent": "",
        "reportingInstance": "",
    }


def _list(kind: str, items: list[dict[str, Any]]) -> str:
    return json.dumps(
        {"kind": kind, "apiVersion": "v1", "metadata": {"resourceVersion": "9"},
         "items": items},
        separators=(",", ":"),
    )  # fmt: skip


def _class(name: str, *, default: bool = False) -> dict[str, Any]:
    metadata: dict[str, Any] = {"name": name, "uid": f"uid-{name}"}
    if default:
        metadata["annotations"] = {
            "storageclass.kubernetes.io/is-default-class": "true"
        }
    return {"metadata": metadata, "provisioner": "disk.csi.example.org"}


ATTACH = (
    'AttachVolume.Attach failed for volume "pvc-1" : rpc error: code = '
    "InvalidArgument desc = the disk type is not supported by this machine type"
)
SCHEDULING = "0/3 nodes are available: 3 node(s) had volume node affinity conflict."


@pytest.fixture(params=["sh", "python"])
def runner(request: pytest.FixtureRequest) -> str:
    """Each script test runs the sh script and its Python twin (0.97.0)."""
    return str(request.param)


@contextlib.contextmanager
def _api_server(state: Path) -> Iterator[str]:
    """A local HTTP server answering like the fake curl; yields its URL."""
    namespace: dict[str, Any] = {}
    exec(FAKE_API, namespace)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def _any(self) -> None:
            size = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(size).decode() if size else None
            headers = [f"{key}: {value}" for key, value in self.headers.items()]
            code, body = namespace["answer"](
                str(state), self.command, self.path, data, headers
            )
            raw = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = do_DELETE = _any

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _run(
    tmp_path: Path,
    scenario: dict[str, Any],
    *,
    storage_class: str,
    timeout: str,
    runner: str = "sh",
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, Any]]]:
    fake, bin_dir, account = tmp_path / "fake", tmp_path / "bin", tmp_path / "sa"
    for directory in (fake, bin_dir, account):
        directory.mkdir()
    (fake / "scenario.json").write_text(json.dumps(scenario))
    curl = bin_dir / "curl"
    curl.write_text(f"#!{sys.executable} -I\n{FAKE_CURL}")
    curl.chmod(curl.stat().st_mode | stat.S_IXUSR)
    (account / "token").write_text("token-1")
    (account / "ca.crt").write_text("ca")
    (account / "namespace").write_text("edge")
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DIR": str(fake),
        "SERVICE_ACCOUNT_DIR": str(account),
        "KUBERNETES_SERVICE_HOST": "10.96.0.1",
        "KUBERNETES_SERVICE_PORT": "443",
        "PROBE_NAME": NAME,
        "PROBE_IMAGE": helm_app.AGENT_IMAGE,
        "PROBE_CLASS": storage_class,
        "PROBE_TIMEOUT": timeout,
        "PROBE_POLL": "0",
        "PROBE_PULL_SECRETS": '[{"name":"pull-1"}]',
        "PROBE_CLAIM": as_json(claim_manifest("agent", {}, "1Gi")),
        "PROBE_POD": as_json(pod_manifest("agent", {}, None)),
    }
    if runner == "sh":
        result = subprocess.run(
            ["sh", "-c", SCRIPT], env=env, capture_output=True, text=True, timeout=60
        )
    else:
        with _api_server(fake) as url:
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-c", PYTHON_SCRIPT],
                env={**env, "PROBE_API": url},
                capture_output=True, text=True, timeout=60,
            )  # fmt: skip
    calls = [
        json.loads(line) for line in (fake / "calls.jsonl").read_text().splitlines()
    ]
    return result, calls


def _cleaned_up(calls: list[dict[str, Any]]) -> None:
    """The run begins and ends by deleting the pod and the claim."""
    deletes = [(PODS + "?gracePeriodSeconds=0", "DELETE"), (CLAIMS, "DELETE")]
    assert [(call["path"], call["method"]) for call in calls[:2]] == deletes
    assert [(call["path"], call["method"]) for call in calls[-2:]] == deletes
    assert all("Authorization: Bearer token-1" in call["headers"] for call in calls)


def _posted(calls: list[dict[str, Any]], what: str) -> dict[str, Any]:
    (call,) = [
        call
        for call in calls
        if call["method"] == "POST" and call["path"].endswith(what)
    ]
    return dict(json.loads(call["data"]))


@pytest.mark.parametrize(
    ("storage_class", "claim_class", "subject"),
    [
        ("standard", None, 'storage class "standard"'),
        ("", "standard", 'the default storage class "standard"'),
    ],
)
def test_the_script_succeeds_when_the_pod_runs(
    tmp_path: Path,
    storage_class: str,
    claim_class: str | None,
    subject: str,
    runner: str,
) -> None:
    scenario = {"phases": ["Pending", "Running"], "claim_class": claim_class}
    result, calls = _run(
        tmp_path, scenario, storage_class=storage_class, timeout="60", runner=runner
    )
    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(f"storage probe: {subject} attached on these nodes in ")
    assert lines[0].endswith("s")
    claim = _posted(calls, "/persistentvolumeclaims")
    assert claim["metadata"]["name"] == NAME
    assert claim["spec"]["resources"] == {"requests": {"storage": "1Gi"}}
    if storage_class:
        assert claim["spec"]["storageClassName"] == "standard"
    else:
        assert "storageClassName" not in claim["spec"]
    pod = _posted(calls, "/pods")
    assert pod["metadata"]["name"] == NAME
    assert pod["spec"]["containers"][0]["image"] == helm_app.AGENT_IMAGE
    assert pod["spec"]["imagePullSecrets"] == [{"name": "pull-1"}]
    assert pod["spec"]["volumes"][0]["persistentVolumeClaim"] == {"claimName": NAME}
    _cleaned_up(calls)


def test_the_script_names_the_attach_failure_and_other_classes(
    tmp_path: Path, runner: str
) -> None:
    events = [
        _event("Scheduled", "assigned to node-1", "Normal", last="2026-11-27T10:00:01Z"),
        _event("FailedAttachVolume", ATTACH, "Warning", last="2026-11-27T10:01:30Z"),
        _event("FailedMount", "timed out waiting", "Warning", last="2026-11-27T10:00:05Z"),
        _event("Pulled", "a later normal event", "Normal", last="2026-11-27T10:02:00Z"),
    ]  # fmt: skip
    scenario = {
        "phases": ["Pending"],
        "events": _list("EventList", events),
        "classes": _list(
            "StorageClassList",
            [_class("standard"), _class("standard-rwo", default=True),
             _class("premium-rwo")],
        ),
    }  # fmt: skip
    result, calls = _run(
        tmp_path, scenario, storage_class="standard", timeout="0", runner=runner
    )
    assert result.returncode == 1
    assert result.stdout == (
        'storage class "standard" cannot attach on these nodes (FailedAttachVolume: '
        f"{ATTACH}): set storageClass to one of: standard-rwo, premium-rwo\n"
    )
    (events_call,) = [call for call in calls if "/events" in call["path"]]
    assert events_call["path"] == (
        f"/api/v1/namespaces/edge/events?fieldSelector=involvedObject.name%3D{NAME}"
    )
    _cleaned_up(calls)


def test_the_script_names_the_default_class(tmp_path: Path, runner: str) -> None:
    events = [
        _event("FailedScheduling", SCHEDULING, "Warning",
               event_time="2026-11-27T10:00:03.123456Z"),
    ]  # fmt: skip
    scenario = {
        "phases": ["Pending"],
        "events": _list("EventList", events),
        "classes": _list(
            "StorageClassList",
            [_class("standard", default=True), _class("premium-rwo")],
        ),
    }
    result, calls = _run(
        tmp_path, scenario, storage_class="", timeout="0", runner=runner
    )
    assert result.returncode == 1
    assert result.stdout == (
        'the default storage class "standard" cannot attach on these nodes '
        f"(FailedScheduling: {SCHEDULING}): set storageClass to one of: premium-rwo\n"
    )
    assert "storageClassName" not in _posted(calls, "/persistentvolumeclaims")["spec"]
    _cleaned_up(calls)


def test_the_script_without_a_class_to_suggest(tmp_path: Path, runner: str) -> None:
    events = [
        _event("FailedScheduling", SCHEDULING, "Warning", last="2026-11-27T10:00:03Z")
    ]
    scenario = {
        "phases": ["Pending"],
        "events": _list("EventList", events),
        "classes": _list("StorageClassList", []),
    }
    result, calls = _run(
        tmp_path, scenario, storage_class="", timeout="0", runner=runner
    )
    assert result.returncode == 1
    assert result.stdout == (
        "the default storage class cannot attach on these nodes "
        f"(FailedScheduling: {SCHEDULING}): ask the cluster's administrator for "
        "a class these nodes can attach\n"
    )
    _cleaned_up(calls)


def test_the_script_reports_a_refused_claim(tmp_path: Path, runner: str) -> None:
    quota = (
        f'persistentvolumeclaims "{NAME}" is forbidden: exceeded quota: storage, '
        "requested: requests.storage=1Gi, limited: requests.storage=10Gi"
    )
    scenario = {"phases": ["Pending"], "claim_status": 403, "claim_error": quota}
    result, calls = _run(
        tmp_path, scenario, storage_class="standard", timeout="0", runner=runner
    )
    assert result.returncode == 1
    assert result.stdout == (
        f"storage probe: the cluster refused the claim (HTTP 403: {quota})\n"
    )
    assert not [call for call in calls if call["path"].endswith("/pods")]
    _cleaned_up(calls)


def test_a_pod_past_its_mount_counts_as_attached(tmp_path: Path, runner: str) -> None:
    scenario = {"phases": ["Pending"], "waiting": "ImagePullBackOff"}
    result, calls = _run(
        tmp_path, scenario, storage_class="standard", timeout="60", runner=runner
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.endswith(
        "s (the probe container did not start: ImagePullBackOff)\n"
    )
    _cleaned_up(calls)


# ------------------------------------------------------------ 0.97.0


def test_a_probe_without_an_image_runs_piceli_s_own() -> None:
    """ops#115: no app image needs sh and curl; the default is pinned."""
    assert DEFAULT_IMAGE.startswith("ghcr.io/pynenc/piceli-controller@sha256:")
    chart = _chart(StorageProbe())
    text = chart.files[TEMPLATE].decode()
    assert DEFAULT_IMAGE.split("@", 1)[1] in text
    assert "python3" in text and "urllib.request" in text
    assert "curl" not in text


def test_helm_renders_the_default_probe_image_and_its_mirror(
    helm: str, tmp_path: Path
) -> None:
    objects = _render(helm, tmp_path, chart=_chart(StorageProbe()))
    job = objects[("Job", NAME)]["spec"]["template"]["spec"]["containers"][0]
    assert job["image"] == DEFAULT_IMAGE
    assert job["command"][:4] == ["python3", "-I", "-B", "-c"]
    assert _probe_env(objects)["PROBE_IMAGE"] == DEFAULT_IMAGE
    mirrored = _render(helm, tmp_path, "--set", "image.registry=mirror.example/m")
    image = mirrored[("Job", NAME)]["spec"]["template"]["spec"]["containers"][0]
    assert (
        image["image"]
        == "mirror.example/m/pynenc/piceli-controller@"
        + (DEFAULT_IMAGE.split("@", 1)[1])
    )


def test_scheduling_values_reach_the_workloads_and_the_probe(
    helm: str, tmp_path: Path
) -> None:
    """ops#115: ``--set nodeSelector.pool=x`` puts the app and its probe on
    one node pool."""
    chart = _chart(StorageProbe(), scheduling=True)
    values = yaml.safe_load(chart.files["values.yaml"])
    assert (values["nodeSelector"], values["tolerations"], values["affinity"]) == (
        {}, [], {},
    )  # fmt: skip
    schema = json.loads(chart.files["values.schema.json"])["properties"]
    assert {"nodeSelector", "tolerations", "affinity"} <= set(schema)
    assert "`nodeSelector`" in chart.files["README.md"].decode()
    plain = _render(helm, tmp_path, chart=chart)
    for key in ("nodeSelector", "tolerations", "affinity"):
        assert key not in plain[("StatefulSet", "agent")]["spec"]["template"]["spec"]
    affinity = {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {"matchExpressions": [
                        {"key": "pool", "operator": "In", "values": ["x"]}
                    ]}
                ]
            }
        },
        "podAntiAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": []},
    }  # fmt: skip
    values_file = tmp_path / "scheduling.yaml"
    values_file.write_text(
        yaml.safe_dump(
            {
                "nodeSelector": {"pool": "x"},
                "tolerations": [{"key": "pool", "operator": "Exists"}],
                "affinity": affinity,
            }
        )
    )
    objects = _render(helm, tmp_path, "-f", str(values_file))
    pods = [
        objects[("StatefulSet", "agent")]["spec"]["template"]["spec"],
        objects[("Job", NAME)]["spec"]["template"]["spec"],
    ]
    for pod in pods:
        assert pod["nodeSelector"] == {"pool": "x"}
        assert pod["tolerations"] == [{"key": "pool", "operator": "Exists"}]
    assert pods[0]["affinity"] == affinity
    probe_pod = json.loads(_probe_env(objects)["PROBE_POD"])["spec"]
    assert probe_pod["nodeSelector"] == {"pool": "x"}
    assert probe_pod["tolerations"] == [{"key": "pool", "operator": "Exists"}]
    assert probe_pod["affinity"] == {"nodeAffinity": affinity["nodeAffinity"]}
    assert probe_pod["imagePullSecrets"] == "@PULL@"  # the script fills it


def test_scheduling_values_merge_with_a_workload_s_own(
    helm: str, tmp_path: Path
) -> None:
    """A workload's own nodeSelector stays unless the value names its key;
    its own tolerations stay and the value's are added."""
    bundle = _bundle()
    (statefulset,) = [o for o in bundle.objects if o.kind == "StatefulSet"]
    pod = statefulset.manifest["spec"]["template"]["spec"]
    pod["nodeSelector"] = {"disk": "ssd", "pool": "own"}
    pod["tolerations"] = [{"key": "own", "operator": "Exists"}]
    chart = build_helm(
        bundle,
        declaration=HelmChart(
            helm_app.pipeline, storage_probe=StorageProbe(), scheduling=True
        ),
    )
    plain = _render(helm, tmp_path, chart=chart)
    spec = plain[("StatefulSet", "agent")]["spec"]["template"]["spec"]
    assert spec["nodeSelector"] == {"disk": "ssd", "pool": "own"}
    assert spec["tolerations"] == [{"key": "own", "operator": "Exists"}]
    objects = _render(
        helm, tmp_path,
        "--set", "nodeSelector.pool=x",
        "--set", "tolerations[0].key=pool,tolerations[0].operator=Exists",
    )  # fmt: skip
    spec = objects[("StatefulSet", "agent")]["spec"]["template"]["spec"]
    assert spec["nodeSelector"] == {"disk": "ssd", "pool": "x"}
    assert spec["tolerations"] == [
        {"key": "own", "operator": "Exists"},
        {"key": "pool", "operator": "Exists"},
    ]
    probe = json.loads(_probe_env(objects)["PROBE_POD"])["spec"]
    assert probe["nodeSelector"] == {"disk": "ssd", "pool": "x"}
    assert probe["tolerations"] == spec["tolerations"]


def test_scheduling_refuses_a_value_the_chart_already_names() -> None:
    from piceli.bundle.helm import HelmToggle

    toggle = HelmToggle("x", patches={})
    with pytest.raises(BundleError, match="already names"):
        build_helm(
            _bundle(),
            declaration=HelmChart(
                helm_app.pipeline, scheduling=True, toggles={"affinity": toggle}
            ),
        )
