"""The pure part of pre-rollout checks: the check Job's pod and log scrubbing."""

from __future__ import annotations

from typing import Any

from piceli import (
    App,
    ClaimTemplate,
    ConfigVolume,
    ExistingClaim,
    Probe,
    SecretVolume,
    Security,
)
from piceli.pipeline.prerollout import (
    check_job,
    claim_names,
    job_digest,
    job_name,
    references,
    scrub,
)
from piceli.pipeline.secrets import placeholder


def workload() -> dict[str, Any]:
    app = App("shop")
    creds = app.secret("creds", {"pw": placeholder("pw")})
    app.stateful_set(
        "db",
        image="example/db:2",
        env={"PW": creds.key("pw"), "MODE": "prod"},
        ready=Probe.http("/healthz", 8080),
        ports=[8080],
        volumes={
            "/etc/db": SecretVolume("db-keys"),
            "/etc/conf": ConfigVolume("db-conf"),
            "/var/lib/db": ClaimTemplate("data", size="1Gi"),
            "/mnt/ext": ExistingClaim("shared"),
        },
        security=Security(run_as_user=10001, read_only_root_filesystem=True),
        service_account="db-sa",
        labels={"tier": "data"},
        replicas=2,
    )
    for component in app.render("shop").components:
        for resource in component.resources:
            if resource.ref.kind == "StatefulSet":
                return resource.manifest
    raise AssertionError


def build(kind: str = "config", **more: Any) -> dict[str, Any]:
    return check_job(
        workload(),
        app="shop",
        namespace="shop",
        kind=kind,
        command=["db", "check"],
        timeout_seconds=60,
        run="20260101T000000Z-abcd1234",
        **more,
    )


def test_the_pod_carries_the_real_settings_and_nothing_that_serves_traffic() -> None:
    job = build()
    pod = job["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    assert container["image"] == "example/db:2"
    assert container["command"] == ["db", "check"]
    assert pod["securityContext"]["runAsUser"] == 10001
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert pod["serviceAccountName"] == "db-sa"
    env = {item["name"]: item for item in container["env"]}
    assert env["PW"]["valueFrom"]["secretKeyRef"]["name"] == "creds"
    for dropped in ("readinessProbe", "livenessProbe", "ports", "args"):
        assert dropped not in container
    labels = job["spec"]["template"]["metadata"]["labels"]
    assert "tier" not in labels and "app.kubernetes.io/name" not in labels
    assert pod["restartPolicy"] == "Never"
    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["activeDeadlineSeconds"] == 60
    assert len(job["metadata"]["name"]) <= 52


def test_the_config_check_never_sees_a_claim() -> None:
    pod = build()["spec"]["template"]["spec"]
    volumes = {item["name"]: item for item in pod["volumes"]}
    assert volumes["db-keys"]["secret"]["secretName"] == "db-keys"
    assert volumes["db-conf"]["configMap"]["name"] == "db-conf"
    assert volumes["data"] == {"name": "data", "emptyDir": {}}
    assert volumes["shared"] == {"name": "shared", "emptyDir": {}}
    assert "persistentVolumeClaim" not in str(pod)


def test_the_upgrade_check_mounts_only_the_named_claims_read_only() -> None:
    pod = build(
        "upgrade",
        ordinal=1,
        claims={"/var/lib/db": "data-db-1"},
        node="node-b",
    )["spec"]["template"]["spec"]
    volumes = {item["name"]: item for item in pod["volumes"]}
    assert volumes["data"]["persistentVolumeClaim"] == {
        "claimName": "data-db-1",
        "readOnly": True,
    }
    assert volumes["shared"] == {"name": "shared", "emptyDir": {}}
    mounts = {m["mountPath"]: m for m in pod["containers"][0]["volumeMounts"]}
    assert mounts["/var/lib/db"]["readOnly"] is True
    match = pod["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]["nodeSelectorTerms"][0]["matchFields"][0]
    assert match == {"key": "metadata.name", "operator": "In", "values": ["node-b"]}


def test_objects_the_release_creates_become_optional_for_the_check() -> None:
    pod = build(optional=[("Secret", "creds"), ("Secret", "db-keys")])["spec"][
        "template"
    ]["spec"]
    env = {item["name"]: item for item in pod["containers"][0]["env"]}
    assert env["PW"]["valueFrom"]["secretKeyRef"]["optional"] is True
    volumes = {item["name"]: item for item in pod["volumes"]}
    assert volumes["db-keys"]["secret"]["optional"] is True
    assert "optional" not in volumes["db-conf"]["configMap"]


def test_claim_names_follow_the_statefulset_ordinals() -> None:
    manifest = workload()
    assert claim_names(manifest, 1) == {
        "/var/lib/db": "data-db-1",
        "/mnt/ext": "shared",
    }
    assert claim_names(manifest) == {"/mnt/ext": "shared"}
    assert {(r.kind, r.name) for r in references(manifest)} == {
        ("Secret", "creds"),
        ("Secret", "db-keys"),
        ("ConfigMap", "db-conf"),
    }


def test_names_are_bounded_and_the_digest_ignores_the_run() -> None:
    assert len(job_name("a" * 63, "upgrade", 12, "20260101T000000Z-abcd1234")) <= 52
    first = build()
    second = check_job(
        workload(),
        app="shop",
        namespace="shop",
        kind="config",
        command=["db", "check"],
        timeout_seconds=60,
        run="another-run-id",
    )
    assert first["metadata"]["name"] != second["metadata"]["name"]
    # Runs of the same day differ: the unique tail of the run id names the Job.
    day = check_job(
        workload(),
        app="shop",
        namespace="shop",
        kind="config",
        command=["db", "check"],
        timeout_seconds=60,
        run="20260101T000000Z-ffff0000",
    )
    assert first["metadata"]["name"] != day["metadata"]["name"]
    assert job_digest(first) == job_digest(second)


def test_scrub_keeps_a_bounded_tail_without_secret_like_text() -> None:
    text = "\n".join(f"line {i}" for i in range(100))
    tail = scrub(text)
    assert tail.splitlines()[0] == "line 70" and len(tail.splitlines()) == 30
    dirty = scrub(
        "Authorization: Bearer abc.def\napi_key=zzzz1234\n"
        "token ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\nhello pw1234",
        values=["pw1234", "ab"],
    )
    assert "abc.def" not in dirty and "zzzz1234" not in dirty
    assert "ghp_" not in dirty and "pw1234" not in dirty
    assert "hello" in dirty
    assert len(scrub("x " * 5000)) <= 4000


def test_staged_copies_replace_references_and_keep_only_data() -> None:
    from piceli.pipeline.prerollout import staged_copy, staged_name, staged_values

    pod = build(
        renamed={("Secret", "creds"): "db-chks0-abcd1234"},
        optional=[("Secret", "creds"), ("ConfigMap", "db-conf")],
    )["spec"]["template"]["spec"]
    env = {item["name"]: item for item in pod["containers"][0]["env"]}
    # A staged copy wins over optional: the check reads the real value.
    assert env["PW"]["valueFrom"]["secretKeyRef"] == {
        "name": "db-chks0-abcd1234",
        "key": "pw",
    }
    volumes = {item["name"]: item for item in pod["volumes"]}
    assert volumes["db-conf"]["configMap"]["optional"] is True
    assert staged_name("db", "20260101T000000Z-abcd1234", "Secret", 0) == (
        "db-chks0-abcd1234"
    )
    copy = staged_copy(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": "creds",
                "labels": {"app.kubernetes.io/name": "shop"},
                "annotations": {"piceli.io/owner": "shop"},
            },
            "type": "kubernetes.io/basic-auth",
            "data": {"pw": "c2VjcmV0LXZhbHVl"},
        },
        name="db-chks0-abcd1234",
        namespace="shop",
        app="shop",
        run="20260101T000000Z-abcd1234",
    )
    assert copy == {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": "db-chks0-abcd1234",
            "namespace": "shop",
            "labels": {
                "piceli.io/pre-rollout": "staged",
                "piceli.io/pre-rollout-app": "shop",
                "piceli.io/pre-rollout-run": "abcd1234",
            },
        },
        "type": "kubernetes.io/basic-auth",
        "data": {"pw": "c2VjcmV0LXZhbHVl"},
    }
    assert staged_values(copy) == ["secret-value"]
