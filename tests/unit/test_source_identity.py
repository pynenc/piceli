"""Git-derived source identity, input locks and build-time drift detection."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from piceli.artifacts import (
    InputsLock,
    InputsSpec,
    SourceDriftError,
    SourceIdentity,
    SourceIdentityError,
    SourceSpec,
    capture_source_identity,
    compare_inputs,
    pinned_sources,
    record_inputs,
    verify_inputs,
)
from piceli.artifacts.source_identity import UnknownSourceError, redact_remote
from piceli.k8s.cli import app

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()


def make_repo(root: Path, name: str = "service-a") -> Path:
    repo = root / name
    (repo / "src").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "src" / "app.py").write_text("print('hello')\n")
    (repo / "README.md").write_text("service\n")
    (repo / ".gitignore").write_text("target/\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "initial")
    return repo


def write_spec(root: Path, body: str) -> Path:
    spec = root / "inputs.toml"
    spec.write_text(body)
    return spec


def identity(repo: Path, **kwargs: object) -> SourceIdentity:
    return capture_source_identity(repo, name="service-a", **kwargs)  # type: ignore[arg-type]


def test_clean_checkout(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    git(repo, "remote", "add", "origin", "https://user:token@example.com/org/a.git")
    found = identity(repo)
    assert found.commit == git(repo, "rev-parse", "HEAD")
    assert found.dirty is False and found.diff_sha256 is None
    assert found.changed_paths == 0
    assert found.repository == "service-a"
    assert found.remote_url == "https://example.com/org/a.git"
    assert SourceIdentity.from_dict(found.to_dict()) == found


def test_dirty_tracked_change_is_hashed_by_content(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "src" / "app.py").write_text("print('changed')\n")
    first = identity(repo)
    assert first.dirty and first.changed_paths == 1
    assert first.diff_sha256 and first.diff_sha256.startswith("sha256:")
    # Staging does not change the working-tree identity.
    git(repo, "add", "src/app.py")
    assert identity(repo).diff_sha256 == first.diff_sha256
    (repo / "src" / "app.py").write_text("print('changed again')\n")
    assert identity(repo).diff_sha256 != first.diff_sha256
    # Restoring the committed bytes makes the checkout clean again.
    git(repo, "checkout", "-q", "HEAD", "--", "src/app.py")
    assert identity(repo).dirty is False


def test_untracked_content_counts_and_ignored_files_do_not(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "target").mkdir()
    (repo / "target" / "out.bin").write_bytes(b"ignored build output")
    assert identity(repo).dirty is False
    (repo / "notes").mkdir()
    (repo / "notes" / "new.txt").write_text("one")
    first = identity(repo)
    assert first.dirty and first.changed_paths == 1
    (repo / "notes" / "new.txt").write_text("two")
    assert identity(repo).diff_sha256 != first.diff_sha256


def test_deleted_file_and_mode_change_are_distinct(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "README.md").unlink()
    deleted = identity(repo)
    assert deleted.dirty
    git(repo, "checkout", "-q", "HEAD", "--", "README.md")
    os.chmod(repo / "README.md", 0o755)
    executable = identity(repo)
    assert executable.dirty and executable.diff_sha256 != deleted.diff_sha256


def test_subpath_limits_the_dirty_scope(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "README.md").write_text("docs only\n")
    scoped = identity(repo, subpath="src")
    assert scoped.dirty is False and scoped.subpath == "src"
    (repo / "src" / "extra.py").write_text("x = 1\n")
    assert identity(repo, subpath="src").dirty is True
    with pytest.raises(SourceIdentityError, match="missing"):
        identity(repo, subpath="nope")


def test_rejects_non_root_and_non_repository(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    with pytest.raises(SourceIdentityError, match="top level"):
        identity(repo / "src")
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(SourceIdentityError):
        identity(plain)
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q")
    with pytest.raises(SourceIdentityError, match="no commit"):
        identity(empty)


def test_ignores_git_redirect_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path)
    other = make_repo(tmp_path, "service-b")
    # Give the two repositories different commits, and read the expected commit
    # before GIT_DIR is set: otherwise the reference itself reads the other repo.
    (other / "README.md").write_text("other service\n")
    git(other, "commit", "-qam", "diverge")
    expected = git(repo, "rev-parse", "HEAD")
    assert expected != git(other, "rev-parse", "HEAD")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    assert identity(repo).commit == expected


def test_redact_remote() -> None:
    assert redact_remote("https://tok@example.com/r.git") == "https://example.com/r.git"
    assert redact_remote("ssh://git:pw@example.com/r") == "ssh://git@example.com/r"
    assert redact_remote("git@example.com:org/r.git") == "git@example.com:org/r.git"


def test_spec_parsing(tmp_path: Path) -> None:
    spec = write_spec(
        tmp_path,
        '[[source]]\nname = "service-a"\npath = "service-a"\n'
        'ref = "main"\nallow_dirty = true\nsubpath = "src"\n',
    )
    parsed = InputsSpec.from_toml(spec)
    assert parsed.sources == (
        SourceSpec("service-a", "service-a", "main", True, "src"),
    )
    assert parsed.base == tmp_path.resolve()
    for body in (
        "",
        '[[source]]\nname = "a"\n',
        '[[source]]\nname = "a"\npath = "x"\nextra = 1\n',
        '[[source]]\nname = "a"\npath = "x"\nsubpath = "../x"\n',
        '[[source]]\nname = "a"\npath = "x"\nref = "--output=x"\n',
        '[[source]]\nname = "a b"\npath = "x"\n',
        '[[source]]\nname = "a"\npath = "x"\n[[source]]\nname = "a"\npath = "y"\n',
        "not toml [",
    ):
        with pytest.raises(SourceIdentityError):
            InputsSpec.from_toml(write_spec(tmp_path, body))


def test_record_enforces_dirty_and_ref_policy(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "tag", "v1")
    spec = InputsSpec((SourceSpec("service-a", "service-a", ref="v1"),), tmp_path)
    lock = record_inputs(spec)
    assert lock.source("service-a").commit == head
    assert InputsLock.from_json(lock.to_json()) == lock

    (repo / "README.md").write_text("more\n")
    with pytest.raises(SourceIdentityError, match="allow_dirty"):
        record_inputs(spec)
    allowed = InputsSpec(
        (SourceSpec("service-a", "service-a", ref=head, allow_dirty=True),), tmp_path
    )
    assert record_inputs(allowed).source("service-a").dirty

    git(repo, "commit", "-q", "-am", "next")
    with pytest.raises(SourceIdentityError, match="requires"):
        record_inputs(spec)
    missing = InputsSpec((SourceSpec("service-a", "service-a", ref="nope"),), tmp_path)
    with pytest.raises(SourceIdentityError, match="not found"):
        record_inputs(missing)


def test_verify_detects_commit_dirty_diff_and_spec_drift(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    spec = InputsSpec(
        (SourceSpec("service-a", "service-a", allow_dirty=True),), tmp_path
    )
    lock = record_inputs(spec)
    assert verify_inputs(spec, lock).ok

    (repo / "README.md").write_text("edit\n")
    drift = verify_inputs(spec, lock)
    assert {item.field for item in drift.drifts} == {"dirty", "diff_sha256"}
    assert drift.to_dict()["state"] == "drift"
    with pytest.raises(SourceDriftError, match="dirty"):
        drift.raise_for_drift()

    dirty_lock = record_inputs(spec)
    (repo / "README.md").write_text("another edit\n")
    assert [item.field for item in verify_inputs(spec, dirty_lock).drifts] == [
        "diff_sha256"
    ]

    git(repo, "commit", "-q", "-am", "next")
    assert "commit" in {item.field for item in verify_inputs(spec, lock).drifts}

    strict = InputsSpec((SourceSpec("service-a", "service-a"),), tmp_path)
    fresh = record_inputs(strict)
    assert [item.field for item in verify_inputs(spec, fresh).drifts] == ["spec_sha256"]


def test_compare_reports_missing_and_extra_sources(tmp_path: Path) -> None:
    make_repo(tmp_path, "service-a")
    make_repo(tmp_path, "service-b")
    both = record_inputs(
        InputsSpec(
            (
                SourceSpec("service-a", "service-a"),
                SourceSpec("service-b", "service-b"),
            ),
            tmp_path,
        )
    )
    one = InputsLock(both.spec_sha256, both.sources[:1])
    fields = {(d.name, d.field) for d in compare_inputs(both, one).drifts}
    assert fields == {("service-b", "present")}
    assert {(d.name, d.field) for d in compare_inputs(one, both).drifts} == fields


def test_pinned_sources_fails_when_checkout_changes_during_build(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    spec = InputsSpec((SourceSpec("service-a", "service-a"),), tmp_path)
    with pinned_sources(spec) as start:
        assert start.source("service-a").dirty is False

    with pytest.raises(SourceDriftError, match="during the build"):
        with pinned_sources(spec):
            (repo / "src" / "app.py").write_text("print('mid-build edit')\n")

    git(repo, "checkout", "-q", "HEAD", "--", "src/app.py")
    lock = record_inputs(spec)
    git(repo, "commit", "-q", "--allow-empty", "-m", "moved")
    with pytest.raises(SourceDriftError, match="differ from the lock"):
        with pinned_sources(spec, lock):
            pass

    # An error from the build itself is not masked by the drift check.
    with pytest.raises(RuntimeError, match="build failed"):
        with pinned_sources(spec):
            (repo / "README.md").write_text("changed\n")
            raise RuntimeError("build failed")


def test_lock_rejects_tampering(tmp_path: Path) -> None:
    make_repo(tmp_path)
    spec = InputsSpec((SourceSpec("service-a", "service-a"),), tmp_path)
    document = record_inputs(spec).to_dict()
    for mutate in (
        lambda d: d.update(revision="other"),
        lambda d: d["sources"][0].update(commit="HEAD"),
        lambda d: d["sources"][0].update(dirty=True),
        lambda d: d["sources"][0].update(unknown=1),
        lambda d: d.update(sources=[]),
    ):
        broken = json.loads(json.dumps(document))
        mutate(broken)
        with pytest.raises((SourceIdentityError, ValueError)):
            InputsLock.from_dict(broken)
    with pytest.raises(SourceIdentityError):
        InputsLock.from_json("[]")


def test_cli_record_and_verify(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    spec = write_spec(tmp_path, '[[source]]\nname = "service-a"\npath = "service-a"\n')
    lock = tmp_path / "out" / "inputs.lock.json"
    runner = CliRunner()

    recorded = runner.invoke(
        app, ["inputs", "record", "--spec", str(spec), "--out", str(lock)]
    )
    assert recorded.exit_code == 0, recorded.output
    payload = json.loads(recorded.stdout)
    assert payload["state"] == "recorded"
    assert payload["sources"][0]["commit"] == git(repo, "rev-parse", "HEAD")
    assert InputsLock.from_json(lock.read_text()).sources[0].name == "service-a"

    verified = runner.invoke(
        app, ["inputs", "verify", "--spec", str(spec), "--lock", str(lock)]
    )
    assert verified.exit_code == 0, verified.output
    assert json.loads(verified.stdout)["state"] == "verified"

    (repo / "README.md").write_text("drift\n")
    drifted = runner.invoke(
        app, ["inputs", "verify", "--spec", str(spec), "--lock", str(lock)]
    )
    assert drifted.exit_code == 1
    drift = json.loads(drifted.stdout)
    assert (drift["state"], drift["reason"]) == ("drift", "source-drift")
    assert "source drift: service-a: dirty" in drifted.stderr

    rejected = runner.invoke(app, ["inputs", "record", "--spec", str(spec)])
    assert rejected.exit_code == 2
    body = json.loads(rejected.stdout)
    assert (body["state"], body["reason"]) == ("rejected", "source-dirty")
    assert "allow_dirty" in body["message"]
    assert "[source-dirty]" in rejected.stderr


def test_nested_repository_contributes_its_commit(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    nested = make_repo(repo, "vendor")
    first = identity(repo)
    assert first.dirty and first.changed_paths == 1
    (nested / "README.md").write_text("nested change\n")
    second = identity(repo)
    assert second.diff_sha256 != first.diff_sha256
    git(nested, "commit", "-q", "-am", "nested")
    assert identity(repo).diff_sha256 not in {first.diff_sha256, second.diff_sha256}


TWO_SOURCES = (
    '[[source]]\nname = "service-a"\npath = "service-a"\n'
    '[[source]]\nname = "service-b"\npath = "service-b"\n'
)


def test_record_and_verify_only_selected_sources(tmp_path: Path) -> None:
    make_repo(tmp_path, "service-a")
    b = make_repo(tmp_path, "service-b")
    spec = InputsSpec(
        (SourceSpec("service-a", "service-a"), SourceSpec("service-b", "service-b")),
        tmp_path,
    )
    full = record_inputs(spec)
    partial = record_inputs(spec, only=["service-a"])
    # Same declaration digest; the selection is recorded and round-trips.
    assert partial.spec_sha256 == full.spec_sha256
    assert partial.only == ("service-a",)
    assert [item.name for item in partial.sources] == ["service-a"]
    assert InputsLock.from_json(partial.to_json()) == partial
    assert "only" not in full.to_dict()

    (b / "README.md").write_text("elsewhere\n")
    # The partial lock verifies its own selection; the full lock verifies
    # the selected source only when asked to.
    assert verify_inputs(spec, partial).ok
    assert verify_inputs(spec, full, only=["service-a"]).ok
    assert not verify_inputs(spec, full).ok
    assert [d.name for d in verify_inputs(spec, full, only=["service-b"]).drifts] == [
        "service-b",
        "service-b",
    ]
    # A build needs every source: a partial lock is drift for the rest.
    with pytest.raises(SourceDriftError):
        with pinned_sources(spec, partial):
            pass

    with pytest.raises(UnknownSourceError) as error:
        record_inputs(spec, only=["service-c"])
    assert error.value.code == "unknown-source"
    with pytest.raises(UnknownSourceError):
        verify_inputs(spec, full, only=["service-c"])
    with pytest.raises(SourceIdentityError):
        InputsLock(full.spec_sha256, full.sources, ("service-b",))


def test_cli_only(tmp_path: Path) -> None:
    make_repo(tmp_path, "service-a")
    b = make_repo(tmp_path, "service-b")
    spec = write_spec(tmp_path, TWO_SOURCES)
    lock = tmp_path / "inputs.lock.json"
    runner = CliRunner()

    recorded = runner.invoke(
        app,
        ["inputs", "record", "--spec", str(spec), "--out", str(lock)],
    )
    assert recorded.exit_code == 0, recorded.output
    (b / "README.md").write_text("elsewhere\n")

    only = runner.invoke(
        app,
        ["inputs", "verify", "--spec", str(spec), "--lock", str(lock)]
        + ["--only", "service-a"],
    )
    assert only.exit_code == 0, only.output
    payload = json.loads(only.stdout)
    assert payload["only"] == ["service-a"]
    assert [item["name"] for item in payload["sources"]] == ["service-a"]

    everything = runner.invoke(
        app, ["inputs", "verify", "--spec", str(spec), "--lock", str(lock)]
    )
    assert everything.exit_code == 1

    partial = runner.invoke(
        app,
        ["inputs", "record", "--spec", str(spec), "--only", "service-a"]
        + ["--only", "service-a"],
    )
    assert partial.exit_code == 0, partial.output
    assert json.loads(partial.stdout)["only"] == ["service-a"]

    unknown = runner.invoke(
        app,
        ["inputs", "verify", "--spec", str(spec), "--lock", str(lock)]
        + ["--only", "nope"],
    )
    assert unknown.exit_code == 2
    assert json.loads(unknown.stdout) == {
        "state": "rejected",
        "reason": "unknown-source",
        "message": "unknown source 'nope'",
        "source": "nope",
    }
