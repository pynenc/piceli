"""A starting train spec: ``piceli train-init`` (0.28.0).

:func:`translate` reads a shell script (the merge-and-test script a project
runs today) and finds what a spec can say: test commands (``cargo``,
``pytest`` and its runners, ``npm``/``pnpm``/``yarn``, ``make``, ``go``,
``just``, ``tox``, ``nox``) become steps, the package lists cargo was given
become a comment (``Impact()`` selects what each change touches), and the
base branch is read from a ``BASE=``-style variable or a ``git`` command.
Git plumbing (merge, rebase, worktrees, tags, pushes) is what the train
itself does; shell glue (``echo``, ``set``, ``cd``, control keywords) is
skipped; every other command line is kept as a ``TODO`` comment with a hint.

:func:`render` writes the spec as Python, in the shape the train guide
uses. Without a script it writes a commented template whose active step
follows the files of the project (``Cargo.toml``, ``pyproject.toml``,
``package.json``, else ``make test``).

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
import re
import shlex
import textwrap
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Words that start shell glue: skipped without a TODO.
GLUE = frozenset(
    [
        "echo",
        "printf",
        "set",
        "shopt",
        "trap",
        "exit",
        "return",
        "local",
        "export",
        "declare",
        "readonly",
        "typeset",
        "cd",
        "pushd",
        "popd",
        "mkdir",
        "rm",
        "rmdir",
        "cp",
        "mv",
        "ln",
        "touch",
        "cat",
        "tee",
        "sleep",
        "date",
        "true",
        "false",
        "read",
        "shift",
        "source",
        ".",
        "unset",
        "wait",
        "test",
        "[",
        "[[",
        "]]",
        "fi",
        "done",
        "esac",
        "then",
        "else",
        "do",
        "}",
        "{",
        "case",
        "for",
        "select",
        "function",
        "sed",
        "grep",
        "awk",
        "tail",
        "head",
        "tr",
        "sort",
        "uniq",
        "wc",
        "cut",
        "basename",
        "dirname",
        "realpath",
        "readlink",
        "mktemp",
        "printenv",
        "id",
        "whoami",
        "hostname",
        "command",
        "type",
        "hash",
        "pwd",
        "umask",
        "ulimit",
    ]
)
#: Leading words that only start a command: dropped before classifying.
PREFIXES = frozenset(
    [
        "if",
        "elif",
        "while",
        "until",
        "then",
        "do",
        "else",
        "!",
        "time",
        "exec",
        "nice",
        "nohup",
    ]
)
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_REDIRECT = frozenset({">", ">>", "<", "<<", ">&", "&>", "<&", ">|", "&>>", "<<<"})
_SPLIT = frozenset({"&&", "||", ";", ";;", "|", "|&", "&", "(", ")"})
_STEP = re.compile(r"[^a-z0-9_.-]+")
_BASE_VAR = re.compile(
    r"\b[A-Za-z_]*BASE[A-Za-z_]*=[\"']?(?:\$\{[A-Za-z_]+:-)?([A-Za-z0-9._/-]+)",
    re.IGNORECASE,
)
_BASE_DEFAULT = re.compile(r"\$\{[A-Za-z_]*BASE[A-Za-z_]*:-([A-Za-z0-9._/-]+)\}")
_BRANCHY = ("main", "master", "develop", "trunk")
_PACKAGES = re.compile(
    r"^\s*(?:export\s+)?[A-Za-z_]*(?:PACKAGES|CRATES|PKGS)[A-Za-z_]*=[\"'(]([^\"')]+)[\"')]",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DraftStep:
    """One step found in the script: its name, its Python code, where."""

    name: str
    code: str
    line: int
    source: str
    cargo: bool = False
    packages: tuple[str, ...] = ()


@dataclass(frozen=True)
class Line:
    """A script line the spec does not run: why, and what to do."""

    line: int
    text: str
    hint: str = ""


@dataclass
class Translation:
    """What :func:`translate` found."""

    base: str = "main"
    base_found: bool = False
    steps: list[DraftStep] = field(default_factory=list)
    todo: list[Line] = field(default_factory=list)
    handled: list[Line] = field(default_factory=list)
    repeated: list[Line] = field(default_factory=list)
    skipped: int = 0
    packages: tuple[str, ...] = ()


def _q(text: str) -> str:
    return json.dumps(text)


def _tuple(items: Sequence[str]) -> str:
    inner = ", ".join(_q(item) for item in items)
    return f"({inner},)" if len(items) == 1 else f"({inner})"


def _list(items: Sequence[str]) -> str:
    return "[" + ", ".join(_q(item) for item in items) + "]"


def _env(env: dict[str, str]) -> str:
    return "{" + ", ".join(f"{_q(k)}: {_q(v)}" for k, v in env.items()) + "}"


def logical_lines(text: str) -> list[tuple[int, str]]:
    """``(first line number, text)`` with ``\\`` continuations joined."""
    out: list[tuple[int, str]] = []
    pending: list[str] = []
    start = 0
    for number, raw in enumerate(text.splitlines(), 1):
        if not pending:
            start = number
        if raw.rstrip().endswith("\\"):
            pending.append(raw.rstrip()[:-1])
            continue
        pending.append(raw)
        out.append((start, " ".join(part.strip() for part in pending).strip()))
        pending = []
    if pending:
        out.append((start, " ".join(part.strip() for part in pending).strip()))
    return out


def _tokens(line: str) -> list[str] | None:
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    try:
        return list(lexer)
    except ValueError:
        return None


def _commands(tokens: list[str]) -> list[list[str]]:
    commands: list[list[str]] = [[]]
    skip = False
    for token in tokens:
        if skip:
            skip = False
            continue
        if token in _SPLIT:
            commands.append([])
        elif token in _REDIRECT:
            if commands[-1] and commands[-1][-1].isdigit():
                commands[-1].pop()
            skip = True
        else:
            commands[-1].append(token)
    return [command for command in commands if command]


def _strip(command: list[str]) -> tuple[list[str], dict[str, str]]:
    """The command without leading keywords, wrappers and assignments."""
    env: dict[str, str] = {}
    words = list(command)
    while words:
        word = words[0]
        if word in PREFIXES:
            words.pop(0)
        elif _ASSIGN.match(word):
            name, _, value = word.partition("=")
            env[name] = value
            words.pop(0)
        elif word == "env":
            words.pop(0)
        elif word == "timeout":
            words.pop(0)
            while words and words[0].startswith("-"):
                flag = words.pop(0)
                if flag in {"-s", "-k", "--signal", "--kill-after"} and words:
                    words.pop(0)
            if words:
                words.pop(0)  # the duration
        else:
            break
    return words, env


def _name(*parts: str) -> str:
    name = _STEP.sub("-", "-".join(parts).lower()).strip("-.")
    return name or "step"


@dataclass(frozen=True)
class _Found:
    kind: str  # step, handled, glue, todo
    name: str = ""
    code: str = ""
    cargo: bool = False
    packages: tuple[str, ...] = ()
    hint: str = ""


def _cargo(words: list[str], env: dict[str, str]) -> _Found:
    sub = words[1] if len(words) > 1 else ""
    if sub in {"test", "check"}:
        packages: list[str] = []
        args: list[str] = []
        rest = words[2:]
        index = 0
        binary = False
        while index < len(rest):
            word = rest[index]
            if binary:
                args.append(word)
            elif word == "--":
                binary = True
                args.append(word)
            elif word in {"-p", "--package"} and index + 1 < len(rest):
                packages.append(rest[index + 1])
                index += 1
            elif word.startswith("--package="):
                packages.append(word.split("=", 1)[1])
            elif word.startswith("-p") and len(word) > 2:
                packages.append(word[2:])
            elif word in {"--workspace", "--all", "--no-fail-fast"}:
                pass
            else:
                args.append(word)
            index += 1
        if sub == "test":
            parts = ["select=Impact()"]
            if args:
                parts.append(f"args={_tuple(args)}")
            if env:
                parts.append(f"env={_env(env)}")
            code = f"CargoTests({', '.join(parts)})"
        else:
            parts = [f"args={_tuple(args)}" if args else "args=()"]
            if env:
                parts.append(f"env={_env(env)}")
            code = f"CargoCheck({', '.join(parts)})"
        return _Found(
            "step", f"cargo-{sub}", code, cargo=True, packages=tuple(packages)
        )
    return _command(_name("cargo", sub), words, env, cargo=True)


def _command(
    name: str, words: list[str], env: dict[str, str], *, cargo: bool = False
) -> _Found:
    parts = [_q(name), _list(words)]
    if env:
        parts.append(f"env={_env(env)}")
    if cargo:
        parts.append("cargo=True")
    return _Found("step", name, f"Step.command({', '.join(parts)})", cargo=cargo)


def _classify(words: list[str], env: dict[str, str]) -> _Found:
    if not words:
        return _Found("glue")
    head = words[0]
    if head.endswith("()") or (len(words) > 1 and words[1] == "()"):
        return _Found("glue")
    if head in GLUE:
        return _Found("glue")
    if head == "git":
        return _Found("handled")
    if any("$" in word or "`" in word for word in words):
        return _Found(
            "todo",
            hint="uses shell variables: write the command with fixed arguments "
            "(steps get PICELI_TRAIN_BASE, PICELI_TRAIN_HEAD, PICELI_TRAIN_BRANCHES)",
        )
    if head == "cargo":
        if len(words) > 1 and words[1].startswith("+"):
            return _command(_name("cargo", *words[2:3]), words, env, cargo=True)
        return _cargo(words, env)
    runner = words
    if (
        head in {"uv", "poetry", "pdm", "hatch", "pipenv", "rye"}
        and "run" in words[1:3]
    ):
        at = words.index("run") + 1
        while at < len(words) and words[at].startswith("-"):
            at += 1
        runner = words[at:]
    elif head.startswith("python") and words[1:3] == ["-m", "pytest"]:
        runner = ["pytest"]
    if runner and runner[0] in {"pytest", "py.test", "tox", "nox"}:
        return _command(_name(runner[0].replace("py.test", "pytest")), words, env)
    if head in {"npm", "pnpm", "yarn", "bun"}:
        sub = words[1] if len(words) > 1 else "install"
        if sub == "run" and len(words) > 2:
            sub = words[2]
        return _command(_name(head, sub), words, env)
    if head in {"make", "gmake", "just"}:
        targets = [
            word
            for index, word in enumerate(words[1:], 1)
            if not word.startswith("-")
            and "=" not in word
            and words[index - 1] not in {"-C", "-f", "-j", "--directory", "--file"}
        ]
        return _command(
            _name("make" if head == "gmake" else head, *targets[:1]), words, env
        )
    if head == "go" and len(words) > 1 and words[1] in {"test", "vet", "build"}:
        return _command(_name("go", words[1]), words, env)
    if head in {"docker", "podman", "buildah", "nix"} and any(
        word in {"build", "push", "buildx"} for word in words[1:3]
    ):
        return _Found(
            "todo",
            name="images",
            hint="images for the cluster: piceli train init --images COMPOSITION "
            "[--repository URL] builds them from the tested commit and offers "
            "them to Controller(prebuilt=True)",
        )
    if head in {"ssh", "scp", "rsync", "kubectl", "helm"}:
        return _Found(
            "todo",
            hint="a deploy: a Step.publish(...) runs on --promote only, or let the "
            "GitOps controller deploy the promoted head",
        )
    return _Found(
        "todo",
        hint="run it as Step.command(NAME, [...]) if it judges the merge, "
        "or Step.publish(NAME, [...]) if it runs after promotion",
    )


def translate(text: str, *, images: bool = False) -> Translation:
    """Read a shell script; ``images``: the spec builds images (docker
    build/push lines are covered, not TODO)."""
    found = Translation()
    seen: dict[str, int] = {}
    names: set[str] = set()
    base_git: str | None = None
    for number, line in logical_lines(text):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not found.base_found:
            match = _BASE_DEFAULT.search(line) or _BASE_VAR.search(line)
            if match and match.group(1) != "tested" and "$" not in match.group(1):
                found.base, found.base_found = match.group(1), True
        packages = _PACKAGES.match(line)
        if packages and not found.packages:
            found.packages = tuple(packages.group(1).split())
        tokens = _tokens(line)
        if tokens is None:
            found.todo.append(Line(number, stripped, "the line does not parse"))
            continue
        kinds: set[str] = set()
        hint = ""
        for command in _commands(tokens):
            words, env = _strip(command)
            item = _classify(words, env)
            if item.kind == "handled" and base_git is None:
                base_git = next(
                    (
                        w.removeprefix("origin/")
                        for w in words
                        if w.removeprefix("origin/") in _BRANCHY
                    ),
                    None,
                )
            if item.kind == "todo" and item.name == "images" and images:
                item = _Found("handled")
            if item.kind == "step":
                key = item.code
                if key in seen:
                    found.repeated.append(
                        Line(number, stripped, f"the same command as line {seen[key]}")
                    )
                    kinds.add("repeated")
                    continue
                seen[key] = number
                name = item.name
                n = 2
                while name in names:
                    name, n = f"{item.name}-{n}", n + 1
                names.add(name)
                code = item.code
                if name != item.name:
                    code = (
                        code.replace(_q(item.name), _q(name), 1)
                        if code.startswith("Step.command")
                        else code.replace("(", f"({_q(name)}, ", 1).replace(", )", ")")
                    )
                found.steps.append(
                    DraftStep(name, code, number, stripped, item.cargo, item.packages)
                )
                if item.packages and not found.packages:
                    found.packages = item.packages
            kinds.add(item.kind)
            if item.kind == "todo" and not hint:
                hint = item.hint
        if "todo" in kinds:
            found.todo.append(Line(number, stripped, hint))
        elif "handled" in kinds and "step" not in kinds:
            found.handled.append(
                Line(number, stripped, "the train merges, tags and pushes")
            )
        elif kinds <= {"glue"}:
            found.skipped += 1
    if not found.base_found and base_git:
        found.base, found.base_found = base_git, True
    return found


# ------------------------------------------------------------------ render


def project_kind(directory: Path) -> str:
    """``rust``, ``python``, ``node`` or ``make``: the template's active step."""
    if (directory / "Cargo.toml").is_file():
        return "rust"
    if (directory / "pyproject.toml").is_file():
        return "python"
    if (directory / "package.json").is_file():
        return "node"
    return "make"


_TEMPLATE_STEPS = {
    "rust": ["CargoCheck(),", "CargoTests(select=Impact()),"],
    "python": ['Step.command("tests", ["uv", "run", "pytest", "-q"]),'],
    "node": ['Step.command("tests", ["npm", "test"]),'],
    "make": ['Step.command("tests", ["make", "test"]),  # TODO: your test command'],
}


@dataclass(frozen=True)
class Options:
    """What the spec is asked to do on top of the script."""

    name: str
    base: str | None = None
    images: str | None = None
    images_attr: str | None = None
    repository: str = ""
    branch: str = "main"
    host: str | None = None
    builds: str | None = None


def _with_hosts(code: str, host: str) -> str:
    return (
        code[:-1] + (", " if not code.endswith("()") else "") + f"hosts=[{_q(host)}])"
    )


def render(
    options: Options,
    found: Translation | None,
    *,
    source: str | None = None,
    kind: str = "make",
    spec_file: str = "train_spec.py",
) -> str:
    """The spec module's text."""
    base = options.base or (found.base if found else "main")
    imports = {"Step", "Train"}
    lines: list[str] = []
    origin = f" from {source}" if source else ""
    lines.append(f'"""The merge train of {options.name}{origin} (piceli train init).')
    lines.append("")
    lines.append(
        f"    piceli train {spec_file} --branches BRANCH...            # test, nothing moves"
    )
    if source:
        lines.append(
            f'    piceli train {spec_file} --branches BRANCH... --compare "{source} {{branches}}"'
        )
    lines.append(
        f"    piceli train {spec_file} --branches BRANCH... --promote  # and move {base}"
    )
    lines.append("")
    lines.append(
        "Each step runs in the merged checkout; exit status 0 passes. The train"
    )
    lines.append("guide (docs/train.md) explains every option.")
    lines.append('"""')
    lines.append("")
    body: list[str] = []
    indent = " " * 8
    if found is None:
        body.append(
            f"{indent}# Each step runs in the merged checkout; exit status 0 passes."
        )
        body.append(f"{indent}# Rust: CargoCheck(), CargoTests(select=Impact()),")
        body.append(
            f'{indent}# Python: Step.command("pytest", ["uv", "run", "pytest", "-q"]),'
        )
        body.append(f'{indent}# Node: Step.command("npm-test", ["npm", "test"]),')
        for code in _TEMPLATE_STEPS[kind]:
            if options.host and code.startswith("Cargo"):
                code = _with_hosts(code.rstrip(","), options.host) + ","
            body.append(f"{indent}{code}")
        if kind == "rust":
            imports |= {"CargoCheck", "CargoTests", "Impact"}
        body.append(
            f'{indent}# On --promote only: Step.publish("publish", ["scripts/publish.sh"]),'
        )
    else:
        if found.packages:
            body.append(
                f"{indent}# The script named the packages {', '.join(found.packages)}; "
                "Impact() tests"
            )
            body.append(
                f'{indent}# the ones each change can affect (select="all": every one).'
            )
        for step in found.steps:
            code = step.code
            if step.cargo and options.host:
                code = _with_hosts(code, options.host)
            if code.startswith("CargoTests"):
                imports |= {"CargoTests", "Impact"}
            elif code.startswith("CargoCheck"):
                imports.add("CargoCheck")
            body.append(f"{indent}# line {step.line}: {_comment(step.source)}")
            body.append(f"{indent}{code},")
        if not found.steps:
            body.append(
                f'{indent}Step.command("tests", ["make", "test"]),  # TODO: no test command found'
            )
        for item in found.todo:
            body.append(f"{indent}# TODO line {item.line}: {_comment(item.text)}")
            if item.hint:
                for part in textwrap.wrap(item.hint, 76):
                    body.append(f"{indent}#   {part}")
    if options.images:
        args = [_q(options.images)]
        if options.repository:
            args += [
                f"repository={_q(options.repository)}",
                f"branch={_q(options.branch)}",
            ]
        body.append(
            f"{indent}# On --promote: build the images the GitOps controller of {options.images}"
        )
        controller = (
            f"{options.images}'s {options.images_attr}"
            if options.images_attr
            else "its cluster"
        )
        body.append(
            f"{indent}# wants, from the tested commit, and offer them to it. The controller of"
        )
        body.append(
            f"{indent}# {controller} must be declared Controller(..., prebuilt=True)."
        )
        body.append(f"{indent}Step.images({', '.join(args)}),")
    lines.append("from piceli.train import " + ", ".join(sorted(imports)))
    lines.append("")
    lines.append("train = Train(")
    lines.append(f"    {_q(options.name)},")
    lines.append(f"    base={_q(base)},")
    lines.append('    tested="tested",')
    if options.host:
        if options.builds:
            lines.append(
                f"    builds={_q(options.builds)},  # declares the build host {options.host}"
            )
        else:
            lines.append(
                f'    # builds="infra.py:cluster",  # TODO: the module declaring the build host {options.host}'
            )
            lines.append(
                "    # (a DevBuilds, or a Cluster with dev=DevBuilds(...)); until then its steps run here."
            )
    lines.append("    steps=[")
    lines.extend(body)
    lines.append("    ],")
    lines.append(")")
    return "\n".join(lines) + "\n"


def _comment(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= 160 else text[:157] + "..."


# ------------------------------------------------------------------- write

_LABEL = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")
_HOST = re.compile(r"[a-z0-9]([-a-z0-9.]{0,61}[a-z0-9])?")


def _invalid(message: str) -> Exception:
    from piceli.train.model import TrainError

    return TrainError("train-init-invalid", message)


def default_name(cwd: Path) -> str:
    """A DNS label from the repository's (else the directory's) name."""
    import subprocess

    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        top = ""
    raw = Path(top).name if top else cwd.name
    label = re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-")[:63].strip("-")
    return label if label and _LABEL.fullmatch(label) else "app"


def _check_options(
    *,
    images: str | None,
    repository: str | None,
    branch: str,
    host: str | None,
    builds: str | None,
) -> tuple[str | None, str | None]:
    composition = attr = None
    if images is not None:
        composition, sep, attr_text = images.partition(":")
        attr = attr_text if sep else None
        if (
            not composition.endswith(".py")
            or composition.startswith("/")
            or ".." in composition.split("/")
        ):
            raise _invalid(
                f"--images {images!r}: the composition module's path in its "
                "repository (infra.py[:ATTR])"
            )
        from piceli.train.model import Step, TrainError

        try:
            Step.images(composition, repository=repository or "", branch=branch)
        except TrainError as error:
            raise _invalid(f"--images: {error}") from None
    elif repository:
        raise _invalid("--repository names the composition's repository: add --images")
    if host is not None and not _HOST.fullmatch(host):
        raise _invalid(f"--host {host!r} is not a build host name")
    if builds is not None and (host is None or ":" not in builds):
        raise _invalid("--builds is MODULE:ATTR and goes with --host")
    return composition, attr


def write_spec(
    path: Path,
    *,
    script: Path | None = None,
    name: str | None = None,
    base: str | None = None,
    images: str | None = None,
    repository: str | None = None,
    branch: str = "main",
    host: str | None = None,
    builds: str | None = None,
    force: bool = False,
    cwd: Path,
) -> dict[str, Any]:
    """Write the spec (``piceli.train-init.v1``); raises ``TrainError``
    (``train-init-exists``, ``train-init-invalid``) before writing anything."""
    from piceli.train.model import TrainError
    from piceli.train.spec import load

    target = path if path.is_absolute() else cwd / path
    composition, attr = _check_options(
        images=images, repository=repository, branch=branch, host=host, builds=builds
    )
    if name is not None and not _LABEL.fullmatch(name):
        raise _invalid(f"--name {name!r} is not a DNS label")
    if base == "tested":
        raise _invalid("the base cannot be the tested branch")
    if target.exists() and not force:
        raise TrainError(
            "train-init-exists", f"{path} exists; add --force to overwrite it"
        )
    found: Translation | None = None
    source: str | None = None
    command: str | None = None
    if script is not None:
        script_path = script if script.is_absolute() else cwd / script
        try:
            text = script_path.read_text(errors="replace")
        except OSError as error:
            raise _invalid(f"--from {script}: {error.strerror or error}") from None
        found = translate(text, images=composition is not None)
        source = str(script)
        command = f"bash {shlex.quote(str(script_path.resolve()))} {{branches}}"
    options = Options(
        name=name or default_name(target.parent if target.parent.is_dir() else cwd),
        base=base,
        images=composition,
        images_attr=attr,
        repository=repository or "",
        branch=branch,
        host=host,
        builds=builds,
    )
    text = render(
        options,
        found,
        source=source,
        kind=project_kind(target.parent if target.parent.is_dir() else cwd),
        spec_file=str(path),
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    body: dict[str, Any] = {
        "schema": "piceli.train-init.v1",
        "path": str(path),
        "train": options.name,
        "base": options.base or (found.base if found else "main"),
        "source": source,
        "compare": command,
        "steps": [
            {"name": s.name, "line": s.line, "code": s.code}
            for s in (found.steps if found else [])
        ],
        "todo": [
            {"line": i.line, "text": i.text, "hint": i.hint}
            for i in (found.todo if found else [])
        ],
        "handled": [
            {"line": i.line, "text": i.text} for i in (found.handled if found else [])
        ],
        "repeated": [
            {"line": i.line, "text": i.text, "hint": i.hint}
            for i in (found.repeated if found else [])
        ],
        "skipped": found.skipped if found else 0,
        "packages": list(found.packages) if found else [],
        "images": composition is not None,
        "host": host,
    }
    try:
        load(f"{target.resolve()}:train", target.resolve().parent)
        body["loads"] = True
    except TrainError as error:
        body["loads"] = False
        body["load_error"] = f"{error} [{error.code}]"
    return body
