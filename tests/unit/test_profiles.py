"""Credential profiles: ``piceli login``, ``Target.profile``, ``credentials=`` and ``--profile``.

Only temporary directories (``PICELI_PROFILES_DIR`` points into ``tmp_path``);
no cluster, no ambient kubeconfig.
"""

from __future__ import annotations

import json
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from piceli import App, profiles, tempfiles
from piceli.errors import ERRORS
from piceli.k8s.cli import app as cli
from piceli.k8s.cli.profiles import expand_profile_argv
from piceli.k8s.release_spec import ReleaseSpec
from piceli.pipeline import Pipeline, Target
from piceli.pipeline.compose import _target_table
from piceli.profiles import ProfileError

runner = CliRunner()
IMAGE = "docker.io/library/redis:7.2.3@sha256:" + "d" * 64


@pytest.fixture(autouse=True)
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    directory = tmp_path / "profiles"
    monkeypatch.setenv("PICELI_PROFILES_DIR", str(directory))
    monkeypatch.delenv("PICELI_IN_CLUSTER", raising=False)
    monkeypatch.setenv("PICELI_SERVICE_ACCOUNT_DIR", str(tmp_path / "no-sa"))
    profiles.set_override(None)
    yield directory
    profiles.set_override(None)
    profiles._incluster_config = None
    tempfiles.cleanup_all()


def write_kubeconfig(path: Path, *contexts: str) -> Path:
    items = "".join(
        f"- name: {name}\n  context: {{cluster: c, user: u}}\n" for name in contexts
    )
    path.write_text(
        "apiVersion: v1\nkind: Config\nclusters:\n- name: c\n  cluster: "
        '{server: "https://203.0.113.9"}\nusers:\n- name: u\n  user: '
        f"{{token: SECRET-TOKEN-VALUE}}\ncontexts:\n{items}"
    )
    return path


def login(tmp_path: Path, name: str = "my-cluster", context: str = "ctx") -> Path:
    kubeconfig = write_kubeconfig(tmp_path / "kc.yaml", context, "other")
    profiles.save_profile(name, kubeconfig, context)
    return kubeconfig


# ----------------------------------------------------------------- storage


def test_login_stores_a_reference_with_private_modes(
    tmp_path: Path, store: Path
) -> None:
    kubeconfig = login(tmp_path)
    file = store / "my-cluster.json"
    assert stat.S_IMODE(store.stat().st_mode) == 0o700
    assert stat.S_IMODE(file.stat().st_mode) == 0o600
    body = json.loads(file.read_text())
    assert body["kubeconfig"] == str(kubeconfig)
    assert body["context"] == "ctx"
    assert "SECRET-TOKEN-VALUE" not in file.read_text()
    assert list(store.iterdir()) == [file]  # no staging file left behind


def test_login_validates_file_and_context(tmp_path: Path) -> None:
    kubeconfig = write_kubeconfig(tmp_path / "kc.yaml", "a", "b")
    with pytest.raises(ProfileError) as missing:
        profiles.save_profile("x", tmp_path / "absent.yaml", "a")
    with pytest.raises(ProfileError) as unknown:
        profiles.save_profile("x", kubeconfig, "nope")
    with pytest.raises(ProfileError) as ambiguous:  # never current-context
        profiles.save_profile("x", kubeconfig, None)
    with pytest.raises(ProfileError) as bad_name:
        profiles.save_profile("Bad Name", kubeconfig, "a")
    for error in (missing, unknown, ambiguous, bad_name):
        assert error.value.code == "profile-invalid"
    only = write_kubeconfig(tmp_path / "one.yaml", "solo")
    assert profiles.save_profile("x", only).context == "solo"


def test_profiles_list_and_logout(tmp_path: Path) -> None:
    kubeconfig = login(tmp_path)
    listed = runner.invoke(cli, ["profiles", "--json"])
    assert listed.exit_code == 0
    document = json.loads(listed.stdout)
    assert document["profiles"] == [
        {
            "name": "my-cluster",
            "kubeconfig": str(kubeconfig),
            "context": "ctx",
            "kubeconfig_present": True,
        }
    ]
    first = runner.invoke(cli, ["logout", "my-cluster"])
    again = runner.invoke(cli, ["logout", "my-cluster"])
    assert json.loads(first.stdout)["state"] == "removed"
    assert json.loads(again.stdout)["state"] == "absent"
    assert kubeconfig.is_file()  # the kubeconfig itself is never touched
    assert (
        json.loads(runner.invoke(cli, ["profiles", "--json"]).stdout)["profiles"] == []
    )


def test_login_command_rejects_with_a_registered_code(tmp_path: Path) -> None:
    result = runner.invoke(
        cli, ["login", "x", "--kubeconfig", str(tmp_path / "absent.yaml")]
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reason"] == "profile-invalid"


# ------------------------------------------------------------------ Target


def test_target_profile_resolves_at_use_time_not_at_construction(
    tmp_path: Path,
) -> None:
    target = Target.profile("my-cluster", namespace="shop", nodes={"a": "node-1"})
    pipeline = Pipeline(App("shop"), target)  # builds with no profile stored
    assert pipeline.target is target
    with pytest.raises(ProfileError) as error:
        target.kubeconfig  # noqa: B018
    assert error.value.code == "profile-not-found"
    assert "piceli login my-cluster" in str(error.value)
    kubeconfig = login(tmp_path)
    assert target.kubeconfig == kubeconfig
    assert target.context == "ctx"


def test_profile_target_identity_names_the_profile_and_kubeconfig_is_unchanged() -> (
    None
):
    profile = Target.profile("my-cluster", namespace="shop")
    explicit = Target.kubeconfig("kc", context="ctx", namespace="shop")
    assert profile.identity() == {
        "profile": "my-cluster",
        "namespace": "shop",
        "cluster_uid": None,
        "namespace_uid": None,
        "nodes": {},
    }
    assert explicit.identity() == {  # hash input of existing targets, unchanged
        "context": "ctx",
        "namespace": "shop",
        "cluster_uid": None,
        "namespace_uid": None,
        "nodes": {},
    }
    assert "kc" not in repr(profile)


def test_target_names_a_profile_or_a_kubeconfig_not_both() -> None:
    from piceli.pipeline import PipelineError

    with pytest.raises(PipelineError):
        Target("kc", context="ctx", namespace="shop", credentials="my-cluster")
    with pytest.raises(PipelineError):
        Target(namespace="shop")


def test_pipeline_release_table_carries_the_profile_name() -> None:
    pipeline = Pipeline(App("shop"), Target.profile("my-cluster", namespace="shop"))
    table = _target_table(pipeline)
    assert table["credentials"] == "my-cluster"
    assert "kubeconfig" not in table and "context" not in table


# ------------------------------------------------------------- release spec


def spec_dict(target: dict[str, object]) -> dict[str, object]:
    return {
        "target": target,
        "release": {
            "name": "web",
            "owner": "owner",
            "field_manager": "manager",
            "composition": "compose.py:build",
            "state_dir": "state",
        },
        "images": {"web": IMAGE},
    }


def test_release_spec_credentials_resolve_when_used(tmp_path: Path) -> None:
    spec = ReleaseSpec.from_dict(
        spec_dict({"credentials": "my-cluster", "namespace": "demo"}), tmp_path
    )
    with pytest.raises(ProfileError) as error:
        spec.kubeconfig_target()
    assert error.value.code == "profile-not-found"
    kubeconfig = login(tmp_path)
    resolved = spec.kubeconfig_target()
    assert (resolved.kubeconfig, resolved.context) == (kubeconfig, "ctx")


@pytest.mark.parametrize(
    "target",
    [
        {"credentials": "p", "kubeconfig": "kc", "context": "c", "namespace": "d"},
        {"credentials": "p", "context": "c", "namespace": "d"},
        {"namespace": "d"},
        {"credentials": "Bad Name", "namespace": "d"},
    ],
)
def test_release_spec_refuses_ambiguous_target_credentials(
    tmp_path: Path, target: dict[str, object]
) -> None:
    from piceli.k8s.release_spec import ReleaseSpecError

    with pytest.raises(ReleaseSpecError):
        ReleaseSpec.from_dict(spec_dict(target), tmp_path)


# --------------------------------------------------------------- in-cluster


def service_account(root: Path) -> Path:
    root.mkdir()
    (root / "token").write_text("not-a-real-token")
    (root / "ca.crt").write_text("not-a-real-ca")
    (root / "namespace").write_text("piceli-system")
    return root


def test_profile_resolves_to_the_service_account_in_a_cluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "PICELI_SERVICE_ACCOUNT_DIR", str(service_account(tmp_path / "sa"))
    )
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    target = Target.profile("my-cluster", namespace="shop")  # no stored profile
    assert target.context == profiles.IN_CLUSTER_CONTEXT
    config = target.kubeconfig.read_text()
    assert "tokenFile" in config and "not-a-real-token" not in config
    assert target.kubeconfig == target.kubeconfig  # one materialisation per process


def test_in_cluster_flag_without_a_service_account_is_a_clean_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PICELI_IN_CLUSTER", "1")
    with pytest.raises(ProfileError) as error:
        profiles.resolve("my-cluster")
    assert error.value.code == "profile-invalid"


# ------------------------------------------------------------------ --profile


def test_profile_option_replaces_kubeconfig_and_context(tmp_path: Path) -> None:
    kubeconfig = login(tmp_path)
    argv = expand_profile_argv(
        ["observe", "logs-command", "--profile", "my-cluster", "--user=x"]
    )
    assert argv == [
        "observe",
        "logs-command",
        "--user=x",
        "--kubeconfig",
        str(kubeconfig),
        "--context",
        "ctx",
    ]
    assert expand_profile_argv(["profiles", "--json"]) == ["profiles", "--json"]
    result = runner.invoke(
        cli,
        expand_profile_argv(
            [
                "observe",
                "logs-command",
                "--namespace",
                "demo",
                "--target",
                "deployment/api",
                "--profile=my-cluster",
            ]
        ),
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)[:1] != []


def test_profile_option_is_exclusive_with_kubeconfig() -> None:
    for extra in (["--kubeconfig", "kc"], ["--context", "c"], ["--context=c"]):
        with pytest.raises(SystemExit) as raised:
            expand_profile_argv(["observe", "status", "--profile", "p", *extra])
        assert getattr(raised.value, "code", None) == 2


def test_profile_option_missing_profile_names_the_login_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        expand_profile_argv(["observe", "status", "--profile", "absent"])
    out = json.loads(capsys.readouterr().out)
    assert out["reason"] == "profile-not-found"
    assert "piceli login absent" in out["message"]


def test_profile_option_on_a_file_driven_command_overrides_the_target(
    tmp_path: Path,
) -> None:
    kubeconfig = login(tmp_path)
    target = Target.kubeconfig("old.kubeconfig", context="old", namespace="shop")
    assert target.context == "old"
    assert expand_profile_argv(
        ["status", "app.py:pipeline", "--profile", "my-cluster"]
    ) == [
        "status",
        "app.py:pipeline",
    ]
    assert (target.kubeconfig, target.context) == (kubeconfig, "ctx")
    assert target.identity()["profile"] == "my-cluster"


def test_the_codes_are_registered() -> None:
    for code in ("profile-not-found", "profile-invalid", "profile-conflict"):
        assert code in ERRORS
