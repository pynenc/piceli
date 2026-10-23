"""The Nix host agent against a fake ``nix``, switch and ``systemctl`` (0.24.0).

The cache is a ``file://`` directory in ``tmp_path`` filled by the real
publishing functions (manifests, ref pointers) and narinfos signed with a
generated key; commands are answered by :class:`FakeHost`. No network, no
``nix``; the opt-in test with the real ``nix`` is ``test_agent_real.py``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
import urllib.error
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from piceli.dev import nix
from piceli.dev.nix import NixCache
from piceli.hosts import agent as host_agent
from piceli.hosts.agent import Agent
from piceli.hosts.model import Branch, HostCheck, HostReport, NixHost, Tag, agent_config

SECRET = nix.generate_key("cache-1")
PUBLIC = nix.public_key(SECRET)
SYSTEM = "x86_64-linux"
ATTRIBUTE = ".#nixosConfigurations.web-1.config.system.build.toplevel"
OLD = "/nix/store/" + "0" * 32 + "-nixos-system-web-1-old"
T0 = dt.datetime(2026, 10, 22, 10, 0, tzinfo=dt.UTC)


def store_path(commit: str) -> str:
    digest = hashlib.sha256(commit.encode()).hexdigest()
    alphabet = "0123456789abcdfghijklmnpqrsvwxyz"
    part = "".join(alphabet[int(digest[i : i + 2], 16) % 32] for i in range(0, 64, 2))
    return f"/nix/store/{part}-nixos-system-web-1"


def commit_of(n: int) -> str:
    return f"{n:040x}"


def nar_hash_of(path: str) -> str:
    return "sha256:" + nix.nix32(hashlib.sha256(path.encode()).digest())


def narinfo(path: str, secret: str = SECRET, nar: str | None = None) -> str:
    nar = nar or nar_hash_of(path)
    fingerprint = f"1;{path};{nar};1234;".encode()
    return (
        f"StorePath: {path}\nURL: nar/{Path(path).name}.nar\nCompression: none\n"
        f"NarHash: {nar}\nNarSize: 1234\nReferences: \n"
        f"Sig: {nix.sign(secret, fingerprint)}\n"
    )


def publish(
    root: Path,
    commit: str,
    *,
    tag: str | None = None,
    branch: str | None = None,
    when: dt.datetime = T0,
    secret: str = SECRET,
    narinfo_secret: str | None = None,
    nar: str | None = None,
    system: str = SYSTEM,
    attribute: str = ATTRIBUTE,
    name: str = "web-1",
    pinned: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """What ``piceli dev nix build --publish`` leaves in a file cache."""
    path = store_path(commit)
    cache = NixCache(PUBLIC, url=f"file://{root}")
    body = nix.manifest(
        name=name, commit=commit, tag=tag, target=attribute,
        published={"store_path": path, "system": system,
                   "nar_hash": nar_hash_of(path), "nar_size": 1234,
                   "closure_size": 5678},
        host="nix-1", key="cache-1", now=when, pinned=pinned,
    )  # fmt: skip
    signed = nix.sign_manifest(body, secret)
    nix.upload_manifest(cache, signed, s3_keys=None)
    (root / f"{Path(path).name.split('-', 1)[0]}.narinfo").write_text(
        narinfo(path, narinfo_secret or secret, nar)
    )
    keys = [
        nix.pointer_key(name, kind, ref)
        for kind, ref in (("tag", tag), ("branch", branch))
        if ref
    ]
    nix.upload_pointers(cache, signed, keys, secret, s3_keys=None, now=when)
    return signed


class FakeHost:
    """``nix``, ``nix-env``, ``switch-to-configuration`` and ``systemctl``."""

    def __init__(self, current: Path, *, unit_active: bool = True) -> None:
        self.current = current
        self.calls: list[list[str]] = []
        self.envs: list[Mapping[str, str]] = []
        self.timeouts: list[float | None] = []
        self.unit_active = unit_active
        self.fail_switch: set[str] = set()
        self.fail_copy = False
        self.fail_delete = False
        self.fail_gc: int | Exception = 0

    # The system profile, as ``nix-env -p PROFILE`` keeps it: numbered
    # ``PROFILE-N-link`` generations and ``PROFILE`` -> the current one.
    @staticmethod
    def generations(profile: Path) -> dict[int, str]:
        found = {}
        for link in profile.parent.glob(f"{profile.name}-*-link"):
            number = link.name[len(profile.name) + 1 : -len("-link")]
            if number.isdigit():
                found[int(number)] = os.readlink(link)
        return found

    @staticmethod
    def add_generation(profile: Path, path: str) -> int:
        number = max(FakeHost.generations(profile), default=0) + 1
        (profile.parent / f"{profile.name}-{number}-link").symlink_to(path)
        if profile.is_symlink():
            profile.unlink()
        profile.symlink_to(f"{profile.name}-{number}-link")
        return number

    def _profile(self, argv: list[str]) -> int:
        profile = Path(argv[argv.index("-p") + 1])
        if "--set" in argv:
            self.add_generation(profile, argv[-1])
            return 0
        if self.fail_delete:
            return 1
        found = sorted(self.generations(profile), reverse=True)
        current = int(os.readlink(profile).split("-")[-2])
        spec = argv[argv.index("--delete-generations") + 1 :]
        if spec[0].startswith("+"):  # nix's deleteGenerationsGreaterThan
            rest = found[found.index(current) :]
            drop = set(rest[int(spec[0][1:]) :])
        else:
            drop = {int(n) for n in spec}
        assert current not in drop
        for number in drop:
            (profile.parent / f"{profile.name}-{number}-link").unlink()
        return 0

    def __call__(self, argv: Sequence[str], **kwargs: Any) -> Any:
        argv = list(argv)
        self.calls.append(argv)
        self.envs.append(kwargs.get("env") or {})
        self.timeouts.append(kwargs.get("timeout"))
        out, code = b"", 0
        if argv[0] == "fake-nix-env":
            code = self._profile(argv)
        elif argv[0] == "fake-nix-store" and "--gc" in argv:
            if isinstance(self.fail_gc, Exception):
                raise self.fail_gc
            code = self.fail_gc
            out = b"" if code else b"12 store paths deleted, 345.60 MiB freed\n"
        elif argv[0] == "fake-nix" and "copy" in argv:
            code = 1 if self.fail_copy else 0
        elif argv[0] == "fake-nix" and "path-info" in argv:
            path = argv[-1]
            out = json.dumps({path: {"narHash": nar_hash_of(path)}}).encode()
        elif argv[0].endswith("/bin/switch-to-configuration"):
            path = argv[0][: -len("/bin/switch-to-configuration")]
            if path in self.fail_switch:
                code = 1
            else:
                self.current.unlink()
                self.current.symlink_to(path)
        elif argv[0] == "fake-systemctl":
            code = 0 if self.unit_active else 3
        return subprocess.CompletedProcess(argv, code, out, b"")

    def commands(self, word: str) -> list[list[str]]:
        return [call for call in self.calls if word in " ".join(call)]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def host(cache_dir: Path, **overrides: Any) -> NixHost:
    values: dict[str, Any] = {
        "server": "ssh://root@web-1.example.net",
        "flake": ".#web-1",
        "follow": Tag("v*"),
        "cache": NixCache(PUBLIC, url=f"file://{cache_dir}"),
        "checks": [HostCheck.unit("nginx.service", timeout=10)],
    }
    return NixHost("web-1", **{**values, **overrides})


def make_agent(
    tmp_path: Path,
    declared: NixHost,
    fake: FakeHost,
    clock: Clock | None = None,
    opener: Any = None,
) -> Agent:
    config = {
        **agent_config(declared),
        "state_dir": str(tmp_path / "state"),
        "profile": str(tmp_path / "profile"),
        "current_system": str(fake.current),
        "system": SYSTEM,
        "tools": {"nix": "fake-nix", "nix_env": "fake-nix-env",
                  "nix_store": "fake-nix-store", "systemctl": "fake-systemctl"},
    }  # fmt: skip
    clock = clock or Clock()
    return Agent(
        config, runner=fake, opener=opener or _no_network, sleep=clock.sleep,
        monotonic=clock.monotonic, check_interval=2.0,
    )  # fmt: skip


def _no_network(request: Any, timeout: float = 0) -> Any:
    raise AssertionError(f"unexpected request {getattr(request, 'full_url', request)}")


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    path = tmp_path / "cache"
    path.mkdir()
    return path


@pytest.fixture
def fake(tmp_path: Path) -> FakeHost:
    link = tmp_path / "current-system"
    link.symlink_to(OLD)
    return FakeHost(link)


def status(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "state" / "status.json").read_text())


# ------------------------------------------------------------------ deploys


def test_a_tag_host_switches_to_the_highest_version_and_checks_it(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    for n, tag in enumerate(["v1.0.0", "v1.10.0", "v1.2.0", "v2.0.0-rc.1", "vNext"]):
        publish(cache_dir, commit_of(n + 1), tag=tag)
    poll = make_agent(tmp_path, host(cache_dir), fake).run_once()
    assert poll["state"] == "deployed" and poll["commit"] == commit_of(2)
    body = status(tmp_path)
    assert body["schema"] == "piceli.nix-host-status.v1"
    assert body["current"]["tag"] == "v1.10.0"
    assert body["current"]["store_path"] == store_path(commit_of(2))
    assert body["previous"]["store_path"] == OLD
    attempt = body["last_attempt"]
    assert [s["name"] for s in attempt["stages"]] == [
        "resolve", "verify", "realise", "switch", "checks",
    ]  # fmt: skip
    assert all(s["state"] == "passed" and "seconds" in s for s in attempt["stages"])
    assert attempt["checks"] == [
        {"name": "unit-1", "kind": "unit", "state": "passed", "tries": 1,
         "seconds": 0.0},
    ]  # fmt: skip
    assert body["ignored_tags"] == ["v2.0.0-rc.1", "vNext"]
    assert fake.commands("fake-nix-env") == [
        ["fake-nix-env", "-p", str(tmp_path / "profile"), "--set",
         store_path(commit_of(2))],
    ]  # fmt: skip
    assert os.readlink(fake.current) == store_path(commit_of(2))
    assert (tmp_path / "state" / "status.json").stat().st_mode & 0o777 == 0o644
    assert (tmp_path / "state" / "state.json").stat().st_mode & 0o777 == 0o600
    # The next poll finds it current and does nothing.
    calls = len(fake.calls)
    again = make_agent(tmp_path, host(cache_dir), fake).run_once()
    assert again["state"] == "current" and len(fake.calls) == calls


def test_prereleases_count_when_asked() -> None:
    from piceli.hosts.agent import semver

    ordered = ["v1.0.0-alpha", "v1.0.0-alpha.1", "v1.0.0-beta.2", "v1.0.0-beta.11",
               "v1.0.0-rc.1", "v1.0.0", "1.0.1", "v1.10.0"]  # fmt: skip
    keys = [semver(tag) for tag in ordered]
    assert keys == sorted(keys, key=lambda k: k)  # type: ignore[arg-type, return-value]
    assert semver("v1.0") is None and semver("release-1") is None


def test_the_host_never_builds_and_trusts_only_the_cache_key(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    publish(cache_dir, commit_of(1), tag="v1.0.0")
    make_agent(tmp_path, host(cache_dir), fake).run_once()
    (copy,) = fake.commands(" copy ")
    options = {copy[i + 1]: copy[i + 2] for i, a in enumerate(copy) if a == "--option"}
    assert options == {
        "builders": "",
        "max-jobs": "0",
        "substituters": "",
        "trusted-public-keys": PUBLIC,
        "require-sigs": "true",
    }
    assert copy[copy.index("--from") + 1] == f"file://{cache_dir}"
    for call in fake.calls:
        assert "build" not in call and "--no-check-sigs" not in call
        assert "nixos-rebuild" not in " ".join(call)


def test_a_bad_signature_is_refused_before_anything_is_realised(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    stranger = nix.generate_key("cache-1")  # same name, another key
    publish(cache_dir, commit_of(1), tag="v1.0.0", secret=stranger)
    poll = make_agent(tmp_path, host(cache_dir), fake).run_once()
    assert poll == {"at": poll["at"], "state": "rejected",
                    "reason": "host-signature-invalid"}  # fmt: skip
    assert fake.calls == []
    # A pointer signed with the key, to a manifest signed with another one.
    signed = publish(cache_dir, commit_of(2), tag="v1.1.0")
    forged = nix.sign_manifest({**signed, "built_at": "x"}, stranger)
    (cache_dir / "piceli" / "web-1" / f"{commit_of(2)}.json").write_text(
        json.dumps(forged)
    )
    cache = NixCache(PUBLIC, url=f"file://{cache_dir}")
    nix.upload_pointers(cache, forged, ["piceli/web-1/refs/tags/v1.1.0.json"],
                        SECRET, s3_keys=None, now=T0)  # fmt: skip
    poll = make_agent(tmp_path, host(cache_dir), fake).run_once()
    assert poll["state"] == "rejected" and poll["reason"] == "host-signature-invalid"
    assert fake.calls == []
    assert status(tmp_path)["last_attempt"]["stages"][-1] == {
        **status(tmp_path)["last_attempt"]["stages"][-1],
        "name": "verify", "state": "failed", "reason": "host-signature-invalid",
    }  # fmt: skip


def test_a_narinfo_that_does_not_match_the_manifest_is_refused(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    publish(cache_dir, commit_of(1), tag="v1.0.0", nar="sha256:" + "1" * 52)
    poll = make_agent(tmp_path, host(cache_dir), fake).run_once()
    assert poll["reason"] == "host-narinfo-mismatch" and fake.calls == []
    # A narinfo signed with another key fails its signature first.
    publish(cache_dir, commit_of(2), tag="v1.1.0",
            narinfo_secret=nix.generate_key("cache-1"))  # fmt: skip
    poll = make_agent(tmp_path, host(cache_dir), fake).run_once()
    assert poll["reason"] == "host-signature-invalid" and fake.calls == []


def test_another_system_or_attribute_is_refused(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    publish(cache_dir, commit_of(1), branch="main", system="aarch64-linux")
    declared = host(cache_dir, follow=Branch("main"))
    poll = make_agent(tmp_path, declared, fake).run_once()
    assert poll["reason"] == "host-system-mismatch" and fake.calls == []
    publish(cache_dir, commit_of(2), branch="main",
            attribute=".#nixosConfigurations.db-1.config.system.build.toplevel",
            when=T0 + dt.timedelta(minutes=1))  # fmt: skip
    poll = make_agent(tmp_path, declared, fake).run_once()
    assert poll["reason"] == "host-manifest-mismatch" and fake.calls == []


def test_a_failing_check_rolls_back_and_marks_the_build_bad(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    publish(cache_dir, commit_of(1), tag="v1.0.0")
    fake.unit_active = False
    clock = Clock()
    poll = make_agent(tmp_path, host(cache_dir), fake, clock).run_once()
    assert poll["state"] == "rolled-back" and poll["reason"] == "host-check-failed"
    attempt = status(tmp_path)["last_attempt"]
    assert attempt["checks"][0]["state"] == "failed"
    assert attempt["checks"][0]["tries"] == 6  # every 2 s for 10 s
    assert attempt["rollback"]["state"] == "done" and attempt["rollback"]["to"] == OLD
    assert fake.commands("fake-nix-env")[-1][-1] == OLD
    assert fake.commands("switch-to-configuration")[-1][0] == (
        f"{OLD}/bin/switch-to-configuration"
    )
    assert os.readlink(fake.current) == OLD
    assert status(tmp_path)["current"]["store_path"] == OLD
    # Not retried: the build is bad until a newer pointer.
    calls = len(fake.calls)
    fake.unit_active = True
    poll = make_agent(tmp_path, host(cache_dir), fake, clock).run_once()
    assert poll["state"] == "skipped" and poll["reason"] == "host-build-bad"
    assert len(fake.calls) == calls
    # Publishing the same commit again retries it.
    publish(cache_dir, commit_of(1), tag="v1.0.0", when=T0 + dt.timedelta(hours=1))
    poll = make_agent(tmp_path, host(cache_dir), fake, clock).run_once()
    assert poll["state"] == "deployed"
    assert len(status(tmp_path)["history"]) == 2


def test_a_failed_switch_rolls_back(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    publish(cache_dir, commit_of(1), branch="main")
    fake.fail_switch.add(store_path(commit_of(1)))
    poll = make_agent(tmp_path, host(cache_dir, follow=Branch("main")), fake).run_once()
    assert poll["state"] == "rolled-back" and poll["reason"] == "host-switch-failed"
    assert os.readlink(fake.current) == OLD


def test_a_failed_fetch_is_retried_and_not_marked_bad(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    publish(cache_dir, commit_of(1), tag="v1.0.0")
    fake.fail_copy = True
    poll = make_agent(tmp_path, host(cache_dir), fake).run_once()
    assert poll["state"] == "failed" and poll["reason"] == "host-realise-failed"
    assert not fake.commands("fake-nix-env") and os.readlink(fake.current) == OLD
    fake.fail_copy = False
    assert make_agent(tmp_path, host(cache_dir), fake).run_once()["state"] == "deployed"


def test_a_replayed_old_pointer_is_refused(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    declared = host(cache_dir, follow=Branch("main"))
    publish(cache_dir, commit_of(1), branch="main")
    pointer = cache_dir / "piceli" / "web-1" / "refs" / "heads" / "main.json"
    old = pointer.read_bytes()
    assert make_agent(tmp_path, declared, fake).run_once()["state"] == "deployed"
    # Published later, even by a clock that is behind: published_at grows.
    publish(cache_dir, commit_of(2), branch="main", when=T0 - dt.timedelta(days=1))
    assert (
        json.loads(pointer.read_text())["published_at"]
        > json.loads(old)["published_at"]
    )
    assert make_agent(tmp_path, declared, fake).run_once()["state"] == "deployed"
    calls = len(fake.calls)
    pointer.write_bytes(old)  # validly signed, but older than one seen
    poll = make_agent(tmp_path, declared, fake).run_once()
    assert poll["state"] == "rejected" and poll["reason"] == "host-pointer-replayed"
    assert len(fake.calls) == calls
    assert status(tmp_path)["current"]["commit"] == commit_of(2)


def test_a_tag_host_never_moves_to_an_older_version(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    publish(cache_dir, commit_of(1), tag="v1.1.0")
    assert make_agent(tmp_path, host(cache_dir), fake).run_once()["state"] == "deployed"
    (cache_dir / "piceli" / "web-1" / "refs" / "tags" / "v1.1.0.json").unlink()
    publish(cache_dir, commit_of(2), tag="v1.0.5")
    poll = make_agent(tmp_path, host(cache_dir), fake).run_once()
    assert poll == {"at": poll["at"], "state": "idle", "reason": "older"}


def test_a_system_built_from_the_same_commit_is_adopted(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    publish(cache_dir, commit_of(1), tag="v1.0.0")
    fake.current.unlink()
    fake.current.symlink_to(store_path(commit_of(1)))  # the first install
    poll = make_agent(tmp_path, host(cache_dir), fake).run_once()
    assert poll["state"] == "current" and fake.calls == []
    assert status(tmp_path)["current"]["tag"] == "v1.0.0"


# ------------------------------------------------------------------ credentials


def test_read_credentials_come_from_a_root_only_file(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    secret_file = tmp_path / "cache.env"
    secret_file.write_text("AWS_ACCESS_KEY_ID=read-id\nAWS_SECRET_ACCESS_KEY=read-s\n")
    secret_file.chmod(0o644)
    publish(cache_dir, commit_of(1), tag="v1.0.0")
    declared = host(cache_dir, read_credentials_file=str(secret_file))
    poll = make_agent(tmp_path, declared, fake).run_once()
    assert poll["reason"] == "host-credentials-exposed" and fake.calls == []
    secret_file.chmod(0o600)
    secret_file.chmod(0o400)  # sops-nix's default mode
    assert make_agent(tmp_path, declared, fake).run_once()["state"] == "deployed"
    (copy_env,) = [
        env for call, env in zip(fake.calls, fake.envs, strict=True) if "copy" in call
    ]
    assert copy_env["AWS_ACCESS_KEY_ID"] == "read-id"
    assert "read-s" not in json.dumps(status(tmp_path))
    # Only the copy gets them: not the switch, not the checks.
    for call, env in zip(fake.calls, fake.envs, strict=True):
        if "copy" not in call:
            assert "AWS_SECRET_ACCESS_KEY" not in env


def test_an_s3_cache_is_read_with_sigv4_and_read_only_keys(tmp_path: Path) -> None:
    seen: list[Any] = []

    def opener(request: Any, timeout: float = 0) -> Any:
        seen.append(request)
        raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)  # type: ignore[arg-type]

    cache = host_agent.Cache(
        {"kind": "s3", "endpoint": "s3.example.net", "bucket": "bin",
         "region": "eu-1"},
        ("read-id", "read-secret"), opener=opener,
    )  # fmt: skip
    assert cache.get("piceli/web-1/refs/heads/main.json") is None
    (request,) = seen
    assert request.full_url == (
        "https://s3.example.net/bin/piceli/web-1/refs/heads/main.json"
    )
    assert request.get_header("Authorization").startswith(
        "AWS4-HMAC-SHA256 Credential=read-id/"
    )
    assert "read-secret" not in json.dumps(dict(request.header_items()))


# ------------------------------------------------------------------ OTLP


class Collector:
    def __init__(self, up: bool) -> None:
        self.up = up
        self.posts: list[dict[str, Any]] = []
        self.tries = 0

    def __call__(self, request: Any, timeout: float = 0) -> Any:
        assert request.full_url == "https://otel.example.net:4318/v1/logs"
        assert timeout <= 2.0
        self.tries += 1
        if not self.up:
            raise urllib.error.URLError("connection refused")
        self.posts.append(json.loads(request.data))

        class Response:
            status = 200

            def __enter__(self) -> Response:
                return self

            def __exit__(self, *args: Any) -> None:
                return None

        return Response()


def _events(posts: list[dict[str, Any]]) -> list[str]:
    names = []
    for post in posts:
        for resource in post["resourceLogs"]:
            for scope in resource["scopeLogs"]:
                for record in scope["logRecords"]:
                    attrs = {a["key"]: a["value"] for a in record["attributes"]}
                    names.append(attrs["event.name"]["stringValue"])
    return names


def test_a_collector_that_is_down_never_blocks_a_deploy(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    publish(cache_dir, commit_of(1), tag="v1.0.0")
    declared = host(cache_dir, report=HostReport("https://otel.example.net:4318"))
    down = Collector(up=False)
    poll = make_agent(tmp_path, declared, fake, opener=down).run_once()
    assert poll["state"] == "deployed"
    assert down.tries == 1  # one try per run, then the records wait
    report = status(tmp_path)["report"]
    assert report["state"] == "down" and report["reason"] == "host-report-failed"
    queued = json.loads((tmp_path / "state" / "otlp-queue.json").read_text())
    assert len(queued) == report["queued"] > 0
    # The next run sends what waited.
    publish(cache_dir, commit_of(2), tag="v1.1.0")
    up = Collector(up=True)
    assert make_agent(tmp_path, declared, fake, opener=up).run_once()["state"] == (
        "deployed"
    )
    events = _events(up.posts)
    assert events.count("piceli.host.deploy.start") == 2
    assert events.count("piceli.host.deploy.result") == 2
    assert "piceli.host.check" in events and "piceli.host.stage" in events
    assert json.loads((tmp_path / "state" / "otlp-queue.json").read_text()) == []
    resource = up.posts[0]["resourceLogs"][0]["resource"]["attributes"]
    assert {"key": "host.name", "value": {"stringValue": "web-1"}} in resource


def test_a_rollback_is_reported(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    publish(cache_dir, commit_of(1), tag="v1.0.0")
    fake.unit_active = False
    up = Collector(up=True)
    declared = host(cache_dir, report=HostReport("https://otel.example.net:4318"))
    make_agent(tmp_path, declared, fake, opener=up).run_once()
    assert "piceli.host.rollback" in _events(up.posts)


# ------------------------------------------------------------------ declaration


def test_declarations_are_checked(cache_dir: Path) -> None:
    from piceli.hosts.model import HostError

    def code(**overrides: Any) -> str:
        with pytest.raises(HostError) as raised:
            host(cache_dir, **overrides)
        return raised.value.code

    assert code(server="ssh://bad user@h") == "host-invalid"
    assert code(flake="../x") == "host-invalid"
    assert code(follow="v*") == "host-invalid"
    assert code(cache="file:///x") == "host-invalid"
    assert code(checks=["nginx"]) == "host-check-invalid"
    assert code(poll_seconds=5) == "host-invalid"
    assert code(age_identity="Bad Name") == "host-invalid"
    assert code(age_key_file="key.txt") == "host-invalid"
    https = NixCache(PUBLIC, url="https://cache.example.net")
    assert code(cache=https) == "host-invalid"  # tags need a listing
    assert host(cache_dir, cache=https, follow=Branch("main")).build == "web-1"
    for bad in (
        lambda: HostCheck.unit("bad unit"),
        lambda: HostCheck.http("ftp://x"),
        lambda: HostCheck.http("http://user:pw@x/"),
        lambda: HostCheck.command(),
        lambda: HostCheck.unit("a", timeout=0),
    ):
        with pytest.raises(HostError) as raised:
            bad()
        assert raised.value.code == "host-check-invalid"
    with pytest.raises(HostError):
        HostReport("http://otel.example.net:4318")
    assert HostReport("http://127.0.0.1:4318").describe()["endpoint"] == (
        "http://127.0.0.1:4318/v1/logs"
    )
    declared = host(
        cache_dir, flake=".#nixosConfigurations.web-1.config.system.build.toplevel"
    )
    assert declared.attribute == ATTRIBUTE and declared.build == "web-1"
    assert host(cache_dir, flake=".#hosts.edge", build="edge").build == "edge"


def test_the_agent_config_holds_no_secret(cache_dir: Path) -> None:
    cache = NixCache(
        PUBLIC, url="s3://bin?endpoint=s3.example.net&region=eu-1",
        write_credentials="cache-write",
    )  # fmt: skip
    config = agent_config(host(cache_dir, cache=cache))
    assert config["cache"] == {
        "store_url": "s3://bin?endpoint=s3.example.net&region=eu-1",
        "public_key": PUBLIC,
        "objects": {"kind": "s3", "endpoint": "s3.example.net", "bucket": "bin",
                    "region": "eu-1"},
    }  # fmt: skip
    # The read-only keys are a sops-nix secret on the host.
    assert config["read_credentials_file"] == "/run/secrets/piceli/cache.env"
    assert "cache-write" not in json.dumps(config)
    assert agent_config(host(cache_dir))["read_credentials_file"] is None
    declared = host(cache_dir, age_identity="web-1-age")
    assert declared.age_key_file == "/var/lib/sops-nix/key.txt"
    assert "web-1-age" not in json.dumps(agent_config(declared))


def test_the_agent_is_standard_library_only() -> None:
    import ast
    import inspect
    import sys

    imported = {
        (node.module if isinstance(node, ast.ImportFrom) else alias.name).split(".")[0]
        for node in ast.walk(ast.parse(inspect.getsource(host_agent)))
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    imported -= {"piceli", "nix_verify"}  # the verifier travels with it
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}


# ------------------------------------------------------------------ pin, unpin, rollback


def _events_of(collector: Collector) -> list[str]:
    return _events(collector.posts)


def test_a_pin_deploys_an_older_tag_and_holds_it_until_unpinned(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    publish(cache_dir, commit_of(1), tag="v1.0.0")
    publish(cache_dir, commit_of(2), tag="v1.1.0")
    up = Collector(up=True)
    declared = host(cache_dir, report=HostReport("https://otel.example.net:4318"))
    agent = lambda: make_agent(tmp_path, declared, fake, opener=up)  # noqa: E731
    assert agent().run_once()["commit"] == commit_of(2)
    result = agent().control("pin", "v1.0.0")
    assert result["state"] == "pinned" and result["reason"] is None
    assert result["attempt"]["trigger"] == "pin"
    assert [s["name"] for s in result["attempt"]["stages"]] == [
        "resolve", "verify", "realise", "switch", "checks",
    ]  # fmt: skip
    assert os.readlink(fake.current) == store_path(commit_of(1))
    hold = status(tmp_path)["hold"]
    assert hold["kind"] == "pin" and hold["tag"] == "v1.0.0"
    assert hold["commit"] == commit_of(1) and hold["at"]
    # A newer tag is not deployed while pinned; the hold survives each poll.
    publish(cache_dir, commit_of(3), tag="v1.2.0")
    calls = len(fake.calls)
    poll = agent().run_once()
    assert poll["state"] == "held" and poll["reason"] == "host-pinned"
    assert len(fake.calls) == calls and status(tmp_path)["hold"]["tag"] == "v1.0.0"
    events = _events_of(up)
    assert events.count("piceli.host.pin") == 2  # asked, then its result
    assert "piceli.host.deploy.result" in events and "piceli.host.check" in events
    # Unpinned, the next poll follows the tags again (and is started now).
    result = agent().control("unpin")
    assert result["state"] == "unpinned" and result["hold"] is None
    assert result["was"]["tag"] == "v1.0.0"
    assert fake.calls[-1] == [
        "fake-systemctl", "start", "--no-block", "piceli-host-agent.service",
    ]  # fmt: skip
    assert "piceli.host.unpin" in _events_of(up)
    assert agent().run_once()["commit"] == commit_of(3)


def test_a_pin_of_the_running_build_holds_without_a_switch(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    publish(cache_dir, commit_of(1), tag="v1.0.0")
    make_agent(tmp_path, host(cache_dir), fake).run_once()
    calls = len(fake.calls)
    result = make_agent(tmp_path, host(cache_dir), fake).control("pin", "v1.0.0")
    assert result["state"] == "pinned" and result["attempt"] is None
    assert len(fake.calls) == calls


def test_a_pin_is_verified_and_rolled_back_like_any_deploy(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    publish(cache_dir, commit_of(1), tag="v1.1.0")
    make_agent(tmp_path, host(cache_dir), fake).run_once()
    agent = lambda: make_agent(tmp_path, host(cache_dir), fake)  # noqa: E731
    missing = agent().control("pin", "v0.9.0")
    assert missing["state"] == "rejected" and missing["reason"] == "host-pin-not-found"
    assert agent().control("pin", "../x")["reason"] == "host-pin-not-found"
    publish(cache_dir, commit_of(2), tag="v1.0.0", secret=nix.generate_key("cache-1"))
    forged = agent().control("pin", "v1.0.0")
    assert forged["state"] == "rejected"
    assert forged["reason"] == "host-signature-invalid"
    publish(cache_dir, commit_of(3), tag="v0.9.0")
    fake.unit_active = False
    failed = agent().control("pin", "v0.9.0")
    assert failed["state"] == "failed" and failed["reason"] == "host-check-failed"
    assert failed["attempt"]["rollback"]["state"] == "done"
    assert os.readlink(fake.current) == store_path(commit_of(1))
    assert failed["hold"] is None and status(tmp_path)["hold"] is None


def test_a_rollback_returns_to_the_previous_system_and_holds_it(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    agent = lambda: make_agent(tmp_path, host(cache_dir), fake)  # noqa: E731
    publish(cache_dir, commit_of(1), tag="v1.0.0")
    agent().run_once()
    assert agent().control("rollback")["state"] == "rolled-back"  # to OLD
    assert agent().control("unpin")["state"] == "unpinned"
    agent().run_once()
    publish(cache_dir, commit_of(2), tag="v1.1.0")
    agent().run_once()
    calls = len(fake.calls)
    result = agent().control("rollback")
    assert result["state"] == "rolled-back"
    attempt = result["attempt"]
    assert attempt["trigger"] == "rollback"
    assert [s["name"] for s in attempt["stages"]] == ["switch", "checks"]
    assert not any(" copy " in " ".join(c) for c in fake.calls[calls:])  # in store
    assert os.readlink(fake.current) == store_path(commit_of(1))
    body = status(tmp_path)
    assert body["current"]["tag"] == "v1.0.0"
    assert body["previous"]["tag"] == "v1.1.0"
    assert body["hold"]["kind"] == "rollback" and body["hold"]["tag"] == "v1.0.0"
    # The next poll does not deploy again what was rolled away from.
    assert agent().run_once()["state"] == "held"
    assert os.readlink(fake.current) == store_path(commit_of(1))
    state = json.loads((tmp_path / "state" / "state.json").read_text())
    assert not state.get("bad")  # a rollback marks nothing bad


def test_a_rollback_needs_a_previous_system_in_the_store(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    result = make_agent(tmp_path, host(cache_dir), fake).control("rollback")
    assert result["state"] == "rejected"
    assert result["reason"] == "host-rollback-unavailable"
    publish(cache_dir, commit_of(1), tag="v1.0.0")
    make_agent(tmp_path, host(cache_dir), fake).run_once()

    def gone(argv: Sequence[str], **kwargs: Any) -> Any:
        if "path-info" in argv:
            return subprocess.CompletedProcess(list(argv), 1, b"", b"")
        return fake(argv, **kwargs)

    agent = make_agent(tmp_path, host(cache_dir), fake)
    agent.runner = gone
    assert agent.control("rollback")["reason"] == "host-rollback-unavailable"
    assert os.readlink(fake.current) == store_path(commit_of(1))


def test_a_gated_host_switches_only_to_a_build_with_its_verified_pin(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    from piceli.infra.promotion import Promotion

    declared = host(cache_dir, gate=Promotion(input="app", environment="main"))
    assert agent_config(declared)["gate"] == {
        "input": "app", "environment": "main", "source": "app",
    }  # fmt: skip
    assert "gate" not in agent_config(host(cache_dir))  # ungated: unchanged
    publish(cache_dir, commit_of(1), tag="v1.0.0")  # published without the gate
    poll = make_agent(tmp_path, declared, fake).run_once()
    assert poll["reason"] == "host-promotion-missing" and fake.calls == []
    other = {"input": "app", "commit": "e" * 40, "environment": "rc",
             "verified_run": "r1", "verified_at": "2026-10-23T10:00:00Z"}  # fmt: skip
    publish(cache_dir, commit_of(2), tag="v1.1.0", pinned=other,
            when=T0 + dt.timedelta(minutes=1))  # fmt: skip
    poll = make_agent(tmp_path, declared, fake).run_once()
    assert poll["reason"] == "host-promotion-missing" and fake.calls == []
    publish(cache_dir, commit_of(3), tag="v1.2.0",
            pinned={**other, "environment": "main"},
            when=T0 + dt.timedelta(minutes=2))  # fmt: skip
    poll = make_agent(tmp_path, declared, fake).run_once()
    assert poll["state"] == "deployed", poll


def test_the_status_carries_the_pin_of_the_build_it_runs(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    """``current.pinned`` and each attempt's ``pinned``: the deployed
    manifest's; a rollback restores the previous build's; a status written
    before the field is read as before (no ``pinned``)."""
    pin = {"input": "app", "commit": "e" * 40, "environment": "main",
           "verified_run": "r1", "verified_at": "2026-10-23T10:00:00Z"}  # fmt: skip
    agent = lambda: make_agent(tmp_path, host(cache_dir), fake)  # noqa: E731
    publish(cache_dir, commit_of(1), tag="v1.0.0", pinned=pin)
    poll = agent().run_once()
    assert poll["state"] == "deployed", poll
    body = status(tmp_path)
    assert body["current"]["pinned"] == pin
    assert body["last_attempt"]["pinned"] == pin
    publish(
        cache_dir, commit_of(2), tag="v1.1.0", when=T0 + dt.timedelta(minutes=1)
    )  # published without a gate
    assert agent().run_once()["state"] == "deployed"
    body = status(tmp_path)
    assert body["current"]["tag"] == "v1.1.0" and body["current"]["pinned"] is None
    assert body["previous"]["pinned"] == pin
    result = agent().control("rollback")
    assert result["state"] == "rolled-back"
    assert result["attempt"]["pinned"] == pin
    body = status(tmp_path)
    assert body["current"]["tag"] == "v1.0.0" and body["current"]["pinned"] == pin
    # A pin of the running build keeps its pin.
    assert agent().control("pin", "v1.0.0")["state"] == "pinned"
    assert status(tmp_path)["current"]["pinned"] == pin


def test_a_status_without_pinned_still_reads(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    publish(cache_dir, commit_of(1), tag="v1.0.0")
    make_agent(tmp_path, host(cache_dir), fake).run_once()
    state = tmp_path / "state" / "state.json"
    old = json.loads(state.read_text())
    old["current"].pop("pinned", None)
    state.write_text(json.dumps(old))
    publish(cache_dir, commit_of(2), tag="v1.1.0", when=T0 + dt.timedelta(minutes=1))
    assert make_agent(tmp_path, host(cache_dir), fake).run_once()["state"] == "deployed"
    assert status(tmp_path)["previous"].get("pinned") is None
