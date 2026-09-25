"""Fresh-agent check of the agent skill in ``skills/piceli``.

Copies the skill directory alone to a scratch location and runs its
walkthrough end to end with the installed piceli (the built wheel in CI)
against the in-process fake Kubernetes API of ``piceli.testing``: no cluster,
no Docker, no kubeconfig of the machine. Every command it runs is quoted
verbatim from ``SKILL.md``, so the skill cannot drift from what works:

    install check → render → plan → deploy with the approved hash → status →
    change and deploy inside the owner's policy → diagnose a refusal →
    fail an apply, diagnose it and resume → plan a rollback → roll back with
    the approved hash.

It also checks the SKILL.md front matter (``name``, ``description``,
``metadata.piceli-version`` equal to the installed ``major.minor``).

    python scripts/skill_check.py            # exit 0 when every step passed

``walkthrough`` is reused by the kind test (``tests/integration``) with a
real cluster and ``examples/shop``.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "skills" / "piceli"
SKILL = SKILL_DIR / "SKILL.md"
SHOP = "examples/shop.py:pipeline"


class StepFailed(AssertionError):
    pass


@dataclass
class Session:
    """One run of the walkthrough in a copy of the skill."""

    skill: Path
    env: Mapping[str, str]
    python: str = sys.executable
    #: The pipeline the commands target (SKILL.md shows ``examples/shop.py:pipeline``).
    pipeline: str = SHOP
    text: str = field(default="")
    log: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.text = (self.skill / "SKILL.md").read_text()

    def run(
        self, line: str, expect: int, values: Mapping[str, str] | None = None
    ) -> tuple[str, list[dict[str, Any]]]:
        """Run a command quoted in SKILL.md (placeholders filled) and check its exit."""
        if line not in self.text:
            raise StepFailed(f"SKILL.md does not show the command {line!r}")
        line = line.replace(SHOP, self.pipeline)
        for key, value in (values or {}).items():
            line = line.replace(f"<{key}>", value)
        argv = shlex.split(line)
        if argv[0] == "python":
            argv[0] = self.python
        elif argv[0] == "piceli":
            argv[:1] = [self.python, "-m", "piceli"]
        done = subprocess.run(
            argv,
            cwd=self.skill,
            env={**os.environ, **self.env},
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        self.log.append(f"$ {line}  (exit {done.returncode})")
        self.outputs.append(done.stdout + done.stderr)
        if os.environ.get("SKILL_CHECK_VERBOSE"):
            print(f"$ {line}\n{done.stdout}{done.stderr}", file=sys.stderr)
        if done.returncode != expect:
            raise StepFailed(
                f"{line!r} exited {done.returncode}, expected {expect}\n"
                f"stdout:\n{done.stdout}\nstderr:\n{done.stderr}"
            )
        objects = []
        for text in done.stdout.splitlines():
            if text.startswith("{"):
                try:
                    objects.append(json.loads(text))
                except json.JSONDecodeError:
                    pass
        return done.stdout + done.stderr, objects


def front_matter_problems(text: str, installed: str) -> list[str]:
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if match is None:
        return ["SKILL.md must start with YAML front matter"]
    head = match.group(1)
    problems = []
    for key in ("name: piceli", "description: ", "metadata:", "piceli-version:"):
        if key not in head:
            problems.append(f"front matter lacks {key.strip()!r}")
    found = re.search(r'piceli-version:\s*"([0-9.]+)"', head)
    wanted = ".".join(installed.split(".")[:2])
    if found is None or found.group(1) != wanted:
        problems.append(f"piceli-version must be {wanted!r} (installed {installed})")
    return problems


def snippet_problems(text: str, example: str) -> list[str]:
    """Every line of each ```python block of SKILL.md is in the example file."""
    lines = {line.strip() for line in example.splitlines()}
    return [
        f"SKILL.md python line not in examples/shop.py: {line.strip()!r}"
        for block in re.findall(r"```python\n(.*?)```", text, re.S)
        for line in block.splitlines()
        if line.strip() not in {"", "...,", ")"} and line.strip() not in lines
    ]


def _last(objects: list[dict[str, Any]], key: str) -> str:
    for item in reversed(objects):
        if item.get(key):
            return str(item[key])
    raise StepFailed(f"no {key} in the output")


def walkthrough(
    session: Session,
    *,
    change: Callable[[int], None],
    fail_next_apply: Callable[[], None] | None = None,
    check_image: Callable[[int], None] | None = None,
) -> None:
    """The SKILL.md walkthrough: deploy, change, recover and roll back.

    ``change(n)`` makes the n-th app change (a new release);
    ``fail_next_apply`` makes the next apply fail once (fake API only);
    ``check_image(n)`` asserts that change ``n`` is live (0: the first).
    """

    session.run("python scripts/check_install.py", 0)
    session.run("piceli render examples/shop.py:pipeline", 0)
    _, planned = session.run("python scripts/plan.py examples/shop.py:pipeline", 0)
    first = _last(planned, "combined_hash")
    session.run(
        "python scripts/deploy.py examples/shop.py:pipeline --approve <combined hash>",
        0,
        {"combined hash": first},
    )
    session.run("python scripts/status.py examples/shop.py:pipeline", 0)
    if check_image:
        check_image(0)

    # A change inside the owner's policy (one Deployment updated) runs alone.
    change(1)
    _, result = session.run(
        "python scripts/deploy.py examples/shop.py:pipeline --approve-if-policy", 0
    )
    if _last(result, "approved_by") != "policy":
        raise StepFailed("the policy run was not recorded as approved_by policy")
    if check_image:
        check_image(1)

    # A stale hash is refused; diagnose says to plan again.
    output, _ = session.run(
        "python scripts/deploy.py examples/shop.py:pipeline --approve <combined hash>",
        2,
        {"combined hash": first},
    )
    if "pipeline-plan-changed" not in output:
        raise StepFailed("the stale hash was not refused with pipeline-plan-changed")
    session.run(
        "python scripts/diagnose.py <code>", 0, {"code": "pipeline-plan-changed"}
    )

    if fail_next_apply is not None:
        # An apply that fails once: explain it, then resume the same run.
        change(2)
        fail_next_apply()
        output, result = session.run(
            "python scripts/deploy.py examples/shop.py:pipeline --approve-if-policy", 1
        )
        if "cause:" not in output or _last(result, "stage") != "apply":
            raise StepFailed("the failed apply was not diagnosed at its stage")
        session.run("python scripts/deploy.py examples/shop.py:pipeline --resume", 0)
        if check_image:
            check_image(2)

    # Roll back: plan (exit 3, the hash for the owner), then apply that hash.
    _, planned = session.run("python scripts/rollback.py examples/shop.py:pipeline", 3)
    rollback = _last(planned, "plan_hash")
    session.run(
        "python scripts/rollback.py examples/shop.py:pipeline --approve <plan hash>",
        0,
        {"plan hash": rollback},
    )
    session.run("python scripts/status.py examples/shop.py:pipeline", 0)
    if check_image:
        check_image(1 if fail_next_apply is not None else 0)


def copy_skill(destination: Path) -> Path:
    target = destination / "piceli"
    shutil.copytree(SKILL_DIR, target, ignore=shutil.ignore_patterns("__pycache__"))
    return target


def greeting(skill: Path, index: int) -> None:
    """Change ``GREETING`` in the copied example (an agent's edit)."""
    path = skill / "examples" / "shop.py"
    text = re.sub(
        r'GREETING = "[^"]*"', f'GREETING = "hello {index}"', path.read_text()
    )
    path.write_text(text)
    shutil.rmtree(path.parent / "__pycache__", ignore_errors=True)


def fake_check() -> list[str]:
    """Run the walkthrough against the fake API; return the log."""
    import piceli
    from piceli.testing import TARGET, fake_cluster

    problems = front_matter_problems(SKILL.read_text(), piceli.__version__)
    problems += snippet_problems(
        SKILL.read_text(), (SKILL_DIR / "examples" / "shop.py").read_text()
    )
    if problems:
        raise StepFailed("; ".join(problems))
    with tempfile.TemporaryDirectory() as scratch, fake_cluster() as cluster:
        base = Path(scratch)
        skill = copy_skill(base)
        kubeconfig = cluster.kubeconfig(base / "kubeconfig")
        session = Session(
            skill,
            {
                "SHOP_KUBECONFIG": str(kubeconfig),
                "SHOP_CONTEXT": "fake",
                "SHOP_NAMESPACE": TARGET.namespace,
                "SHOP_TRANSPORT": "loopback-http",
                "SHOP_STATE_DIR": str(base / "state"),
                "SHOP_READINESS_SECONDS": "5",
                # Never the machine's kubeconfig, even by accident.
                "KUBECONFIG": str(base / "missing-kubeconfig"),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )

        def fail_next_apply() -> None:
            cluster.api.inject("PATCH", "/deployments/web", status=403, dry_run=False)

        def check_image(index: int) -> None:
            web = cluster.api.objects[("Deployment", "web")]
            env = web["spec"]["template"]["spec"]["containers"][0]["env"]
            value = {item["name"]: item.get("value") for item in env}["GREETING"]
            expected = "hello" if index == 0 else f"hello {index}"
            if value != expected:
                raise StepFailed(f"GREETING is {value!r}, expected {expected!r}")

        walkthrough(
            session,
            change=lambda index: greeting(skill, index),
            fail_next_apply=fail_next_apply,
            check_image=check_image,
        )
        secret = cluster.api.objects.get(("Secret", "cache-credentials"))
        if secret is None:
            raise StepFailed("the generated secret was not created")
        value = base64.b64decode(secret["data"]["password"]).decode()
        if any(value in output for output in session.outputs):
            raise StepFailed("a command printed the generated secret value")
        return session.log


def main() -> int:
    try:
        log = fake_check()
    except StepFailed as error:
        print(f"skill check failed: {error}", file=sys.stderr)
        return 1
    print("\n".join(log))
    print(f"skill check passed: {len(log)} commands from SKILL.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
