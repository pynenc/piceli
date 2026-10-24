"""The short Nix steps of a docker build host: the input fetch, the publisher (0.26.1).

Both run as a ``docker run --rm -i`` of the image the build run uses (the
host's image, else the profile's, else the ``DevBuilds`` builder image),
never of an image named ``None``; as the build run's user (65532), with a
``passwd`` entry and a home it owns, so Nix (and ``ssh`` for a deploy key)
find who they run as; and a step that ends without a result names the
docker or Nix error, without a credential. Nothing here runs docker.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from piceli.dev import nix, nix_fetch, nix_inputs, nix_publish, nix_step
from piceli.dev.model import BuildHost, DevBuilds, DevError, DevProfile

IMAGE = "ghcr.io/example/builder@sha256:" + "b" * 64
HOST_IMAGE = "ghcr.io/example/host@sha256:" + "c" * 64
TOKEN = "ghp_" + "s3cr3t" * 6
TRIED = [{"input": "app", "url": "git+ssh://git@github.com/org/app",
          "credential": "app-read", "tried": "credential app-read (a deploy key over SSH)"}]  # fmt: skip
REQUEST = {"inputs": [{"name": "app", "mode": "ssh-key", "key": TOKEN}]}


def _dev(host: BuildHost) -> DevBuilds:
    return DevBuilds(
        node=None, image=IMAGE, profiles=[DevProfile.for_nix(timeout="2h")],
        hosts=[host],
    )  # fmt: skip


def _image_of(argv: list[str]) -> str:
    return argv[argv.index("--entrypoint") + 2]


# ------------------------------------------------------------------ the image


def test_the_steps_run_the_builder_image_when_neither_host_nor_profile_names_one() -> (
    None
):
    host = BuildHost.docker("nix-host", context="ssh://nix@nix-host", runs=False)
    dev = _dev(host)
    image = nix_step.step_image(host, dev.profile("nix"))
    assert image == IMAGE  # what the build run uses: host, profile, DevBuilds
    assert _image_of(nix_inputs.fetcher_argv(host, image)) == IMAGE
    assert _image_of(nix.publisher_argv(host, image)) == IMAGE
    with_image = BuildHost.docker("nix-host", image=HOST_IMAGE)
    assert nix_step.step_image(with_image, dev.profile("nix")) == HOST_IMAGE


def test_a_missing_image_is_refused_never_run_as_none() -> None:
    host = BuildHost.docker("nix-host", context="ssh://nix@nix-host")
    for call in (
        lambda: nix_inputs.fetcher_argv(host, None),
        lambda: nix.publisher_argv(host, None),
        lambda: nix_inputs.fetch_inputs(host, None),
        lambda: nix_inputs.fetcher_argv(host, "None"),
    ):
        with pytest.raises(DevError) as raised:
            call()
        assert raised.value.code == "dev-nix-invalid"
        assert "nix-host" in str(raised.value) and "image" in str(raised.value)
    # A native host needs none.
    assert nix_inputs.fetcher_argv(BuildHost.native("mini"), None)[1:3] == ["-I", "-c"]


def test_builder_serve_publishes_with_the_builder_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``builder serve --publish`` on a docker host, ``for_nix()`` without an
    image: the fetch and the publisher get the ``DevBuilds`` image."""
    from piceli.infra import nix_releases
    from piceli.k8s.cli import builder

    host = BuildHost.docker("nix-host", context="ssh://nix@nix-host", runs=False)
    seen: dict[str, Any] = {}
    real_fetch, real_push = nix_inputs.fetch_inputs, nix_releases.dev_push

    def fetch(chosen: Any, image: Any) -> Any:
        seen["fetch"] = image
        return real_fetch(chosen, image)

    def push(chosen: Any, image: Any) -> Any:
        seen["push"] = image
        return real_push(chosen, image)

    monkeypatch.setattr(nix_inputs, "fetch_inputs", fetch)
    monkeypatch.setattr(nix_releases, "dev_push", push)
    builder._publisher([], _dev(host), host)
    assert seen == {"fetch": IMAGE, "push": IMAGE}


# ------------------------------------------------------------------ the user


def test_the_steps_run_as_the_build_user_with_a_passwd_entry_and_a_home() -> None:
    host = BuildHost.docker("nix-host", context="ssh://nix@nix-host")
    for argv, marker in (
        (nix_inputs.fetcher_argv(host, IMAGE), "def fetch("),
        (nix.publisher_argv(host, IMAGE), "PICELI-NIX-PUBLISH"),
    ):
        joined = " ".join(argv)
        assert "None" not in argv
        # Root only to add the entry; it drops to 65532 before reading stdin.
        assert "--user 0:0 --cap-drop ALL --cap-add SETUID --cap-add SETGID" in joined
        assert "--security-opt no-new-privileges" in joined
        assert "-v /nix:/nix:ro -e NIX_REMOTE=daemon" in joined
        assert "HOME=/tmp" not in joined  # root's /tmp: Nix then asks passwd
        tail = argv[argv.index("--entrypoint") :]
        assert tail[:5] == ["--entrypoint", "python3", IMAGE, "-I", "-c"]
        assert tail[5] == nix_step.BOOT and marker in tail[6]
        assert f"{nix_step.UID}" == "65532" and f"{nix_step.GID}" == "65532"


def test_the_boot_gives_the_step_a_home_it_owns() -> None:
    """Not root here: no passwd change; HOME becomes a directory the user
    owns (Nix ignores a HOME it does not own and asks passwd instead)."""
    source = (
        "import json, os, sys\n"
        "if __name__ == '__main__':\n"
        "    print(json.dumps({'home': os.environ['HOME'], 'user': "
        "os.environ.get('USER'), 'stdin': sys.stdin.read()}))\n"
    )
    with tempfile.TemporaryDirectory() as scratch:  # the homes go with it
        env = {"PATH": os.environ.get("PATH", ""), "HOME": "/", "TMPDIR": scratch}
        done = subprocess.run(
            [sys.executable, "-I", "-c", nix_step.BOOT, source],
            input=b"request", capture_output=True, env=env, check=False,
        )  # fmt: skip
        assert done.returncode == 0, done.stderr
        found = json.loads(done.stdout)
        assert found["stdin"] == "request"
        home = Path(found["home"])
        assert home.is_dir() and home.stat().st_uid == os.geteuid()
        assert home.parent == Path(scratch).resolve() or home.parent == Path(scratch)
        assert found["user"]
        failing = subprocess.run(
            [sys.executable, "-I", "-c", nix_step.BOOT, "raise SystemExit(7)"],
            capture_output=True, env=env, check=False,
        )  # fmt: skip
        assert failing.returncode == 7  # the step's exit code is kept


# ------------------------------------------------------------------ the message


def _completed(argv: list[str], code: int, stdout: str, stderr: str) -> Any:
    return subprocess.CompletedProcess(argv, code, stdout.encode(), stderr.encode())


def test_a_docker_refusal_is_named_in_the_unreachable_message() -> None:
    def runner(argv: list[str], **_: Any) -> Any:
        return _completed(
            argv, 125, "",
            "docker: invalid reference format: repository name must be lowercase.\n"
            "See 'docker run --help'.\n",
        )  # fmt: skip

    with pytest.raises(DevError) as raised:
        nix_inputs.run_fetcher(["docker"], REQUEST, TRIED, runner=runner)
    message = str(raised.value)
    assert raised.value.code == "infra-nix-input-unreachable"
    assert message.startswith("app git+ssh://git@github.com/org/app (credential ")
    assert "invalid reference format" in message and "exit 125" in message
    assert "the fetch failed" not in message


def test_a_nix_abort_is_named_and_the_credential_redacted() -> None:
    def runner(argv: list[str], **_: Any) -> Any:
        return _completed(
            argv, 134, "",
            f"warning: key {TOKEN}\n"
            "terminate called after throwing an instance of 'nix::Error'\n"
            "  what():  error: cannot determine user's home directory\n",
        )  # fmt: skip

    with pytest.raises(DevError) as raised:
        nix_inputs.run_fetcher(["docker"], REQUEST, TRIED, runner=runner)
    message = str(raised.value)
    assert "error: cannot determine user's home directory" in message
    assert TOKEN not in message and "app git+ssh://" in message


def test_the_fetchers_reason_finds_nixs_error_in_an_abort() -> None:
    lines = [
        "terminate called after throwing an instance of 'nix::Error'",
        "  what():  error: cannot determine user's home directory",
        "Aborted",
    ]
    assert nix_fetch.reason(lines) == "error: cannot determine user's home directory"
    assert nix_fetch.reason(["fatal: repository not found", "error: x"]) == (
        "fatal: repository not found"
    )
    assert nix_fetch.reason(["No user exists for uid 65532"]) == (
        "No user exists for uid 65532"
    )


def test_a_publisher_that_did_not_run_names_the_cause() -> None:
    def runner(argv: list[str], **_: Any) -> Any:
        return _completed(
            argv, 1, "", "error: cannot determine user's home directory\n"
        )

    with pytest.raises(DevError) as raised:
        nix.run_publisher(["docker"], {"signing_key": TOKEN}, runner=runner)
    assert raised.value.code == "dev-nix-publish-failed"
    assert "cannot determine user's home directory" in str(raised.value)


def test_a_publisher_that_reported_a_failure_names_nixs_error() -> None:
    def runner(argv: list[str], **_: Any) -> Any:
        result = {"state": "failed", "reason": "dev-nix-publish-failed"}
        return _completed(
            argv, 1, nix_publish.RESULT_MARKER + json.dumps(result),
            f"key {TOKEN}\nerror: cannot determine user's home directory\n",
        )  # fmt: skip

    with pytest.raises(DevError) as raised:
        nix.run_publisher(["docker"], {"signing_key": TOKEN}, runner=runner)
    assert str(raised.value) == (
        "signing or pushing the closure failed (dev-nix-publish-failed: "
        "error: cannot determine user's home directory)"
    )
