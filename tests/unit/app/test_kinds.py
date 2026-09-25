"""Typed App kinds: StatefulSet, DaemonSet, Job, CronJob, autoscaler, PDB,
Ingress and Gateway API HTTPRoute (render and declaration-time validation)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from piceli import (
    App,
    ClaimTemplate,
    ExistingClaim,
    GatewayRef,
    PodDefaults,
    Resources,
    Route,
    Rule,
    Security,
    ServicePort,
)
from piceli.k8s.ops.secret_versions import SecretVersionRef
from piceli.k8s.release_spec import NodeRef

IMAGE = "registry.example/api@sha256:" + "1" * 64
NODES = {"primary": NodeRef("node-a", "uid-a")}


def _manifests(app: App, **kwargs) -> dict[tuple[str, str], dict]:
    return {
        (resource.ref.kind, resource.ref.name): resource.manifest
        for component in app.render("shop", **kwargs).components
        for resource in component.resources
    }


def _components(app: App, **kwargs) -> dict[str, tuple[set[str], tuple[str, ...]]]:
    return {
        component.name: (
            {f"{r.ref.kind}/{r.ref.name}" for r in component.resources},
            component.dependencies,
        )
        for component in app.render("shop", **kwargs).components
    }


def _pod(manifest: dict) -> dict:
    spec = manifest["spec"]
    if manifest["kind"] == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    return spec["template"]["spec"]


# ------------------------------------------------------------------ render


def test_stateful_set_renders_claims_headless_service_and_policies():
    app = App("shop")
    data = ClaimTemplate("data", size="1Gi", storage_class="standard")
    db = app.stateful_set(
        "db",
        image=IMAGE,
        ports=[5432],
        replicas=3,
        volumes={"/var/lib/db": data, "/backup": ExistingClaim("backups")},
        pod_management="Parallel",
        update_strategy="RollingUpdate",
    )
    manifests = _manifests(app)
    sts = manifests[("StatefulSet", "db")]
    assert sts["apiVersion"] == "apps/v1"
    spec = sts["spec"]
    assert spec["replicas"] == 3
    assert spec["serviceName"] == "db"
    assert spec["podManagementPolicy"] == "Parallel"
    assert spec["updateStrategy"] == {"type": "RollingUpdate"}
    assert spec["selector"] == {"matchLabels": {"app.kubernetes.io/name": "db"}}
    assert spec["volumeClaimTemplates"] == [
        {
            "metadata": {"name": "data"},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": "standard",
                "resources": {"requests": {"storage": "1Gi"}},
            },
        }
    ]
    # Claims outlive the StatefulSet: deletion and scale-down retain them.
    assert spec["persistentVolumeClaimRetentionPolicy"] == {
        "whenDeleted": "Retain",
        "whenScaled": "Retain",
    }
    pod = _pod(sts)
    # A claim template is mounted but is not a pod volume.
    assert pod["volumes"] == [
        {"name": "backups", "persistentVolumeClaim": {"claimName": "backups"}}
    ]
    assert pod["containers"][0]["volumeMounts"] == [
        {"name": "data", "mountPath": "/var/lib/db"},
        {"name": "backups", "mountPath": "/backup"},
    ]
    service = manifests[("Service", "db")]
    assert service["spec"] == {
        "clusterIP": "None",
        "selector": db.selector_labels,
        "ports": [{"port": 5432, "targetPort": 5432, "protocol": "TCP"}],
    }
    # The governing Service joins the StatefulSet's component.
    assert _components(app)["db"][0] == {"StatefulSet/db", "Service/db"}


def test_stateful_set_service_options():
    app = App("shop")
    app.stateful_set("db", image=IMAGE, ports=[5432, 9187], service_name="db-peers")
    manifests = _manifests(app)
    assert manifests[("StatefulSet", "db")]["spec"]["serviceName"] == "db-peers"
    ports = manifests[("Service", "db-peers")]["spec"]["ports"]
    assert [port["name"] for port in ports] == ["port-5432", "port-9187"]

    plain = App("shop")
    plain.stateful_set("cache", image=IMAGE, headless=False)
    manifests = _manifests(plain)
    assert "serviceName" not in manifests[("StatefulSet", "cache")]["spec"]
    assert not any(kind == "Service" for kind, _ in manifests)

    # A headless Service without ports is valid.
    bare = App("shop")
    bare.stateful_set("worker", image=IMAGE)
    assert "ports" not in _manifests(bare)[("Service", "worker")]["spec"]


def test_daemon_set_job_and_cron_job_render():
    app = App("shop")
    app.daemon_set(
        "agent", image=IMAGE, update_strategy="OnDelete", node_selector={"role": "edge"}
    )
    app.job(
        "migrate",
        image=IMAGE,
        command=["migrate"],
        backoff_limit=2,
        active_deadline_seconds=600,
        ttl_seconds_after_finished=3600,
    )
    app.cron_job(
        "report",
        schedule="0 3 * * *",
        time_zone="Etc/UTC",
        concurrency="Forbid",
        image=IMAGE,
        restart_policy="OnFailure",
        successful_jobs_history=1,
    )
    manifests = _manifests(app)
    agent = manifests[("DaemonSet", "agent")]
    assert agent["spec"]["updateStrategy"] == {"type": "OnDelete"}
    assert "replicas" not in agent["spec"]
    assert _pod(agent)["nodeSelector"] == {"role": "edge"}

    job = manifests[("Job", "migrate")]
    assert job["apiVersion"] == "batch/v1"
    assert "selector" not in job["spec"]  # Kubernetes chooses it
    assert job["spec"]["backoffLimit"] == 2
    assert job["spec"]["activeDeadlineSeconds"] == 600
    assert job["spec"]["ttlSecondsAfterFinished"] == 3600
    assert _pod(job)["restartPolicy"] == "Never"
    assert job["spec"]["template"]["metadata"]["labels"] == {
        "app.kubernetes.io/part-of": "shop",
        "app.kubernetes.io/name": "migrate",
    }

    cron = manifests[("CronJob", "report")]
    assert cron["spec"]["schedule"] == "0 3 * * *"
    assert cron["spec"]["timeZone"] == "Etc/UTC"
    assert cron["spec"]["concurrencyPolicy"] == "Forbid"
    assert cron["spec"]["successfulJobsHistoryLimit"] == 1
    assert _pod(cron)["restartPolicy"] == "OnFailure"


def test_autoscaler_owns_replicas():
    app = App("shop")
    api = app.deployment("api", image=IMAGE, resources=Resources(cpu="100m"))
    db = app.stateful_set(
        "db", image=IMAGE, resources=Resources(cpu="100m", memory="64Mi")
    )
    app.autoscaler(api, min_replicas=2, max_replicas=10, cpu=70)
    app.autoscaler(
        db, max_replicas=3, cpu=80, memory=75, scale_down_stabilization_seconds=60
    )
    manifests = _manifests(app)
    # The targeted workloads render no replicas: the HPA sets them.
    assert "replicas" not in manifests[("Deployment", "api")]["spec"]
    assert "replicas" not in manifests[("StatefulSet", "db")]["spec"]
    hpa = manifests[("HorizontalPodAutoscaler", "api")]
    assert hpa["apiVersion"] == "autoscaling/v2"
    assert hpa["spec"] == {
        "scaleTargetRef": {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "name": "api",
        },
        "minReplicas": 2,
        "maxReplicas": 10,
        "metrics": [
            {
                "type": "Resource",
                "resource": {
                    "name": "cpu",
                    "target": {"type": "Utilization", "averageUtilization": 70},
                },
            }
        ],
    }
    db_hpa = manifests[("HorizontalPodAutoscaler", "db")]["spec"]
    assert [m["resource"]["name"] for m in db_hpa["metrics"]] == ["cpu", "memory"]
    assert db_hpa["behavior"] == {"scaleDown": {"stabilizationWindowSeconds": 60}}
    assert _components(app)["api"][0] == {
        "Deployment/api",
        "HorizontalPodAutoscaler/api",
    }


def test_disruption_budget_ingress_and_http_route_render():
    app = App("shop")
    web = app.deployment("web", image=IMAGE, ports=[3000])
    api = app.deployment("api", image=IMAGE, ports=[8080])
    web_service = app.service(web, port=80, target_port=3000)
    api_service = app.service(
        api,
        ports=[
            ServicePort(port=8080, name="http"),
            ServicePort(port=9090, name="metrics"),
        ],
    )
    app.disruption_budget(web, max_unavailable=1)
    app.disruption_budget(api, min_available="50%", name="api-budget")
    app.ingress(
        "shop",
        hosts=["shop.example.com", "*.shop.example.com"],
        class_name="nginx",
        tls_secret="shop-tls",
        routes=[
            Route(web_service, "/"),
            Route(api_service, "/api", port="http"),
            Route("legacy", "/legacy", port="web", match="Exact"),
        ],
    )
    app.http_route(
        "shop",
        gateway=[GatewayRef("public", namespace="gateways", section="https")],
        hosts=["shop.example.com"],
        routes=[Route(web_service), Route(api_service, "/api", port=8080)],
    )
    manifests = _manifests(app)
    assert manifests[("PodDisruptionBudget", "web")]["spec"] == {
        "maxUnavailable": 1,
        "selector": {"matchLabels": {"app.kubernetes.io/name": "web"}},
    }
    assert manifests[("PodDisruptionBudget", "api-budget")]["spec"]["minAvailable"] == (
        "50%"
    )
    ingress = manifests[("Ingress", "shop")]
    assert ingress["apiVersion"] == "networking.k8s.io/v1"
    spec = ingress["spec"]
    assert spec["ingressClassName"] == "nginx"
    assert spec["tls"] == [
        {"hosts": ["shop.example.com", "*.shop.example.com"], "secretName": "shop-tls"}
    ]
    assert [rule["host"] for rule in spec["rules"]] == [
        "shop.example.com",
        "*.shop.example.com",
    ]
    assert spec["rules"][0]["http"]["paths"] == [
        {
            "path": "/",
            "pathType": "Prefix",
            "backend": {"service": {"name": "web", "port": {"number": 80}}},
        },
        {
            "path": "/api",
            "pathType": "Prefix",
            "backend": {"service": {"name": "api", "port": {"number": 8080}}},
        },
        {
            "path": "/legacy",
            "pathType": "Exact",
            "backend": {"service": {"name": "legacy", "port": {"name": "web"}}},
        },
    ]
    route = manifests[("HTTPRoute", "shop")]
    assert route["apiVersion"] == "gateway.networking.k8s.io/v1"
    assert route["spec"] == {
        "parentRefs": [
            {
                "group": "gateway.networking.k8s.io",
                "kind": "Gateway",
                "name": "public",
                "namespace": "gateways",
                "sectionName": "https",
            }
        ],
        "hostnames": ["shop.example.com"],
        "rules": [
            {
                "matches": [{"path": {"type": "PathPrefix", "value": "/"}}],
                "backendRefs": [{"name": "web", "port": 80}],
            },
            {
                "matches": [{"path": {"type": "PathPrefix", "value": "/api"}}],
                "backendRefs": [{"name": "api", "port": 8080}],
            },
        ],
    }
    # Routes join the first Service's component.
    components = _components(app)
    assert "Ingress/shop" in components["web"][0]
    assert "HTTPRoute/shop" in components["web"][0]


def test_ingress_without_hosts_matches_any_host():
    app = App("shop")
    web = app.deployment("web", image=IMAGE, ports=[80])
    app.ingress("web", routes=[Route(app.service(web, port=80))])
    (rule,) = _manifests(app)[("Ingress", "web")]["spec"]["rules"]
    assert "host" not in rule


# ---------------------------------------------------- shared pod settings


def test_pod_defaults_and_service_account_apply_to_every_pod_kind():
    app = App(
        "shop",
        pod_defaults=PodDefaults(
            security=Security.restricted(user=10001, fs_group=10001),
            node_selector={"kubernetes.io/arch": "amd64"},
            termination_grace_seconds=30,
            automount_token=False,
        ),
    )
    watcher = app.service_account(
        "watcher", rules=[Rule(resources=["pods"], verbs=["get"])]
    )
    settings = app.config("settings", {"mode": "on"})
    token = app.secret("token", {"t": _ref()})
    common = {
        "image": IMAGE,
        "env": {"MODE": settings.key("mode"), "TOKEN": token.key("t")},
    }
    app.deployment("api", **common, service_account=watcher)
    app.stateful_set("db", **common, service_account=watcher, node="primary")
    app.daemon_set("agent", **common)
    app.job("migrate", **common, service_account="external")
    app.cron_job("report", schedule="@hourly", **common, service_account=watcher)
    manifests = _manifests(app, nodes=NODES)
    for kind, name in (
        ("Deployment", "api"),
        ("StatefulSet", "db"),
        ("DaemonSet", "agent"),
        ("Job", "migrate"),
        ("CronJob", "report"),
    ):
        pod = _pod(manifests[(kind, name)])
        assert pod["securityContext"]["runAsUser"] == 10001, kind
        assert pod["terminationGracePeriodSeconds"] == 30, kind
        assert pod["nodeSelector"]["kubernetes.io/arch"] == "amd64", kind
        assert pod["containers"][0]["securityContext"] == {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
        }
    for kind, name in (
        ("Deployment", "api"),
        ("StatefulSet", "db"),
        ("CronJob", "report"),
    ):
        pod = _pod(manifests[(kind, name)])
        assert pod["serviceAccountName"] == "watcher"
        assert pod["automountServiceAccountToken"] is True
    assert (
        _pod(manifests[("DaemonSet", "agent")])["automountServiceAccountToken"] is False
    )
    job = _pod(manifests[("Job", "migrate")])
    assert job["serviceAccountName"] == "external"
    assert job["automountServiceAccountToken"] is False
    assert _pod(manifests[("StatefulSet", "db")])["nodeSelector"] == {
        "kubernetes.io/arch": "amd64",
        "kubernetes.io/hostname": "node-a",
    }
    # Every pod kind depends on the config, secret and service account it uses.
    components = _components(app, nodes=NODES)
    for name in ("api", "db", "agent", "migrate", "report"):
        dependencies = set(components[name][1])
        assert {"settings", "token"} <= dependencies, name
    assert "watcher" in components["db"][1]


def _ref() -> SecretVersionRef:
    return SecretVersionRef("0" * 32, "1" * 32)


def test_override_patches_every_kind():
    app = App("shop")
    db = app.stateful_set("db", image=IMAGE)
    job = app.job("migrate", image=IMAGE)
    tolerations = [{"key": "dedicated", "operator": "Exists"}]
    app.override(db, {"spec": {"template": {"spec": {"tolerations": tolerations}}}})
    app.override(job, {"metadata": {"annotations": {"note": "x"}}})
    manifests = _manifests(app)
    assert _pod(manifests[("StatefulSet", "db")])["tolerations"] == tolerations
    assert manifests[("Job", "migrate")]["metadata"]["annotations"] == {"note": "x"}


def test_node_pin_needs_a_declared_node():
    app = App("shop")
    app.job("migrate", image=IMAGE, node="gpu")
    with pytest.raises(ValueError, match="job 'migrate' is pinned to node 'gpu'"):
        app.render("shop")


# ------------------------------------------------------------- validation


def test_workload_names_are_unique_across_kinds():
    app = App("shop")
    app.deployment("api", image=IMAGE)
    with pytest.raises(ValueError, match="deployment 'api' is already declared"):
        app.job("api", image=IMAGE)


def test_claim_templates_only_on_stateful_sets():
    data = ClaimTemplate("data", size="1Gi")
    app = App("shop")
    with pytest.raises(ValidationError, match="ClaimTemplate volumes are per-pod"):
        app.deployment("api", image=IMAGE, volumes={"/data": data})
    with pytest.raises(ValidationError, match="should match pattern"):
        ClaimTemplate("data", size="lots")
    with pytest.raises(ValidationError, match="declared twice"):
        app.stateful_set(
            "db",
            image=IMAGE,
            volumes={
                "/a": data,
                "/b": ClaimTemplate("data", size="2Gi"),
            },
        )


def test_headless_service_name_collision_is_refused():
    app = App("shop")
    web = app.deployment("web", image=IMAGE, ports=[80])
    app.service(web, port=80, name="db")
    with pytest.raises(ValueError, match="Service 'db' is already declared"):
        app.stateful_set("db", image=IMAGE)


def test_job_rejects_selector_and_bad_values():
    from piceli.app.kinds import Job

    with pytest.raises(ValidationError, match="chooses a Job's selector"):
        Job(
            name="migrate",
            containers=[{"name": "migrate", "image": IMAGE}],
            selector={"a": "b"},
        )
    app = App("shop")
    with pytest.raises(ValidationError, match="restart_policy"):
        app.job("migrate", image=IMAGE, restart_policy="Always")
    with pytest.raises(ValidationError, match="schedule"):
        app.cron_job("report", schedule="every day", image=IMAGE)


def test_autoscaler_rules():
    app = App("shop")
    fixed = app.deployment(
        "fixed", image=IMAGE, replicas=3, resources=Resources(cpu="1")
    )
    with pytest.raises(ValueError, match="sets replicas= and is autoscaled"):
        app.autoscaler(fixed, max_replicas=5, cpu=50)
    bare = app.deployment("bare", image=IMAGE)
    with pytest.raises(ValueError, match="needs a cpu request on every container"):
        app.autoscaler(bare, max_replicas=5, cpu=50)
    api = app.deployment("api", image=IMAGE, resources=Resources(cpu="1"))
    with pytest.raises(ValidationError, match="cpu= and/or memory="):
        app.autoscaler(api, max_replicas=5)
    with pytest.raises(ValidationError, match="min_replicas cannot be above"):
        app.autoscaler(api, min_replicas=6, max_replicas=5, cpu=50)
    app.autoscaler(api, max_replicas=5, cpu=50)
    with pytest.raises(ValueError, match="already has autoscaler"):
        app.autoscaler(api, max_replicas=5, cpu=50, name="other")
    job = app.job("migrate", image=IMAGE)
    with pytest.raises(ValueError, match="targets a Deployment or a StatefulSet"):
        app.autoscaler(job, max_replicas=2, cpu=50)  # type: ignore[arg-type]
    other = App("other").deployment("x", image=IMAGE, resources=Resources(cpu="1"))
    with pytest.raises(ValueError, match="not declared on this app"):
        app.autoscaler(other, max_replicas=2, cpu=50)


def test_disruption_budget_rules():
    app = App("shop")
    web = app.deployment("web", image=IMAGE)
    with pytest.raises(ValidationError, match="exactly one"):
        app.disruption_budget(web)
    with pytest.raises(ValidationError, match="exactly one"):
        app.disruption_budget(web, min_available=1, max_unavailable=1)
    with pytest.raises(ValidationError, match="percentage"):
        app.disruption_budget(web, max_unavailable="half")
    job = app.job("migrate", image=IMAGE)
    with pytest.raises(ValueError, match="protects a Deployment"):
        app.disruption_budget(job, max_unavailable=1)  # type: ignore[arg-type]


def test_route_rules():
    app = App("shop")
    api = app.deployment("api", image=IMAGE)
    service = app.service(
        api, ports=[ServicePort(port=80, name="http"), ServicePort(port=81, name="b")]
    )
    with pytest.raises(ValueError, match="has 2 ports; pass port="):
        Route(service)
    with pytest.raises(ValueError, match="no port named 'grpc'"):
        Route(service, port="grpc")
    with pytest.raises(ValueError, match="no port 90"):
        Route(service, port=90)
    with pytest.raises(ValueError, match="pass port="):
        Route("external")
    with pytest.raises(ValidationError, match="path"):
        Route(service, "api", port=80)
    with pytest.raises(ValidationError, match="needs a port number"):
        app.http_route("r", gateway="public", routes=[Route("external", port="web")])
    with pytest.raises(ValueError, match="port 'x' is not one of its ports"):
        app.ingress("i", routes=[Route("api", port="x")])
    with pytest.raises(ValidationError, match="tls_secret= needs hosts="):
        app.ingress("i", routes=[Route(service, port=80)], tls_secret="tls")
    with pytest.raises(ValidationError, match="host"):
        app.ingress("i", routes=[Route(service, port=80)], hosts=["Not A Host"])
    with pytest.raises(ValueError, match="at least one route"):
        app.ingress("i", routes=[])
