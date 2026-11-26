"""``Controller(candidate=)`` (0.83.0): the declaration, how it reaches the
controller process, and the controller's poll, requests and status."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from piceli import Registry
from piceli.gitops.candidates import CREDENTIALS_DIR, STATE_KEY
from piceli.gitops.install import InstallSettings, render_controller
from piceli.gitops.state import candidate_request
from piceli.infra import Candidate, CandidateCopy, Cluster, Controller, Node
from piceli.infra.cluster import ClusterError
from piceli.infra.composition import _cluster_dict, _cluster_from, load_composition
from piceli.infra.controller import CompositionConfig
from tests.unit.infra.test_controller import world

__all__ = ["world"]

EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "composition"
IMAGE = "ghcr.io/example/piceli@sha256:" + "0" * 64
COMMIT = "a" * 40
DIGEST = "sha256:" + "1" * 64


def _candidate(**options: Any) -> Candidate:
    return Candidate(
        environment="main",
        copies=(
            CandidateCopy(
                "web",
                "oci://ghcr.io/example/shop-web",
                tag="{commit}",
                credentials="shop-push",
            ),
        ),
        **options,
    )


def _cluster(**controller: Any) -> Cluster:
    return Cluster(
        "my-cluster",
        api="https://10.0.0.1:6443",
        credentials="my-cluster",
        nodes=[Node("node-a", arch="amd64", roles=["builder", "registry"])],
        registry=Registry.in_cluster(on="node-a"),
        controller=Controller(on="node-a", **controller),
    )


def _iso(seconds: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------- the declaration


def test_the_setting_is_checked_with_the_controller() -> None:
    assert Controller(on="node-a").candidate is None
    assert Controller(on="node-a", candidate=_candidate()).candidate is not None
    for bad, words in (
        (
            lambda: Controller(on="node-a", candidate={"environment": "main"}),
            r"Candidate\(\.\.\.\)",
        ),  # type: ignore[arg-type]
        (
            lambda: Controller(on="node-a", candidate=Candidate(environment="main")),
            "at least one CandidateCopy",
        ),
    ):
        with pytest.raises(ClusterError, match=words) as error:
            bad()
        assert error.value.code == "cluster-invalid"


def test_the_composition_carries_it_to_the_controller_process_only_when_set() -> None:
    assert "controller" not in _cluster_dict(_cluster())
    body = _cluster_dict(_cluster(candidate=_candidate(quiet="10m", keep=3)))
    assert body["controller"]["candidate"]["quiet"] == "10m"
    assert json.loads(json.dumps(body)) == body
    back = _cluster_from(body)
    assert back is not None and back.controller is not None
    assert back.controller.candidate == _candidate(quiet="10m", keep=3)


def test_the_install_mounts_each_credentials_secret_as_files_never_values() -> None:
    composition = load_composition(EXAMPLE / "infra.py")
    document = composition.to_dict()
    assert composition.cluster is not None and composition.cluster.controller
    document["cluster"]["controller"] = {
        "on": composition.cluster.controller.on,
        "builds": ["cluster"],
        "host_wait": "30m",
        "candidate": _candidate().to_dict(),
    }
    config = CompositionConfig(composition=document)
    (deployment,) = [
        item
        for item in render_controller(config, InstallSettings(image=IMAGE))
        if item["kind"] == "Deployment"
    ]
    spec = deployment["spec"]["template"]["spec"]
    (volume,) = [item for item in spec["volumes"] if "candidate" in item["name"]]
    assert volume["secret"] == {"secretName": "shop-push", "defaultMode": 0o400}
    (mount,) = [
        item
        for item in spec["containers"][0]["volumeMounts"]
        if "candidate" in item["name"]
    ]
    assert mount["mountPath"] == f"{CREDENTIALS_DIR}/shop-push"
    assert mount["readOnly"] is True
    assert "shop-push" not in json.dumps(spec["containers"][0]["env"])
    plain = render_controller(
        CompositionConfig(composition=load_composition(EXAMPLE / "infra.py").to_dict()),
        InstallSettings(image=IMAGE),
    )
    assert "candidate" not in json.dumps(plain)


# ------------------------------------------------------------- the controller


def _declare(world: dict[str, Any], **options: Any) -> Any:
    """The controller declares ``Controller(candidate=)`` for ``main``, with
    a fake history reader and copy set; one commit of its first source passed
    long ago."""
    controller = world["controller"]
    controller.poll_once()  # the first deploy
    setting = _candidate(**options)
    controller._candidate_setting = lambda: setting  # no Cluster in this world
    source = controller._candidate_sources()[0]
    controller.state["passed_commits"] = {
        "main": {
            source: {
                COMMIT: {
                    "first_run": "run-1",
                    "first_at": _iso(world["clock"]["now"] - 7200),
                    "last_run": "run-1",
                    "last_at": _iso(world["clock"]["now"] - 7200),
                }
            }
        }
    }
    live = controller._candidates()
    copied: list[tuple[str, str, str | None]] = []
    live._artifacts = lambda document, run, env=None: {
        "env": env,
        "run": run,
        "components": [
            {
                "name": "web",
                "digest": DIGEST,
                "image": {
                    "repository": "shop/web",
                    "reference": f"registry:5000/shop/web@{DIGEST}",
                },
            }
        ],
    }
    live._copy = lambda item, secret, ledger: (
        copied.append((item["source"], item["target"], secret))
        or SimpleNamespace(ok=True, failed=None)
    )
    live._spawn = lambda work: work()
    return SimpleNamespace(controller=controller, copied=copied, live=live)


def test_the_poll_prepares_the_candidate_after_the_quiet_time_and_publishes_it(
    world: dict[str, Any],
) -> None:
    rig = _declare(world)
    status = rig.controller.poll_once()
    (found,) = status["candidates"]
    assert (found["commit"], found["state"]) == (COMMIT, "ready")
    assert found["copies"] == {"web": "copied"}
    assert found["digests"] == [DIGEST]
    assert rig.copied == [
        (
            f"oci://registry:5000/shop/web@{DIGEST}",
            "oci://ghcr.io/example/shop-web:" + "a" * 12,
            "shop-push",
        )
    ]
    assert json.loads(json.dumps(status))["candidates"] == status["candidates"]
    assert world["channel"].read_status()["candidates"] == status["candidates"]
    # The same commit again: nothing is copied twice.
    rig.controller.poll_once()
    assert len(rig.copied) == 1


def test_a_quiet_time_not_over_shows_the_candidate_waiting(
    world: dict[str, Any],
) -> None:
    rig = _declare(world, quiet="3h")
    status = rig.controller.poll_once()
    (found,) = status["candidates"]
    assert found["state"] == "waiting"
    assert rig.copied == []
    world["clock"]["now"] += 3 * 3600
    (found,) = rig.controller.poll_once()["candidates"]
    assert found["state"] == "ready"


def test_the_request_inbox_starts_and_releases_a_candidate(
    world: dict[str, Any],
) -> None:
    rig = _declare(world, quiet="3h")
    rig.controller.poll_once()
    key, body = candidate_request("request", COMMIT[:9])
    world["channel"].add_request(key, body)
    (found,) = rig.controller.poll_once()["candidates"]
    assert found["state"] == "ready"  # no 3 hours to wait
    key, body = candidate_request("release", COMMIT[:9])
    world["channel"].add_request(key, body)
    status = rig.controller.poll_once()
    assert status["candidates"][0]["released"] is True
    assert status["rejected_requests"] == []


def test_a_request_without_a_declaration_is_rejected(world: dict[str, Any]) -> None:
    controller = world["controller"]
    controller.poll_once()
    key, body = candidate_request("request", COMMIT[:9])
    world["channel"].add_request(key, body)
    status = controller.poll_once()
    (rejected,) = status["rejected_requests"]
    assert rejected["reason"] == "gitops-request-invalid"
    assert "no Controller(candidate=)" in rejected["message"]
    assert "candidates" not in status
    assert STATE_KEY not in controller.state
