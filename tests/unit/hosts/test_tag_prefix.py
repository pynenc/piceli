"""Tag prefixes per target (0.24.0): ``Tag("web-v*")`` orders by the version
after its literal prefix.

Two hosts released from one repository (``web-v1.2.3``, ``api-v0.4.0``)
never see each other's tags: in the agent's resolve and ordering, in the
pointer ``dev nix build --publish`` writes, and in the tags ``dev nix gc``
keeps.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from piceli.dev import nix
from piceli.dev.model import DevError
from piceli.hosts.agent import semver, tag_prefix, tag_version
from piceli.hosts.model import Tag
from tests.unit.dev.test_nix import cli_env  # noqa: F401 (fixture)
from tests.unit.hosts.test_agent import (  # noqa: F401 (fixtures)
    OLD,
    Clock,
    FakeHost,
    cache_dir,
    commit_of,
    fake,
    host,
    make_agent,
    publish,
    status,
)


@pytest.mark.parametrize(
    ("pattern", "prefix"),
    [
        ("v*", "v"),
        ("web-v*", "web-v"),
        ("web-*", "web-"),
        ("*", ""),
        ("web-v1.*", "web-v"),  # a pinned major is part of the version
        ("v2*", "v"),
        ("release2-v*", "release2-v"),
        ("web-v1.2.3", "web-v"),
        ("api-[0-9]*", "api-"),
        ("svc?-v*", "svc"),
    ],
)
def test_the_prefix_is_the_literal_part_before_the_version(
    pattern: str, prefix: str
) -> None:
    assert tag_prefix(pattern) == prefix
    assert Tag(pattern).prefix == prefix


@pytest.mark.parametrize(
    ("pattern", "tag", "version"),
    [
        ("web-v*", "web-v1.2.3", "1.2.3"),
        ("web-v*", "web-v1.2.3-rc.1", "1.2.3-rc.1"),
        ("web-*", "web-v1.2.3", "v1.2.3"),  # the optional v of a version
        ("web-*", "web-1.2.3", "1.2.3"),
        ("v*", "v1.2.3", "v1.2.3"),
        ("*", "1.2.3", "1.2.3"),
        ("web-v1.*", "web-v1.4.0", "1.4.0"),
    ],
)
def test_a_tag_compares_as_the_version_after_the_prefix(
    pattern: str, tag: str, version: str
) -> None:
    assert tag_version(pattern, tag) == semver(version)
    assert Tag(pattern).version(tag) == semver(version)


@pytest.mark.parametrize(
    ("pattern", "tag"),
    [
        ("v*", "web-v1.2.3"),  # the glob does not match: another target's tag
        ("web-v*", "api-v1.2.3"),
        ("web-v*", "web-vNext"),  # matches, but no version: ignored
        ("web-*", "web-api-1.2.3"),  # another target nested under the prefix
        ("*", "web-v1.2.3"),  # no prefix declared: the whole tag is the version
        ("web-v1.*", "web-v2.0.0"),
    ],
)
def test_other_tags_have_no_version(pattern: str, tag: str) -> None:
    assert tag_version(pattern, tag) is None
    assert Tag(pattern).version(tag) is None


def test_two_hosts_of_one_repository_follow_their_own_tags(
    tmp_path: Path,
    cache_dir: Path,  # noqa: F811
    fake: FakeHost,  # noqa: F811
) -> None:
    # One build name, three targets' tags in it: each host takes its own.
    for n, tag in enumerate(
        ["web-v1.2.0", "web-v1.10.0", "web-vNext", "api-v9.0.0", "v3.0.0"]
    ):
        publish(cache_dir, commit_of(n + 1), tag=tag)
    web = host(cache_dir, follow=Tag("web-v*"))
    poll = make_agent(tmp_path / "web", web, fake).run_once()
    assert poll["state"] == "deployed" and poll["commit"] == commit_of(2)
    body = status(tmp_path / "web")
    assert body["current"]["tag"] == "web-v1.10.0"
    assert body["ignored_tags"] == ["web-vNext"]  # never api-v9.0.0 or v3.0.0

    link = tmp_path / "plain-system"
    link.symlink_to(OLD)
    plain = make_agent(
        tmp_path / "plain", host(cache_dir), FakeHost(link), Clock()
    ).run_once()
    assert plain["commit"] == commit_of(5)  # Tag("v*"): v3.0.0, not web-v1.10.0
    assert status(tmp_path / "plain")["ignored_tags"] == []


def test_a_prefixed_host_never_moves_to_an_older_version(
    tmp_path: Path,
    cache_dir: Path,  # noqa: F811
    fake: FakeHost,  # noqa: F811
) -> None:
    web = host(cache_dir, follow=Tag("web-v*"))
    publish(cache_dir, commit_of(1), tag="web-v1.10.0")
    assert make_agent(tmp_path, web, fake).run_once()["state"] == "deployed"
    (cache_dir / "piceli/web-1/refs/tags/web-v1.10.0.json").unlink()
    publish(cache_dir, commit_of(2), tag="web-v1.9.0")
    poll = make_agent(tmp_path, web, fake).run_once()
    assert poll["state"] == "idle" and poll["reason"] == "older"


# ------------------------------------------------------------------ dev nix


def _repo(tmp_path: Path) -> tuple[Path, list[str]]:
    """Commits c0..c4: web-v1.2.0, api-v0.1.0, web-v1.10.0, api-v0.2.0 and
    web-v1.9.0 (tagged last: the newest by date, not by version) on c4,
    which also carries api-v0.3.0."""
    from tests.unit.dev.test_pack import git

    source = tmp_path / "app"
    source.mkdir()
    git(source, "init", "-q", "-b", "main")
    git(source, "config", "user.email", "dev@example.com")
    git(source, "config", "user.name", "Dev")
    tags = [["web-v1.2.0"], ["api-v0.1.0"], ["web-v1.10.0"], ["api-v0.2.0"],
            ["web-v1.9.0", "api-v0.3.0", "web-vNext"]]  # fmt: skip
    commits = []
    for index, names in enumerate(tags):
        (source / "f").write_text(str(index))
        git(source, "add", "-A")
        env = {**os.environ, "GIT_COMMITTER_DATE": f"2026-01-0{index + 1}T00:00:00Z"}
        subprocess.run(
            ["git", "-C", str(source), "commit", "-q", "-m", f"c{index}"],
            check=True, env=env,
        )  # fmt: skip
        commits.append(git(source, "rev-parse", "HEAD"))
        for name in names:
            subprocess.run(
                ["git", "-C", str(source), "tag", "-a", "-m", name, name],
                check=True, env=env,
            )  # fmt: skip
    return source, commits


def test_the_published_pointer_names_the_targets_tag(tmp_path: Path) -> None:
    source, commits = _repo(tmp_path)
    head = commits[4]
    assert nix.tag_of(source, head, head) == "api-v0.3.0"  # no pattern: as before
    assert nix.tag_of(source, head, head, "web-v*") == "web-v1.9.0"
    assert nix.tag_of(source, head, head, "api-v*") == "api-v0.3.0"
    assert nix.tag_of(source, "web-v1.9.0", head, "web-v*") == "web-v1.9.0"
    assert nix.tag_of(source, head, head, "shop-v*") is None
    with pytest.raises(DevError) as refused:
        nix.tag_of(source, "api-v0.3.0", head, "web-v*")
    assert refused.value.code == "dev-nix-invalid"


def test_gc_keeps_the_newest_versions_of_each_target(tmp_path: Path) -> None:
    source, c = _repo(tmp_path)
    # Without patterns: the newest tags by date, whatever their target.
    assert set(nix.kept_commits(source, 1, "main")) == {c[4]}
    kept = nix.kept_commits(source, 2, "main", patterns=("web-v*", "api-v*"))
    assert kept == {
        c[2]: ["web-v1.10.0"],
        c[4]: ["web-v1.9.0", "api-v0.3.0", "main"],
        c[3]: ["api-v0.2.0"],
    }
    only_web = nix.kept_commits(source, 1, "main", patterns=("web-v*",))
    assert only_web == {c[2]: ["web-v1.10.0"], c[4]: ["main"]}


def test_gc_command_takes_each_hosts_pattern(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    import piceli.k8s.cli.dev_nix as dev_nix
    from piceli.dev.nix import NixCache
    from tests.unit.dev.test_nix import PUBLIC, _invoke

    source, c = _repo(tmp_path)
    root = tmp_path / "cache"
    for commit in c:  # one manifest per commit, web-1's
        (root / "piceli" / "web-1").mkdir(parents=True, exist_ok=True)
        (root / "piceli" / "web-1" / f"{commit}.json").write_text("{}")
        os.utime(root / "piceli" / "web-1" / f"{commit}.json", (1e6, 1e6))
    monkeypatch.setattr(
        dev_nix, "load_cache", lambda _ref: NixCache(PUBLIC, url=f"file://{root}")
    )
    args = ("dev", "nix", "gc", "--cache", "infra.py:cache", "--repo", str(source),
            "--keep-tags", "1", "--json")  # fmt: skip
    planned = _invoke(*args, "--tag-pattern", "web-v*", "--tag-pattern", "api-v*")
    assert planned.exit_code == 3, planned.stdout + planned.stderr
    plan = json.loads(planned.stdout)
    assert plan["tag_patterns"] == ["web-v*", "api-v*"]
    assert "--tag-pattern 'web-v*' --tag-pattern 'api-v*'" in plan["approve_command"]
    # web-v1.10.0 (c2) and c4 (api-v0.3.0, main) stay; the rest goes.
    assert sorted(plan["delete"]) == sorted(
        f"piceli/web-1/{commit}.json" for commit in (c[0], c[1], c[3])
    )
    bad = _invoke(*args, "--tag-pattern", "web v*")
    assert bad.exit_code == 2 and json.loads(bad.stdout)["reason"] == "dev-nix-invalid"


def test_publish_with_a_tag_pattern_points_the_targets_tag(
    cli_env: dict[str, Any],  # noqa: F811
) -> None:
    import json

    from tests.unit.dev.test_nix import ATTR, _invoke
    from tests.unit.dev.test_pack import git

    for tag in ("api-v2.0.0", "web-v1.4.0", "web-v1.10.0"):
        git(cli_env["app"], "tag", tag)
    args = ("dev", "nix", "build", ATTR, "--cluster", "infra.py:c", "--ref", "HEAD",
            "--publish", "infra.py:cache", "--json")  # fmt: skip
    plain = json.loads(_invoke(*args).stdout)["plan"]
    assert plain["tag"] == "api-v2.0.0"  # the first tag, as before
    planned = _invoke(*args, "--tag-pattern", "web-v*")
    assert planned.exit_code == 3, planned.stdout + planned.stderr
    body = json.loads(planned.stdout)
    assert body["plan"]["tag"] == "web-v1.10.0"
    assert body["plan"]["pointers"] == ["piceli/web-1/refs/tags/web-v1.10.0.json"]
    assert "--tag-pattern 'web-v*'" in body["approve_command"]
    done = _invoke(*args, "--tag-pattern", "web-v*", "--approve", body["digest"])
    assert done.exit_code == 0, done.stdout + done.stderr
    assert (cli_env["cache"] / "piceli/web-1/refs/tags/web-v1.10.0.json").is_file()
