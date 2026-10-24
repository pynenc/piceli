"""Private flake inputs, fetched on the build host before the build (0.26.0).

Which inputs a build covers, how each one is fetched with which credential,
in which order, how a failure is named, and that no token reaches an
argument, the output or the result. ``nix`` is faked; nothing reaches a
network.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from piceli.dev import nix_fetch, nix_inputs
from piceli.dev.model import DevError

TOKEN = "ghp_" + "t0k3n" * 6
REV = "a" * 40
APP_SSH = {"type": "git", "url": "ssh://git@github.com/org/app", "rev": REV,
           "narHash": "sha256-AAAA", "lastModified": 1, "revCount": 3}  # fmt: skip
LOCK = json.dumps(
    {
        "version": 7,
        "root": "root",
        "nodes": {
            "root": {"inputs": {"app": "app", "lib": "lib", "nixpkgs": "nixpkgs"}},
            "app": {"locked": APP_SSH, "inputs": {"shared": "shared"}},
            "lib": {
                "locked": {
                    "type": "github",
                    "owner": "Org",
                    "repo": "lib",
                    "rev": REV,
                    "narHash": "sha256-BBBB",
                }
            },
            "nixpkgs": {
                "locked": {
                    "type": "github",
                    "owner": "NixOS",
                    "repo": "nixpkgs",
                    "rev": REV,
                    "narHash": "sha256-CCCC",
                }
            },
            "shared": {
                "locked": {
                    "type": "git",
                    "rev": REV,
                    "narHash": "sha256-DDDD",
                    "url": "https://gitlab.example.org/org/shared.git",
                }
            },
        },
    }
)


# ------------------------------------------------------------------ matching


@pytest.mark.parametrize(
    "url",
    [
        "git+ssh://git@github.com/org/app",
        "ssh://git@github.com:22/org/app.git",
        "https://github.com/Org/App.git",
        "git@github.com:org/app.git",
        "git+https://user@github.com/org/app/",
    ],
)
def test_every_spelling_of_a_repository_is_one_location(url: str) -> None:
    assert nix_inputs.url_location(url) == ("github.com", "org/app")


def test_forge_inputs_and_local_paths_have_locations() -> None:
    github = {"type": "github", "owner": "Org", "repo": "App"}
    assert nix_inputs.location(github) == ("github.com", "org/app")
    gitlab = {"type": "gitlab", "owner": "group%2Fsub", "repo": "x", "host": "gl.io"}
    assert nix_inputs.location(gitlab) == ("gl.io", "group/sub/x")
    assert nix_inputs.url_location("/srv/git/app.git") == ("", "/srv/git/app")
    assert nix_inputs.location({"type": "tarball", "url": "https://x/y.tgz"}) is None


def test_the_gate_the_sources_and_the_declared_inputs_are_covered_in_order() -> None:
    found = nix_inputs.covered(
        LOCK,
        gate="app",
        sources=[("lib", "https://github.com/org/lib"), ("hosts", "/local/hosts")],
        declared={"shared": "gitlab-token"},
    )
    assert found == [
        {"input": "app", "url": "git+ssh://git@github.com/org/app",
         "credential": "git", "why": "gate"},
        {"input": "lib", "url": "github:Org/lib", "credential": "git",
         "why": "source:lib"},
        {"input": "shared", "url": "git+https://gitlab.example.org/org/shared.git",
         "credential": "gitlab-token", "why": "declared"},
    ]  # fmt: skip
    # nixpkgs is public: nothing to do; a declared name it lacks: nothing.
    assert nix_inputs.covered(LOCK, declared={"gone": "git"}) == []
    assert nix_inputs.covered(None, gate="app") == []
    # A declared credential replaces the default of a gate's input.
    (app,) = nix_inputs.covered(LOCK, gate="app", declared={"app": "app-token"})
    assert (app["credential"], app["why"]) == ("app-token", "gate")


def test_declared_inputs_are_checked() -> None:
    assert nix_inputs.check_declared({"b": "x", "a": "git"}) == {"a": "git", "b": "x"}
    for bad in ({"a b": "git"}, {"app": "Bad Name"}, ["app"]):
        with pytest.raises(ValueError):
            nix_inputs.check_declared(bad)


def test_the_build_host_reads_each_input_from_its_own_checkout() -> None:
    entries = nix_inputs.covered(LOCK, gate="app")
    ((entry, locked),) = nix_inputs.resolve(entries, LOCK)
    assert locked == APP_SSH and entry["credential"] == "git"
    moved = LOCK.replace("org/app", "evil/app")
    with pytest.raises(DevError) as error:
        nix_inputs.resolve(entries, moved)
    assert error.value.code == "infra-nix-input-unreachable"
    assert str(error.value).startswith("app git+ssh://git@github.com/org/app: ")
    with pytest.raises(DevError):
        nix_inputs.resolve([{**entries[0], "input": "other"}], LOCK)
    with pytest.raises(ValueError):
        nix_inputs.check_entries([{"input": "app"}])


def test_urls_never_carry_a_credential() -> None:
    locked = {"type": "git", "url": f"https://x:{TOKEN}@github.com/org/app?a=1"}
    assert nix_inputs.display(locked) == "git+https://github.com/org/app"
    assert nix_inputs.safe_url("ssh://git@github.com/org/app") == (
        "ssh://git@github.com/org/app"
    )


# ------------------------------------------------------------------ credentials


def _token(host: str = "github.com") -> dict[str, str]:
    return {"host": host, "username": "x-access-token", "token": TOKEN}


def test_a_git_ssh_input_is_fetched_over_https_with_the_token() -> None:
    pairs = nix_inputs.resolve(nix_inputs.covered(LOCK, gate="app"), LOCK)
    request, tried = nix_inputs.plan_fetch(pairs, load=lambda _n: _token())
    (item,) = request["inputs"]
    assert item["mode"] == "askpass" and item["token"] == TOKEN
    assert item["locked"]["url"] == "https://github.com/org/app"
    assert item["locked"]["narHash"] == APP_SSH["narHash"]  # the same tree
    assert tried == [{"input": "app", "url": "git+ssh://git@github.com/org/app",
                      "credential": "git", "tried": "credential git over HTTPS"}]  # fmt: skip
    assert TOKEN not in json.dumps(tried)


def test_forge_inputs_get_nix_access_tokens_and_other_hosts_get_nothing() -> None:
    entries = nix_inputs.covered(LOCK, declared={"lib": "git", "shared": "git"})
    pairs = nix_inputs.resolve(entries, LOCK)
    request, tried = nix_inputs.plan_fetch(pairs, load=lambda _n: _token())
    lib, shared = request["inputs"]
    assert (lib["mode"], lib["host"], lib["token"]) == (
        "access-token",
        "github.com",
        TOKEN,
    )
    assert shared["mode"] == "none" and "token" not in shared
    assert tried[1]["tried"] == (
        "credential git is for github.com, not gitlab.example.org, so the host's "
        "own access"
    )
    gitlab = [({"input": "x", "url": "gitlab:o/r", "credential": "g"},
               {"type": "gitlab", "owner": "o", "repo": "r"})]  # fmt: skip
    request, _ = nix_inputs.plan_fetch(gitlab, load=lambda _n: _token("gitlab.com"))
    assert request["inputs"][0]["token"] == f"PAT:{TOKEN}"


def test_a_missing_credential_falls_back_to_the_hosts_own_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PICELI_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    pairs = nix_inputs.resolve(nix_inputs.covered(LOCK, gate="app"), LOCK)
    request, tried = nix_inputs.plan_fetch(pairs)
    assert request["inputs"][0]["mode"] == "none"
    assert tried[0]["tried"] == (
        "credential git is not stored on this host, so the host's own access"
    )


def test_a_git_token_is_stored_private_and_read_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from piceli.infra.machines.model import InfraError

    monkeypatch.setenv("PICELI_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    path = nix_inputs.save_git_token("git", "GitHub.com", "x-access-token", TOKEN)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert nix_inputs.load_git_token("git") == {
        "host": "github.com", "username": "x-access-token", "token": TOKEN,
    }  # fmt: skip
    with pytest.raises(InfraError):
        nix_inputs.save_git_token("git", "not a host", "git", TOKEN)


def test_secrets_git_token_reads_stdin_and_prints_no_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from piceli.k8s.cli import app

    monkeypatch.setenv("PICELI_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    runner = CliRunner()
    argv = ["secrets", "git-token", "git", "--host", "github.com", "--prompt"]
    done = runner.invoke(app, argv, input=TOKEN + "\n")
    assert done.exit_code == 0, done.output
    assert TOKEN not in done.output
    assert json.loads(done.stdout.splitlines()[-1])["kind"] == "git-token"
    assert nix_inputs.load_git_token("git")["token"] == TOKEN
    refused = runner.invoke(app, ["secrets", "git-token", "git", "--host", "h", TOKEN])
    assert refused.exit_code == 2 and TOKEN not in refused.output


# ------------------------------------------------------------------ the fetcher


def _unquote(expr: str) -> str:
    """The JSON inside the fetch expression's Nix string literal."""
    literal = expr[expr.index('fromJSON "') + 10 : expr.rindex('")).outPath')]
    out, chars = [], iter(literal)
    for char in chars:
        out.append(next(chars) if char == "\\" else char)
    return "".join(out)


def test_the_fetch_expression_quotes_any_attribute() -> None:
    locked = {"type": "git", "url": 'https://h/a"b\\c${x}', "rev": REV,
              "shallow": True}  # fmt: skip
    # 0.26.3: exactly as locked (0.26.2 forced shallow = false).
    assert json.loads(_unquote(nix_fetch.fetch_expr(locked))) == locked


class FakeNix:
    """``nix eval`` per input: records argv and env, reads the askpass files."""

    def __init__(self, fail: set[str] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.roots: list[list[str]] = []
        self.fail = fail or set()
        #: ``{locked narHash: what the fetched tree hashes to}``.
        self.hashes: dict[str, str] = {}
        self.lock = ""

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:
        env = kwargs["env"]
        if "--out-link" in argv:  # a GC root of a fetched tree
            self.roots.append(argv[argv.index("--out-link") + 1 :])
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if argv[-3:-1] == ["hash", "path"]:  # 0.26.3: the tree, as locked
            wanted = self.hashes.get(self.calls[-1]["locked"]["narHash"])
            found = wanted or self.calls[-1]["locked"]["narHash"]
            return subprocess.CompletedProcess(argv, 0, f"{found}\n".encode(), b"")
        if "--impure" in argv:  # 0.26.3: the reference lock, stored
            file = argv[-1].split("builtins.readFile ")[1].strip('()"')
            self.lock = Path(file).read_text()
            name = f"/nix/store/{'l' * 32}-flake.lock"
            return subprocess.CompletedProcess(argv, 0, name.encode(), b"")
        locked = json.loads(_unquote(argv[-1]))
        seen = {"argv": argv, "env": dict(env), "locked": locked}
        if env.get("GIT_ASKPASS"):
            script = Path(env["GIT_ASKPASS"])
            seen["askpass_mode"] = stat.S_IMODE(script.stat().st_mode)
            seen["token_mode"] = stat.S_IMODE(
                Path(env["PICELI_ASKPASS_TOKEN"]).stat().st_mode
            )
            seen["answers"] = [
                subprocess.run(
                    [str(script), prompt], env=env, capture_output=True, text=True
                ).stdout
                for prompt in ("Username for 'https://github.com': ", "Password: ")
            ]
        self.calls.append(seen)
        url = str(locked.get("url") or locked.get("repo"))
        if any(name in url for name in self.fail):
            return subprocess.CompletedProcess(
                argv, 128, b"",
                f"fatal: Authentication failed for '{url}' token={TOKEN}\n"
                "error: Failed to fetch git repository\n".encode(),
            )  # fmt: skip
        return subprocess.CompletedProcess(
            argv, 0, f"/nix/store/{'b' * 32}-source".encode(), b"fetched\n"
        )


def _request() -> tuple[dict[str, Any], list[dict[str, str]]]:
    entries = nix_inputs.covered(LOCK, gate="app", declared={"lib": "git"})
    return nix_inputs.plan_fetch(
        nix_inputs.resolve(entries, LOCK), load=lambda _n: _token()
    )


def test_the_fetcher_fetches_each_input_in_order_with_its_own_credential(
    tmp_path: Path,
) -> None:
    request, _ = _request()
    fake = FakeNix()
    environ = {"PATH": str(tmp_path), "HOME": str(tmp_path),
               "GIT_CONFIG_COUNT": "2", "NIX_CONFIG": "cores = 2"}  # fmt: skip
    (tmp_path / "nix").write_text("#!/bin/sh\n")
    (tmp_path / "nix").chmod(0o755)
    said: list[str] = []
    outcome = nix_fetch.fetch(request, runner=fake, emit=said.append, environ=environ)
    assert outcome["state"] == "fetched"
    assert [item["name"] for item in outcome["inputs"]] == ["app", "lib"]
    app, lib = fake.calls
    assert app["locked"]["url"] == "https://github.com/org/app"
    assert app["answers"] == ["x-access-token\n", TOKEN + "\n"]
    assert (app["askpass_mode"], app["token_mode"]) == (0o700, 0o600)
    assert app["env"]["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert (
        app["env"]["GIT_CONFIG_VALUE_0"] == "" and app["env"]["GIT_CONFIG_COUNT"] == "1"
    )
    assert app["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert "NIX_CONFIG" not in app["env"] or TOKEN not in app["env"]["NIX_CONFIG"]
    assert lib["env"]["NIX_CONFIG"] == f"cores = 2\naccess-tokens = github.com={TOKEN}"
    assert "GIT_ASKPASS" not in lib["env"]
    for call in fake.calls:
        assert TOKEN not in " ".join(call["argv"])
        cache = Path(call["env"]["XDG_CACHE_HOME"])
        assert not cache.parent.exists()  # the temporary directory went
    assert TOKEN not in json.dumps(outcome) and TOKEN not in "\n".join(said)


def test_the_fetcher_stops_at_the_first_input_it_cannot_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, _ = _request()
    fake = FakeNix(fail={"org/app"})
    (tmp_path / "nix").write_text("#!/bin/sh\n")
    (tmp_path / "nix").chmod(0o755)
    said: list[str] = []
    outcome = nix_fetch.fetch(
        request, runner=fake, emit=said.append, environ={"PATH": str(tmp_path)}
    )
    assert len(fake.calls) == 1  # lib was never tried
    assert outcome["state"] == "failed" and outcome["input"] == "app"
    assert outcome["detail"] == (
        "fatal: Authentication failed for 'https://github.com/org/app' token=***"
    )
    assert TOKEN not in json.dumps(outcome) and TOKEN not in "\n".join(said)
    monkeypatch.setattr(nix_fetch, "NIX_PATHS", ())
    assert (
        nix_fetch.fetch(request, environ={"PATH": str(tmp_path / "none")})["reason"]
        == "dev-tool-missing"
    )


def test_a_failure_names_the_input_its_url_and_the_credential_before_any_build() -> (
    None
):
    request, tried = _request()
    result = {"state": "failed", "input": "app", "failure": "unreachable",
              "detail": "fatal: Authentication failed", "inputs": []}  # fmt: skip
    seen: dict[str, Any] = {}

    def runner(argv: list[str], **kwargs: Any) -> Any:
        seen["argv"], seen["stdin"] = argv, kwargs["input"]
        return subprocess.CompletedProcess(
            argv, 1,
            (nix_fetch.RESULT_MARKER + json.dumps(result)).encode(),
            f"git said {TOKEN}\n".encode(),
        )  # fmt: skip

    said: list[str] = []
    with pytest.raises(DevError) as error:
        nix_inputs.run_fetcher(["fetcher"], request, tried, runner=runner,
                               say=said.append)  # fmt: skip
    assert error.value.code == "infra-nix-input-unreachable"
    assert str(error.value) == (
        "app git+ssh://git@github.com/org/app (credential git over HTTPS): "
        "fatal: Authentication failed"
    )
    assert TOKEN in seen["stdin"].decode()  # on its stdin only
    assert TOKEN not in " ".join(seen["argv"]) and TOKEN not in "\n".join(said)


def test_the_fetcher_runs_where_the_publisher_runs() -> None:
    from piceli.dev.model import BuildHost

    native = nix_inputs.fetcher_argv(BuildHost.native("mini"), "img")
    assert native[1:3] == ["-I", "-c"] and "def fetch(" in native[3]
    docker = BuildHost.docker("box", image="ghcr.io/x/run@sha256:" + "c" * 64)
    argv = nix_inputs.fetcher_argv(docker, "img")
    assert argv[argv.index("run") : argv.index("run") + 3] == ["run", "--rm", "-i"]
    assert "/nix:/nix:ro" in argv and "NIX_REMOTE=daemon" in argv
    assert "--cap-drop" in argv and os.path.basename(argv[0]) == "docker"


# ------------------------------------------------------------------ both modes

KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjE=\n-----END OPENSSH PRIVATE KEY-----\n"


def test_the_controllers_credential_goes_only_to_the_composition_hosts() -> None:
    entries = [
        item | {"credential": nix_inputs.CONTROLLER}
        for item in nix_inputs.covered(LOCK, gate="app", declared={"shared": "x"})
    ]
    pairs = nix_inputs.resolve(entries, LOCK)
    controller = {"hosts": ["github.com"], "username": "u", "token": TOKEN}
    request, tried = nix_inputs.plan_fetch(pairs, controller=controller)
    app, shared = request["inputs"]
    assert app["mode"] == "askpass" and app["token"] == TOKEN
    assert shared["mode"] == "none" and "token" not in shared
    assert tried[0]["tried"] == "the controller's credential over HTTPS"
    assert tried[1]["tried"].startswith(
        "the controller's credential is for github.com, not gitlab.example.org"
    )
    request, tried = nix_inputs.plan_fetch(pairs[:1])
    assert request["inputs"][0]["mode"] == "none"
    assert tried[0]["tried"].startswith("the controller sent no credential")


def test_a_deploy_key_fetches_git_ssh_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PICELI_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    nix_inputs.save_git_ssh_key("git", "github.com", KEY, "github.com ssh-ed25519 AAAA")
    assert nix_inputs.credential_state("git") == "present"
    assert nix_inputs.credential_state("other") == "missing"
    pairs = nix_inputs.resolve(nix_inputs.covered(LOCK, gate="app"), LOCK)
    request, tried = nix_inputs.plan_fetch(pairs)
    (item,) = request["inputs"]
    assert item["mode"] == "ssh-key" and item["key"] == KEY
    assert item["locked"]["url"] == APP_SSH["url"]  # stays SSH
    assert tried[0]["tried"] == "credential git (a deploy key over SSH)"
    env = nix_fetch.input_env({**item, "index": 0}, str(tmp_path), {})
    command = env["GIT_SSH_COMMAND"]
    key_file = command.split("-i ", 1)[1].split()[0]
    assert stat.S_IMODE(Path(key_file).stat().st_mode) == 0o600
    assert "StrictHostKeyChecking=yes" in command and KEY not in command


def test_secrets_git_ssh_key_stores_a_key_file_and_prints_none_of_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from piceli.k8s.cli import app

    monkeypatch.setenv("PICELI_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    key = tmp_path / "deploy"
    key.write_text(KEY)
    done = CliRunner().invoke(app, ["secrets", "git-ssh-key", "git", "--host",
                                    "github.com", "--file", str(key)])  # fmt: skip
    assert done.exit_code == 0, done.output
    assert "b3BlbnNzaC1rZXktdjE" not in done.output
    assert nix_inputs.load_git_credential("git")["key"] == KEY


def test_secrets_status_says_each_releases_mode_and_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from piceli.infra import NixCache, NixHost, NixRelease, Source, Tag
    from piceli.infra.machines.secrets_status import nix_inputs as rows

    monkeypatch.setenv("PICELI_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    nix_inputs.save_git_token("lib-token", "github.com", "git", TOKEN)
    cache = NixCache("cache-1:" + "A" * 43 + "=", url="file:///tmp/c")
    host = NixHost("web-1", server="ssh://root@web-1.example.org", flake=".#web-1",
                   follow=Tag("web-v*"), cache=cache)  # fmt: skip
    source = Source("https://github.com/org/hosts", name="hosts")
    own = NixRelease(host, source=source, builder="mini", inputs={"lib": "lib-token"})
    sealed = NixRelease(host, source=source, builder="mini", credentials="controller")
    first, second = rows([own, sealed])
    assert first["credentials"] == "host"
    assert first["here"] == {"git": "missing", "lib-token": "present"}
    assert "builder serve --host mini --check" in first["where"]
    assert second["credentials"] == "controller" and "here" not in second
    from piceli.infra import CompositionError

    with pytest.raises(CompositionError):
        NixRelease(host, source=source, builder="mini", credentials="cluster")


def test_each_fetched_tree_gets_a_gc_root_until_the_caller_removes_it(
    tmp_path: Path,
) -> None:
    request, _ = _request()
    fake = FakeNix()
    (tmp_path / "nix").write_text("#!/bin/sh\n")
    (tmp_path / "nix").chmod(0o755)
    roots = tmp_path / "roots"
    roots.mkdir()
    outcome = nix_fetch.fetch({**request, "roots": str(roots)}, runner=fake,
                              environ={"PATH": str(tmp_path)})  # fmt: skip
    assert [item["rooted"] for item in outcome["inputs"]] == [True, True]
    store = f"/nix/store/{'b' * 32}-source"
    assert fake.roots == [[str(roots / "0-app"), store], [str(roots / "1-lib"), store]]
    plain = nix_fetch.fetch(request, runner=FakeNix(), environ={"PATH": str(tmp_path)})
    assert "rooted" not in plain["inputs"][0]


def test_needed_secrets_list_each_releases_input_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from piceli.infra import NixCache, NixHost, NixRelease, Source, Tag
    from piceli.infra.machines.keys import needed_secrets
    from piceli.infra.machines.secrets_status import report

    monkeypatch.setenv("PICELI_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    cache = NixCache("cache-1:" + "A" * 43 + "=", url="file:///tmp/c")
    host = NixHost("web-1", server="ssh://root@web-1.example.org", flake=".#web-1",
                   follow=Tag("web-v*"), cache=cache)  # fmt: skip
    source = Source("https://github.com/org/hosts", name="hosts")
    own = NixRelease(host, source=source, builder="mini",
                     inputs={"app": "app-key", "lib": "app-key"})  # fmt: skip
    found = {s.name: s for s in needed_secrets(own)}
    secret = found["app-key"]
    assert (secret.kind, secret.created_by, secret.build_host) == (
        "git-token",
        "owner",
        "mini",
    )
    assert "flake input app, lib of NixRelease web-1" in secret.purpose
    assert "build host mini" in str(secret.where)
    sealed = NixRelease(host, source=source, builder="mini", credentials="controller")
    names = {s.name: s for s in needed_secrets(sealed)}
    assert names["piceli-build-git"].kind == "cluster-git-secret"
    assert "app-key" not in names

    rows = {r["name"]: r for r in report("infra.py", {"own": own})["secrets"]}
    assert rows["app-key"]["state"] == "pending"
    assert rows["app-key"]["next"].startswith("piceli secrets git-token app-key")
    # Stored here (a deploy key serves too): present, and carried to mini.
    key = "-----BEGIN OPENSSH PRIVATE KEY-----\nx\n-----END OPENSSH PRIVATE KEY-----\n"
    nix_inputs.save_git_ssh_key("app-key", "github.com", key)
    rows = {r["name"]: r for r in report("infra.py", {"own": own})["secrets"]}
    assert rows["app-key"]["state"] == "present"
    assert rows["app-key"]["next"].startswith("piceli secrets backup --only app-key")
    assert "AGE_RECIPIENT_OF_mini" in rows["app-key"]["next"]
    rows = {r["name"]: r for r in report("infra.py", {"s": sealed})["secrets"]}
    assert rows["piceli-build-git"]["state"] == "pending"
    assert "piceli cluster status" in rows["piceli-build-git"]["detail"]


# ------------------------------------------------- 0.26.3: verified, pinned


def test_a_fetched_tree_that_is_not_the_locked_one_is_refused(tmp_path: Path) -> None:
    request, tried = _request()
    fake = FakeNix()
    fake.hashes[APP_SSH["narHash"]] = "sha256-orZ8"
    (tmp_path / "nix").write_text("#!/bin/sh\n")
    (tmp_path / "nix").chmod(0o755)
    outcome = nix_fetch.fetch(request, runner=fake, environ={"PATH": str(tmp_path)})
    assert outcome["state"] == "failed" and outcome["failure"] == "mismatch"
    assert (outcome["expected"], outcome["actual"]) == ("sha256-AAAA", "sha256-orZ8")
    assert "expected sha256-AAAA" in outcome["detail"]
    assert "got sha256-orZ8" in outcome["detail"]
    marker = nix_fetch.RESULT_MARKER + json.dumps(outcome)

    def runner(_argv: list[str], **_kwargs: Any) -> Any:
        return subprocess.CompletedProcess(_argv, 1, marker.encode(), b"")

    with pytest.raises(DevError) as raised:
        nix_inputs.run_fetcher(["fetcher"], request, tried, runner=runner)
    assert raised.value.code == "infra-nix-input-unreachable"
    assert "expected sha256-AAAA, got sha256-orZ8" in str(raised.value)


def test_the_fetch_hashes_the_tree_with_the_lock_attributes(tmp_path: Path) -> None:
    request, _ = _request()
    fake = FakeNix()
    (tmp_path / "nix").write_text("#!/bin/sh\n")
    (tmp_path / "nix").chmod(0o755)
    outcome = nix_fetch.fetch(request, runner=fake, environ={"PATH": str(tmp_path)})
    assert outcome["state"] == "fetched" and "lock" not in outcome
    assert [item["nar_hash"] for item in outcome["inputs"]] == [
        nix_fetch.sri(APP_SSH["narHash"]),
        "sha256-BBBB",
    ]


def test_the_build_gets_a_reference_lock_of_the_fetched_trees(tmp_path: Path) -> None:
    request, _ = _request()
    fake = FakeNix()
    (tmp_path / "nix").write_text("#!/bin/sh\n")
    (tmp_path / "nix").chmod(0o755)
    roots = tmp_path / "roots"
    outcome = nix_fetch.fetch(
        {**request, "lock": LOCK, "roots": str(roots)}, runner=fake,
        environ={"PATH": str(tmp_path)},
    )  # fmt: skip
    stored = f"/nix/store/{'l' * 32}-flake.lock"
    assert outcome["lock"] == stored and outcome["lock_rooted"] is True
    assert [str(roots / "lock"), stored] in fake.roots
    assert nix_inputs.reference_lock_of(outcome) == stored
    pinned = json.loads(fake.lock)["nodes"]
    tree = f"/nix/store/{'b' * 32}-source"
    assert pinned["app"]["locked"] == {
        "type": "path", "path": tree, "narHash": APP_SSH["narHash"],
        "lastModified": APP_SSH["lastModified"], "rev": APP_SSH["rev"],
        "revCount": APP_SSH["revCount"],
    }  # fmt: skip
    original = json.loads(LOCK)["nodes"]
    assert pinned["app"]["inputs"] == original["app"]["inputs"]
    untouched = set(original) - {"app", "lib"}
    assert {key: pinned[key] for key in untouched} == {
        key: original[key] for key in untouched
    }


def test_a_reference_lock_keeps_every_other_node_and_the_inputs() -> None:
    lock = json.dumps({"version": 7, "root": "root", "nodes": {
        "root": {"inputs": {"app": "app", "nixpkgs": "nixpkgs"}},
        "app": {"inputs": {"nixpkgs": ["nixpkgs"]}, "locked": {
            "type": "git", "url": "ssh://git@github.com/org/app", "ref": "main",
            "rev": REV, "revCount": 3, "shallow": True, "lastModified": 1,
            "narHash": "sha256-AAAA"}, "original": {"type": "git", "url": "x"}},
        "nixpkgs": {"locked": {"type": "github", "owner": "o", "repo": "r"}},
    }})  # fmt: skip
    pinned = json.loads(nix_fetch.reference_lock(lock, {"app": "/nix/store/x-source"}))
    assert pinned["nodes"]["app"]["locked"] == {
        "type": "path", "path": "/nix/store/x-source", "narHash": "sha256-AAAA",
        "lastModified": 1, "rev": REV, "revCount": 3,
    }  # fmt: skip
    assert pinned["nodes"]["app"]["inputs"] == {"nixpkgs": ["nixpkgs"]}
    assert pinned["nodes"]["nixpkgs"] == json.loads(lock)["nodes"]["nixpkgs"]
    with pytest.raises(ValueError, match="does not lock lib"):
        nix_fetch.reference_lock(lock, {"lib": "/nix/store/y-source"})


def test_only_a_store_flake_lock_is_a_reference_lock() -> None:
    good = f"/nix/store/{'l' * 32}-flake.lock"
    assert nix_inputs.reference_lock_of({"lock": good}) == good
    for bad in (None, "", "/tmp/flake.lock", "/nix/store/a/b-flake.lock",
                f"/nix/store/{'l' * 32}-source", f"{good} --impure"):  # fmt: skip
        assert nix_inputs.reference_lock_of({"lock": bad}) is None
    assert nix_inputs.reference_lock_of(None) is None
