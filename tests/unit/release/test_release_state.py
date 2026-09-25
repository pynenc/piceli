"""``scripts/release_state.py``: the release workflow's decisions, outside Actions.

The index (PyPI's JSON API) decides whether a version is released, the tag
is pushed only after every file is verified on the index, and every step can
be re-run: an interrupted run releases once and never moves a tag.
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import subprocess
import sys
import threading
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "release_state.py"
WHEEL = "my_pkg-1.2.3-py3-none-any.whl"
SDIST = "my_pkg-1.2.3.tar.gz"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_state", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_state"] = module
    spec.loader.exec_module(module)
    return module


rs = _load()


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    folder = tmp_path / "dist"
    folder.mkdir()
    (folder / WHEEL).write_bytes(b"wheel")
    (folder / SDIST).write_bytes(b"sdist")
    (folder / ".gitignore").write_text("*\n")
    (folder / f"{WHEEL}.publish.attestation").write_text("{}")
    return folder


def _index(*files: str, digest: str = "") -> dict[str, Any]:
    return {
        "urls": [{"filename": name, "digests": {"sha256": digest}} for name in files]
    }


class FakeIndex:
    """``fetch`` for the script: a sequence of answers (dict, None=404, or an error)."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.urls: list[str] = []

    def __call__(self, url: str) -> Mapping[str, Any] | None:
        self.urls.append(url)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _no_sleep(_: float) -> None:
    return None


# ------------------------------------------------------------------ builds


def test_read_build_names_project_version_and_files(dist: Path) -> None:
    build = rs.read_build(dist)
    assert (build.project, build.version, build.tag) == ("my-pkg", "1.2.3", "v1.2.3")
    assert sorted(item.filename for item in build.files) == sorted([SDIST, WHEEL])


def test_read_build_needs_a_wheel_and_an_sdist_of_one_version(dist: Path) -> None:
    (dist / SDIST).unlink()
    with pytest.raises(rs.ReleaseStateError, match="a wheel and an sdist"):
        rs.read_build(dist)
    (dist / "my_pkg-1.2.4.tar.gz").write_bytes(b"other")
    with pytest.raises(rs.ReleaseStateError, match="different projects or versions"):
        rs.read_build(dist)
    with pytest.raises(rs.ReleaseStateError, match="no distribution directory"):
        rs.read_build(dist / "missing")


# ------------------------------------------------------------------ decide


@pytest.fixture
def build(dist: Path) -> Any:
    return rs.read_build(dist)


def test_unreleased_version_is_published_then_tagged(build: Any) -> None:
    plan = rs.decide(build, {}, None, "abc")
    assert (plan.state, plan.publish, plan.finish) == ("unreleased", True, True)
    assert set(plan.missing) == {WHEEL, SDIST}
    assert plan.outputs()["publish"] == "true"
    assert plan.outputs()["tag"] == "v1.2.3"


def test_the_tag_alone_does_not_mean_released(build: Any) -> None:
    # The 0.5.0 failure: tag pushed, upload failed. A re-run must publish.
    plan = rs.decide(build, {}, "abc", "abc")
    assert (plan.publish, plan.finish) == (True, True)


def test_interrupted_upload_publishes_only_what_is_missing(build: Any) -> None:
    plan = rs.decide(build, {WHEEL: ""}, None, "abc")
    assert (plan.state, plan.publish, plan.missing) == ("partial", True, (SDIST,))


def test_published_but_untagged_is_tagged_without_uploading(build: Any) -> None:
    # Killed between upload and tag: the re-run tags once, uploads nothing.
    plan = rs.decide(build, {WHEEL: "", SDIST: ""}, None, "abc")
    assert (plan.state, plan.publish, plan.finish) == ("released", False, True)


def test_released_and_tagged_here_finishes_idempotently(build: Any) -> None:
    plan = rs.decide(build, {WHEEL: "", SDIST: ""}, "abc", "abc")
    assert (plan.publish, plan.finish) == (False, True)


def test_released_from_another_commit_does_nothing(build: Any) -> None:
    # Every later push to main with the same version.
    plan = rs.decide(build, {WHEEL: "", SDIST: ""}, "old", "new")
    assert (plan.publish, plan.finish) == (False, False)


def test_unpublished_version_whose_tag_names_another_commit_is_refused(
    build: Any,
) -> None:
    with pytest.raises(rs.ReleaseStateError, match="points at old, not new"):
        rs.decide(build, {WHEEL: ""}, "old", "new")


def test_a_file_that_differs_on_the_index_is_reported(build: Any) -> None:
    plan = rs.decide(build, {WHEEL: "f" * 64, SDIST: ""}, None, "abc")
    assert plan.mismatched == (WHEEL,)


# ------------------------------------------------------------------- index


def test_published_asks_the_versions_json_api(build: Any) -> None:
    fetch = FakeIndex(_index(WHEEL))
    assert rs.published(build, "https://test.pypi.org/", fetch=fetch) == {WHEEL: ""}
    assert fetch.urls == ["https://test.pypi.org/pypi/my-pkg/1.2.3/json"]
    assert rs.published(build, fetch=FakeIndex(None)) == {}


def test_published_retries_transient_failures_then_gives_up(build: Any) -> None:
    waits: list[float] = []
    fetch = FakeIndex(OSError("502"), OSError("503"), _index(WHEEL))
    assert rs.published(build, fetch=fetch, sleep=waits.append) == {WHEEL: ""}
    assert waits == [5.0, 10.0]
    with pytest.raises(rs.ReleaseStateError, match="after 5 attempts"):
        rs.published(build, fetch=FakeIndex(OSError("down")), sleep=_no_sleep)


def test_verify_waits_for_the_index_to_list_every_file(build: Any) -> None:
    waits: list[float] = []
    fetch = FakeIndex(None, _index(WHEEL), _index(WHEEL, SDIST))
    result = rs.verify(build, fetch=fetch, sleep=waits.append)
    assert result["state"] == "verified" and result["attempts"] == 3
    assert waits == [5.0, 10.0]


def test_verify_fails_when_a_file_never_appears(build: Any) -> None:
    waits: list[float] = []
    with pytest.raises(rs.ReleaseStateError, match=f"still missing .*{SDIST}"):
        rs.verify(build, fetch=FakeIndex(_index(WHEEL)), attempts=8, sleep=waits.append)
    assert waits == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0, 60.0]


class _Handler(http.server.BaseHTTPRequestHandler):
    answers: list[tuple[int, bytes]] = []

    def do_GET(self) -> None:
        status, body = self.answers.pop(0)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        return None


@pytest.fixture
def server() -> Iterator[str]:
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_http_fetch_maps_404_to_none_and_raises_on_5xx(server: str) -> None:
    body = json.dumps(_index(WHEEL)).encode()
    _Handler.answers[:] = [(404, b"{}"), (503, b"{}"), (200, body)]
    assert rs.http_fetch(f"{server}/pypi/x/1/json") is None
    with pytest.raises(OSError):
        rs.http_fetch(f"{server}/pypi/x/1/json")
    assert rs.http_fetch(f"{server}/pypi/x/1/json") == _index(WHEEL)


# -------------------------------------------------------------------- tags


def test_remote_tag_commit_prefers_the_peeled_commit() -> None:
    output = (
        "1111\trefs/tags/v1.2.3\n2222\trefs/tags/v1.2.3^{}\n3333\trefs/tags/v1.2.30\n"
    )
    assert rs.remote_tag_commit(output, "v1.2.3") == "2222"
    assert rs.remote_tag_commit("4444\trefs/tags/v1.2.3\n", "v1.2.3") == "4444"
    assert rs.remote_tag_commit("3333\trefs/tags/v1.2.30\n", "v1.2.3") is None


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, list[str]]:
    """A clone with two commits and a bare ``origin``; no network."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    _git(tmp_path, "init", "-q", str(work))
    for key, value in (
        ("user.name", "Release Bot"),
        ("user.email", "bot@example.com"),
        ("commit.gpgsign", "false"),
        ("tag.gpgsign", "false"),
    ):
        _git(work, "config", key, value)
    _git(work, "remote", "add", "origin", str(origin))
    commits = []
    for message in ("one", "two"):
        _git(work, "commit", "-q", "--allow-empty", "-m", message)
        commits.append(_git(work, "rev-parse", "HEAD"))
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")
    return work, commits


def test_tag_is_created_once_and_never_moved(repo: tuple[Path, list[str]]) -> None:
    work, (first, second) = repo
    assert rs.lookup_tag(work, "v1.2.3") is None
    assert rs.tag(work, "v1.2.3", first).action == "created"
    assert rs.lookup_tag(work, "v1.2.3") == first
    # a re-run of the same release is a no-op
    assert rs.tag(work, "v1.2.3", first).action == "exists"
    # another commit never takes over a published tag
    with pytest.raises(rs.ReleaseStateError, match=f"already points at {first}"):
        rs.tag(work, "v1.2.3", second)
    assert rs.lookup_tag(work, "v1.2.3") == first


# --------------------------------------------------------------------- CLI


def test_killed_between_upload_and_tag_then_rerun_releases_once(
    dist: Path, repo: tuple[Path, list[str]], tmp_path: Path, monkeypatch: Any
) -> None:
    """The M0.1 acceptance scenario, step by step through the CLI."""
    work, (sha, _) = repo
    outputs = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    argv = ["plan", "--dist", str(dist), "--sha", sha, "--git-dir", str(work)]

    # Run 1: nothing on the index -> publish; the upload lands, then the job dies.
    assert rs.main(argv, fetch=FakeIndex(None)) == 0
    assert "publish=true\n" in outputs.read_text()
    assert "finish=true\n" in outputs.read_text()

    # Re-run: the index has both files, no tag yet -> no upload, tag once.
    outputs.write_text("")
    on_index = FakeIndex(_index(WHEEL, SDIST))
    assert rs.main(argv, fetch=on_index) == 0
    assert "publish=false\n" in outputs.read_text()
    assert "finish=true\n" in outputs.read_text()
    verify = ["verify", "--dist", str(dist), "--attempts", "1"]
    assert rs.main(verify, fetch=on_index) == 0
    tag = ["tag", "--tag", "v1.2.3", "--sha", sha, "--git-dir", str(work)]
    assert rs.main(tag) == 0
    assert "tag_action=created\n" in outputs.read_text()

    # A third run changes nothing.
    outputs.write_text("")
    assert rs.main(argv, fetch=on_index) == 0
    assert rs.main(tag) == 0
    assert "publish=false\n" in outputs.read_text()
    assert "tag_action=exists\n" in outputs.read_text()


def test_cli_failure_is_one_json_object_and_exit_1(
    dist: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = rs.main(
        ["verify", "--dist", str(dist), "--attempts", "1"], fetch=FakeIndex(None)
    )
    assert code == 1
    out, err = capsys.readouterr()
    body = json.loads(out)
    assert body["state"] == "failed" and SDIST in body["message"]
    assert err.startswith("::error::")
