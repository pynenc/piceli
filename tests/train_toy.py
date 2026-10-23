"""A toy project for train tests: a Git repository, a bare remote, branches.

:func:`make_toy` creates, under ``tmp_path``:

- ``remote.git``: a bare remote (``origin``);
- ``repo``: a clone on ``main`` with a train spec (``train_spec.py``), a
  rule check (``scripts/fitness.py`` reading ``rules/forbidden.txt`` and
  ``rules/allow.txt``), a unit check (``scripts/unit.py``: fails on a file
  under ``src/`` holding ``BROKEN``) and a publish script
  (``scripts/publish.py``: writes ``$TOY_PUBLISH_OUT/<tag>``).

With ``cargo=True`` the repository is also a Cargo workspace with two
crates, ``toycore`` and ``toyapp`` (which depends on ``toycore``), and the
spec runs :class:`~piceli.train.CargoCheck` and
:class:`~piceli.train.CargoTests` instead of the unit check.
:data:`FLAKY_TEST` is a test that fails once, then passes: it fails when the
file ``$TOY_FLAKY_MARKER`` does not exist yet, after creating it.

Everything lives in ``tmp_path``; nothing outside it is written.
"""

from __future__ import annotations

import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path

FITNESS = '''\
"""Fail on a forbidden word in a source file that rules/allow.txt does not list."""
import pathlib
import sys

def words(path):
    p = pathlib.Path(path)
    return [w.strip() for w in p.read_text().splitlines() if w.strip()] if p.exists() else []

forbidden, allowed = words("rules/forbidden.txt"), set(words("rules/allow.txt"))
bad = []
for root in ("src", "crates"):
    for path in sorted(pathlib.Path(root).rglob("*")):
        if path.is_file() and "target" not in path.parts and path.as_posix() not in allowed:
            text = path.read_text(errors="replace")
            bad += [f"{path}: uses {w!r}" for w in forbidden if w in text]
print("\\n".join(bad) or "fitness: ok")
sys.exit(1 if bad else 0)
'''

UNIT = """\
import pathlib
import sys

bad = [str(p) for p in sorted(pathlib.Path("src").rglob("*")) if p.is_file() and "BROKEN" in p.read_text()]
print("\\n".join(f"{p}: BROKEN" for p in bad) or "unit: ok")
sys.exit(1 if bad else 0)
"""

PUBLISH = """\
import os
import pathlib

out = pathlib.Path(os.environ["TOY_PUBLISH_OUT"])
out.mkdir(parents=True, exist_ok=True)
tag = os.environ["PICELI_TRAIN_TAG"].replace("/", "_")
(out / tag).write_text(os.environ["PICELI_TRAIN_HEAD"])
print("published", tag)
"""

CORE_LIB = """\
pub fn value() -> i32 {
    1
}

#[cfg(test)]
mod tests {
    #[test]
    fn value_is_one() {
        assert_eq!(super::value(), 1);
    }
}
"""

APP_LIB = """\
pub fn twice() -> i32 {
    toycore::value() * 2
}

#[cfg(test)]
mod tests {
    #[test]
    fn twice_is_two() {
        assert_eq!(super::twice(), 2);
    }
}
"""

FLAKY_TEST = """\
#[test]
fn sometimes() {
    if let Ok(path) = std::env::var("TOY_FLAKY_MARKER") {
        if !std::path::Path::new(&path).exists() {
            std::fs::write(&path, "seen").unwrap();
            panic!("fails the first time only");
        }
    }
}
"""

FAILING_TEST = """\
#[test]
fn always_fails() {
    assert_eq!(toyapp::twice(), 3, "twice is not three");
}
"""


def spec_source(*, cargo: bool, extra_steps: str = "") -> str:
    """The toy's ``train_spec.py``."""
    tests = (
        "CargoCheck(),\n        CargoTests(select=Impact()),"
        if cargo
        else 'Step.command("unit", [sys.executable, "scripts/unit.py"]),'
    )
    return textwrap.dedent(
        f"""\
        import sys

        from piceli.train import CargoCheck, CargoTests, Impact, Step, Train

        train = Train(
            "toy",
            base="main",
            tested="tested",
            steps=[
                {tests}
                Step.command("fitness", [sys.executable, "scripts/fitness.py"]),{extra_steps}
                Step.publish("publish", [sys.executable, "scripts/publish.py"]),
            ],
        )
        """
    )


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout.strip()


@dataclass
class Toy:
    root: Path
    remote: Path
    repo: Path
    cargo: bool

    @property
    def spec(self) -> str:
        return f"{self.repo / 'train_spec.py'}:train"

    def git(self, *args: str) -> str:
        return git(self.repo, *args)

    def write(self, files: dict[str, str | None]) -> None:
        for name, content in files.items():
            path = self.repo / name
            if content is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)

    def commit(self, files: dict[str, str | None], message: str) -> str:
        """Commit ``files`` on the checked-out branch."""
        self.write(files)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def branch(
        self, name: str, files: dict[str, str | None], *, base: str = "main"
    ) -> str:
        """Create branch ``name`` from ``base`` with one commit of ``files``."""
        self.git("checkout", "-q", "-b", name, base)
        try:
            return self.commit(files, f"{name}: change")
        finally:
            self.git("checkout", "-q", "main")

    def rev(self, ref: str) -> str | None:
        done = subprocess.run(
            ["git", "rev-parse", "-q", "--verify", f"{ref}^{{commit}}"],
            cwd=self.repo,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return done.stdout.strip() if done.returncode == 0 else None

    def remote_refs(self) -> dict[str, str]:
        out = git(self.remote, "for-each-ref", "--format=%(refname) %(objectname)")
        refs: dict[str, str] = {}
        for line in out.splitlines():
            name, _, sha = line.partition(" ")
            refs[name] = sha
        return refs


def make_toy(tmp_path: Path, *, cargo: bool = False) -> Toy:
    """The toy repository, its remote and a first commit pushed to ``main``."""
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    git(tmp_path, "init", "-q", "-b", "main", str(repo))
    toy = Toy(tmp_path, remote, repo, cargo)
    toy.git("config", "user.name", "Toy Author")
    toy.git("config", "user.email", "toy@example.invalid")
    toy.git("config", "commit.gpgSign", "false")
    toy.git("remote", "add", "origin", str(remote))
    files: dict[str, str | None] = {
        "train_spec.py": spec_source(cargo=cargo),
        "scripts/fitness.py": FITNESS,
        "scripts/unit.py": UNIT,
        "scripts/publish.py": PUBLISH,
        "rules/forbidden.txt": "forbidden\n",
        "rules/allow.txt": "",
        "src/shared.txt": "one\ntwo\nthree\n",
        "README.md": "# toy\n",
        ".gitignore": "target/\n",
    }
    if cargo:
        files.update(
            {
                "Cargo.toml": '[workspace]\nmembers = ["crates/toycore", "crates/toyapp"]\nresolver = "2"\n',
                "crates/toycore/Cargo.toml": '[package]\nname = "toycore"\nversion = "0.1.0"\nedition = "2021"\n',
                "crates/toycore/src/lib.rs": CORE_LIB,
                "crates/toyapp/Cargo.toml": '[package]\nname = "toyapp"\nversion = "0.1.0"\nedition = "2021"\n\n[dependencies]\ntoycore = { path = "../toycore" }\n',
                "crates/toyapp/src/lib.rs": APP_LIB,
            }
        )
    toy.commit(files, "initial")
    if cargo:
        subprocess.run(
            ["cargo", "generate-lockfile", "--offline"],
            cwd=repo,
            check=True,
            capture_output=True,
            timeout=120,
        )
        toy.commit({}, "lock file")
    toy.git("push", "-q", "origin", "main")
    return toy
