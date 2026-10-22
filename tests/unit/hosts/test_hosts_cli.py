"""Ref pointers, gc, the rendered module and ``piceli host status|wait`` (0.24.0)."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.dev import nix
from piceli.dev.nix import NixCache
from piceli.hosts import remote
from piceli.hosts.model import Branch, HostCheck, HostReport, NixHost, SshTarget, Tag
from piceli.hosts.module import nix_string, render_module
from tests.unit.hosts.test_agent import PUBLIC, commit_of, publish, store_path

REV = commit_of(7)


# ------------------------------------------------------------------ publishing


def test_publish_plans_name_the_pointers_they_write(tmp_path: Path) -> None:
    cache = NixCache(PUBLIC, url=f"file://{tmp_path}")
    plan = nix.publish_plan(
        target=".#nixosConfigurations.web-1.config.system.build.toplevel",
        name="web-1", commit=REV, tag="v1.2.0", host="nix-1", profile="nix",
        image=False, cache=cache, branch="main",
    )  # fmt: skip
    assert plan["pointers"] == [
        "piceli/web-1/refs/tags/v1.2.0.json",
        "piceli/web-1/refs/heads/main.json",
    ]
    image = nix.publish_plan(
        target=".#image", name="web-1-image", commit=REV, tag="v1.2.0",
        host="nix-1", profile="nix", image=True, cache=cache,
    )  # fmt: skip
    assert image["pointers"] == []  # an image is not a system a host follows


def test_pointers_are_signed_and_name_the_manifest(tmp_path: Path) -> None:
    signed = publish(tmp_path, REV, tag="v1.2.0", branch="main")
    for ref in ("tags/v1.2.0", "heads/main"):
        body = json.loads(
            (tmp_path / "piceli" / "web-1" / "refs" / f"{ref}.json").read_text()
        )
        assert nix.verify_pointer(body, PUBLIC)
        assert not nix.verify_manifest(body, PUBLIC)  # another schema
        assert body["ref"] == f"refs/{ref}" and body["commit"] == REV
        assert body["manifest"] == f"piceli/web-1/{REV}.json"
        assert body["manifest_sha256"] == nix.manifest_digest(signed)
        assert body["published_at"] == "2026-10-22T10:00:00.000000Z"


def test_branch_of_names_local_and_remote_branches(tmp_path: Path) -> None:
    from tests.unit.dev.test_pack import repo

    source = repo(tmp_path / "app", {"a.txt": "a"})
    branch = subprocess.run(
        ["git", "-C", str(source), "branch", "--show-current"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip
    assert nix.branch_of(source, branch) == branch
    assert nix.branch_of(source, "HEAD") is None
    assert nix.branch_of(source, "../x") is None


def test_gc_keeps_what_the_pointers_name_and_drops_old_tag_pointers(
    tmp_path: Path,
) -> None:
    publish(tmp_path, commit_of(1), tag="v1.0.0")
    publish(tmp_path, commit_of(2), tag="v1.1.0")
    publish(tmp_path, commit_of(3), branch="feature")
    objects = nix.CacheObjects(NixCache(PUBLIC, url=f"file://{tmp_path}"))
    plan = nix.gc_plan(
        objects, {commit_of(2): ["v1.1.0"]}, keep_tags=1, main="main",
        grace_seconds=0, now=time.time() + 10,
    )  # fmt: skip
    hash1 = store_path(commit_of(1)).split("/")[-1].split("-")[0]
    assert plan["delete"] == sorted(
        [
            f"piceli/web-1/{commit_of(1)}.json",
            "piceli/web-1/refs/tags/v1.0.0.json",
            f"{hash1}.narinfo",
        ]
    )
    # The branch pointer keeps its build although Git does not keep it.
    assert f"piceli/web-1/{commit_of(3)}.json" not in plan["delete"]
    nix.gc_apply(objects, plan)
    left = {key for key, _ in objects.list()}
    assert "piceli/web-1/refs/tags/v1.1.0.json" in left
    assert "piceli/web-1/refs/heads/feature.json" in left


def test_gc_deletes_a_pointer_before_its_manifest(tmp_path: Path) -> None:
    deleted: list[str] = []

    class Objects:
        def delete(self, key: str) -> None:
            deleted.append(key)

    nix.gc_apply(
        Objects(),  # type: ignore[arg-type]
        {"delete": ["nar/a.nar", "a.narinfo", f"piceli/w/{REV}.json",
                    "piceli/w/refs/tags/v1.json"]},
    )  # fmt: skip
    assert deleted == [
        "piceli/w/refs/tags/v1.json", f"piceli/w/{REV}.json", "a.narinfo", "nar/a.nar",
    ]  # fmt: skip


# ------------------------------------------------------------------ module


def _host(tmp_path: Path) -> NixHost:
    return NixHost(
        "web-1",
        server="ssh://root@web-1.example.net",
        flake=".#web-1",
        follow=Tag("v*"),
        cache=NixCache(PUBLIC, url=f"file://{tmp_path}"),
        checks=[HostCheck.http("http://127.0.0.1/healthz?x=${y}")],
        report=HostReport("https://poet.example.net:4318", headers_file="/etc/p/h"),
    )


NIX_INSTANTIATE = shutil.which("nix-instantiate") or (
    "/nix/var/nix/profiles/default/bin/nix-instantiate"
    if Path("/nix/var/nix/profiles/default/bin/nix-instantiate").exists()
    else None
)


def test_the_module_embeds_the_agent_and_a_config_without_secrets(
    tmp_path: Path,
) -> None:
    text = render_module(_host(tmp_path), "hosts.py:web", version="0.24.0")
    assert text.startswith("# The Piceli host agent of web-1 (piceli 0.24.0).")
    assert "restartIfChanged = false;" in text and "stopIfChanged = false;" in text
    assert 'OnUnitInactiveSec = "300s";' in text
    assert "piceli_host_agent.py" in text and "nix_verify.py" in text
    assert "--config ${configFile}" in text
    assert "/etc/p/h" in text and "\\${y}" in text  # escaped, not interpolated
    settings = text.split("builtins.fromJSON ", 1)[1].split(";\n", 1)[0]
    assert "credentials" not in settings.replace("read_credentials_file", "")


@pytest.mark.skipif(NIX_INSTANTIATE is None, reason="no nix-instantiate")
def test_the_module_is_valid_nix_and_its_strings_are_exact(tmp_path: Path) -> None:
    assert NIX_INSTANTIATE is not None
    module = tmp_path / "piceli-host.nix"
    module.write_text(render_module(_host(tmp_path), "hosts.py:web"))
    parsed = subprocess.run(
        [NIX_INSTANTIATE, "--parse", str(module)], capture_output=True, text=True
    )
    assert parsed.returncode == 0, parsed.stderr
    from piceli.hosts import agent

    source = Path(agent.__file__).read_text()
    expression = tmp_path / "source.nix"
    expression.write_text(nix_string(source))
    shown = subprocess.run(
        [NIX_INSTANTIATE, "--eval", "--json", str(expression)],
        capture_output=True, text=True,
    )  # fmt: skip
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout) == source


def test_host_module_writes_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import piceli.k8s.cli.host as cli_host
    from piceli.k8s.cli import app

    monkeypatch.setattr(cli_host, "load_host", lambda _ref: _host(tmp_path))
    out = tmp_path / "hosts" / "web-1" / "piceli-host.nix"
    done = CliRunner().invoke(
        app, ["host", "module", "hosts.py:web", "--output", str(out)]
    )
    assert done.exit_code == 0, done.stdout + done.stderr
    body = json.loads(done.stdout)
    assert body["state"] == "written" and body["host"] == "web-1"
    assert out.read_text().startswith("# The Piceli host agent of web-1")


# ------------------------------------------------------------------ status, wait


def _status(**over: Any) -> dict[str, Any]:
    attempt = {
        "commit": REV, "tag": "v1.2.0", "state": "deployed",
        "stages": [{"name": "verify", "state": "passed", "seconds": 0.2}],
        "checks": [{"name": "unit-1", "state": "passed"}], "rollback": None,
    }  # fmt: skip
    return {
        "schema": "piceli.nix-host-status.v1",
        "host": "web-1",
        "current": {"commit": REV, "tag": "v1.2.0", "store_path": store_path(REV)},
        "last_attempt": attempt,
        "poll": {"at": "2026-10-22T10:00:00Z", "state": "deployed"},
        **over,
    }


def test_outcome_of_a_wait() -> None:
    assert remote.outcome(_status(), REV[:7]) == ("deployed", None)
    assert remote.outcome(_status(), "v1.2.0") == ("deployed", None)
    assert remote.outcome(_status(), "v1.3.0") == ("waiting", None)
    assert remote.outcome(_status(), "abc") == ("waiting", None)  # too short
    other = commit_of(8)
    rolled = _status(last_attempt={"commit": other, "state": "rolled-back"})
    assert remote.outcome(rolled, other) == ("failed", "host-rolled-back")
    refused = _status(last_attempt={"commit": other, "state": "rejected"})
    assert remote.outcome(refused, other) == ("failed", "host-deploy-failed")
    skipped = _status(poll={"state": "skipped", "commit": other})
    assert remote.outcome(skipped, other) == ("failed", "host-deploy-failed")


def test_status_is_read_over_ssh_with_known_host_keys_only() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], **_: Any) -> Any:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(_status()).encode(), b"")

    target = SshTarget.parse("ssh://admin@web-1.example.net:2222")
    assert remote.read_status(target, runner=runner)["host"] == "web-1"
    argv = calls[0]
    assert "BatchMode=yes" in argv and "StrictHostKeyChecking=yes" in argv
    assert (
        argv[argv.index("-p") + 1] == "2222" and argv[argv.index("-l") + 1] == "admin"
    )
    assert argv[-3:] == ["cat", "--", "/var/lib/piceli-host/status.json"]

    def unreachable(argv: list[str], **_: Any) -> Any:
        return subprocess.CompletedProcess(argv, 255, b"", b"")

    from piceli.hosts.model import HostError

    with pytest.raises(HostError) as raised:
        remote.read_status(target, runner=unreachable)
    assert raised.value.code == "host-unreachable"


def test_ssh_targets() -> None:
    assert SshTarget.parse("web-1") == SshTarget("web-1", "root", 22)
    assert SshTarget.parse("me@10.0.0.5:2200") == SshTarget("10.0.0.5", "me", 2200)
    assert SshTarget.parse("ssh://[2001:db8::1]:22") == SshTarget(
        "2001:db8::1", "root", 22
    )


def _cli(monkeypatch: pytest.MonkeyPatch, statuses: list[dict[str, Any]]) -> Any:
    import piceli.hosts.remote as remote_module

    reads = iter(statuses)
    monkeypatch.setattr(remote_module, "read_status", lambda *_a, **_k: next(reads))
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    from piceli.k8s.cli import app

    return lambda *args: CliRunner().invoke(app, list(args))


def test_host_status_json(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _cli(monkeypatch, [_status()])
    done = run("host", "status", "root@web-1.example.net", "--json")
    assert done.exit_code == 0, done.stdout + done.stderr
    body = json.loads(done.stdout)
    assert body["current"]["tag"] == "v1.2.0" and body["target"] == "web-1.example.net"
    assert "last attempt" in done.stderr


def test_host_wait_succeeds_fails_and_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = commit_of(9)
    waiting = _status(last_attempt={"commit": other, "state": "running"})
    run = _cli(monkeypatch, [waiting, _status(current={"commit": other}, last_attempt={
        "commit": other, "state": "deployed",
        "stages": [{"name": "switch", "state": "passed", "seconds": 12.5}]})])  # fmt: skip
    done = run("host", "wait", "web-1.example.net", other, "--json")
    assert done.exit_code == 0, done.stdout + done.stderr
    body = json.loads(done.stdout)
    assert (
        body["state"] == "deployed" and body["attempt"]["stages"][0]["seconds"] == 12.5
    )
    run = _cli(monkeypatch, [_status(last_attempt={
        "commit": other, "state": "rolled-back",
        "checks": [{"name": "unit-1", "state": "failed"}],
        "rollback": {"state": "done"}})])  # fmt: skip
    done = run("host", "wait", "web-1.example.net", other, "--json")
    assert done.exit_code == 1
    assert json.loads(done.stdout)["reason"] == "host-rolled-back"
    run = _cli(monkeypatch, [waiting] * 5)
    clock = iter([0.0, 0.0, 5.0, 5.0, 11.0, 11.0, 11.0, 11.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))
    done = run("host", "wait", "web-1.example.net", other, "--timeout", "10")
    assert done.exit_code == 1
    assert json.loads(done.stdout)["reason"] == "host-wait-timeout"


def test_the_infra_exports_the_host_types() -> None:
    import piceli.infra as infra

    assert infra.NixHost is NixHost and infra.Tag is Tag and infra.Branch is Branch


def _pointer(cache_dir: Path, tag: str) -> dict[str, Any]:
    path = cache_dir / "piceli" / "web-1" / "refs" / "tags" / f"{tag}.json"
    return dict(json.loads(path.read_text()))


def test_wait_judges_only_the_newest_publication_of_a_rev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import datetime as dt

    from tests.unit.hosts.test_agent import OLD, T0, Clock, FakeHost, host, make_agent
    from tests.unit.hosts.test_agent import status as read

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    link = tmp_path / "current-system"
    link.symlink_to(OLD)
    fake, clock = FakeHost(link, unit_active=False), Clock()
    rev = commit_of(1)
    publish(cache_dir, rev, tag="v1.0.0")
    make_agent(tmp_path, host(cache_dir), fake, clock).run_once()
    failed = read(tmp_path)
    assert remote.outcome(failed, rev) == ("failed", "host-rolled-back")
    make_agent(tmp_path, host(cache_dir), fake, clock).run_once()  # skipped: bad
    assert remote.outcome(read(tmp_path), rev) == ("failed", "host-rolled-back")

    # The same commit is published again: the old failure no longer counts.
    publish(cache_dir, rev, tag="v1.0.0", when=T0 + dt.timedelta(hours=1))
    again = _pointer(cache_dir, "v1.0.0")["published_at"]
    assert remote.outcome(read(tmp_path), rev, published_after=again) == (
        "waiting", None,
    )  # fmt: skip
    # A host that has seen the newer pointer (an attempt in progress) waits too.
    seen = {**failed, "in_progress": {"commit": rev, "published_at": again,
                                      "state": "running"}}  # fmt: skip
    assert remote.outcome(seen, rev) == ("waiting", None)
    seen = {**failed, "poll": {"state": "idle", "commit": rev, "published_at": again}}
    assert remote.outcome(seen, rev) == ("waiting", None)
    run = _cli(monkeypatch, [read(tmp_path)] * 3)
    clock_values = iter([0.0, 0.0, 5.0, 5.0, 11.0, 11.0, 11.0, 11.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock_values))
    done = run("host", "wait", "web-1.example.net", rev, "--timeout", "10",
               "--published-after", again)  # fmt: skip
    assert json.loads(done.stdout)["reason"] == "host-wait-timeout"

    # The agent records an attempt of the new publication: it is judged.
    make_agent(tmp_path, host(cache_dir), fake, clock).run_once()
    retried = read(tmp_path)
    assert retried["last_attempt"]["published_at"] == again
    assert remote.outcome(retried, rev, published_after=again) == (
        "failed", "host-rolled-back",
    )  # fmt: skip
    fake.unit_active = True
    publish(cache_dir, rev, tag="v1.0.0", when=T0 + dt.timedelta(hours=2))
    make_agent(tmp_path, host(cache_dir), fake, clock).run_once()
    assert remote.outcome(read(tmp_path), rev) == ("deployed", None)
