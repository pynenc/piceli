"""Deploy committed sources: ``piceli deploy TARGET --ref [SOURCE=]REV``.

Without ``--ref`` a deploy reads its sources from the checkouts on disk (the
working tree, dirty or not). With ``--ref`` each named source is read from a
commit instead:

1. **Resolve.** ``REV`` (a branch, tag or commit) is resolved in the source's
   repository to a full commit SHA. The SHA, not ``REV``, is what the combined
   plan hash covers, so approving a plan for commit X can never apply commit Y
   (a branch that moved since ``--plan`` changes the hash). A bare ``--ref
   REV`` is allowed when every declared source is the same repository.
2. **Materialise.** ``git worktree add --detach`` checks the commit out into a
   private temporary directory. A worktree is a real checkout: the
   repository's configured filters run (Git LFS files have their content,
   ``export-ignore`` attributes do not drop files as ``git archive`` would),
   and the source identity reads it with plain git (``dirty: false``, the
   commit). Hooks are disabled. Submodules are not checked out; declare each
   as its own source. One worktree serves every source of the same
   repository and commit.
3. **Read from the commit.** Every file the ``inputs`` and ``build`` stages
   read inside a pinned repository (the source contexts, ``build.toml``,
   ``inputs.toml``, a lock, a Dockerfile, contexts relative to the pipeline
   module) comes from the worktree. Sources without ``--ref`` stay on disk.
4. **The model runs from the working tree.** The pipeline module is imported
   from disk (its kubeconfig, state directory and secrets are local files).
   When the module's directory is inside a pinned repository, its tracked
   Python files (the module and the ``.py`` files beside it) must equal the
   commit, otherwise the deploy is refused with ``deploy-ref-model-differs``:
   the release is then exactly the commit's model and the commit's images.
5. **Clean up.** :meth:`SourceCheckouts.close` removes the worktrees (also
   after a failure or an interrupt). A process killed with ``SIGKILL`` leaves
   a directory that ``git worktree prune`` removes.

Importing this module runs nothing.
"""

from __future__ import annotations

import dataclasses
import re
import shutil
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from piceli.pipeline.errors import PipelineError

if TYPE_CHECKING:
    from piceli.artifacts.build_spec import BuildSpec
    from piceli.artifacts.source_identity import InputsLock, InputsSpec
    from piceli.pipeline.model import Build, Pipeline

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_REV = re.compile(r"[^\s\x00-\x1f\x7f:?*\[\\]{1,256}")
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
#: Checkouts can be large; a worktree add gets this long.
CHECKOUT_SECONDS = 600.0


@dataclass(frozen=True)
class RefRequest:
    """One ``--ref`` value: ``source`` is ``None`` for a bare ``--ref REV``."""

    source: str | None
    rev: str

    def __str__(self) -> str:
        return self.rev if self.source is None else f"{self.source}={self.rev}"


def parse_refs(values: Iterable[str]) -> tuple[RefRequest, ...]:
    """Parse ``--ref`` values (``SOURCE=REV`` or ``REV``).

    :raises PipelineError: ``deploy-ref-invalid`` for a malformed value or a
        source named twice, ``deploy-ref-ambiguous`` for a bare ``REV`` given
        with other values.
    """
    requests: list[RefRequest] = []
    for value in values:
        name, sep, rev = value.partition("=")
        if not sep:
            name, rev = "", value
        if sep and not _NAME.fullmatch(name):
            raise PipelineError(
                "deploy-ref-invalid",
                f"--ref {value!r}: the source name must be 1-128 characters of "
                "[A-Za-z0-9._-]",
            )
        if not _REV.fullmatch(rev) or rev.startswith("-") or ".." in rev:
            raise PipelineError(
                "deploy-ref-invalid",
                f"--ref {value!r}: give a branch, tag or commit (no ranges, "
                "spaces or leading '-')",
            )
        requests.append(RefRequest(name or None, rev))
    if any(item.source is None for item in requests) and len(requests) > 1:
        raise PipelineError(
            "deploy-ref-ambiguous",
            "a bare --ref REV cannot be combined with other --ref values; name "
            "each source as --ref SOURCE=REV",
        )
    names = [item.source for item in requests if item.source is not None]
    for name in sorted({item for item in names if names.count(item) > 1}):
        raise PipelineError(
            "deploy-ref-invalid", f"--ref names source {name!r} more than once"
        )
    return tuple(requests)


@dataclass
class SourceCheckout:
    """One source pinned to a commit, and where that commit is checked out."""

    name: str
    repository: Path
    rev: str
    commit: str
    root: Path | None = None

    def describe(self) -> dict[str, Any]:
        return {"ref": self.rev, "commit": self.commit}


def _git(repo: Path, *args: str, timeout: float = 30.0, check: bool = True) -> bytes:
    from piceli.artifacts.source_identity import _git as run_git

    return run_git(repo, *args, timeout=timeout, check=check)


def _toplevel(path: Path) -> Path | None:
    from piceli.artifacts.source_identity import SourceIdentityError

    if not path.is_dir():
        return None
    try:
        top = _git(path, "rev-parse", "--show-toplevel", check=False)
    except SourceIdentityError as error:
        if error.code == "git-unavailable":
            raise PipelineError("git-unavailable", str(error)) from None
        return None
    text = top.decode(errors="replace").strip()
    return Path(text).resolve() if text else None


def _load_inputs(build: Build) -> InputsSpec | None:
    from piceli.pipeline.runner import classify

    try:
        return build.load().load_inputs()
    except PipelineError:
        raise
    except OSError as error:
        raise PipelineError("pipeline-invalid", str(error)) from None
    except Exception as error:
        raise classify(error) from None


def declared_sources(pipeline: Pipeline) -> dict[str, Path]:
    """Every source the pipeline's builds declare: name → resolved path on disk."""
    sources: dict[str, Path] = {}
    for build in pipeline.builds:
        inputs = _load_inputs(build)
        if inputs is None:
            continue
        for source in inputs.sources:
            path = inputs.declared(source).resolve()
            if sources.setdefault(source.name, path) != path:
                raise PipelineError(
                    "deploy-ref-ambiguous",
                    f"two builds declare source {source.name!r} at different paths",
                )
    return sources


def resolve_refs(
    pipeline: Pipeline, requests: Sequence[RefRequest]
) -> dict[str, SourceCheckout]:
    """Resolve each request to a full commit SHA (name → checkout, not materialised).

    :raises PipelineError: ``deploy-ref-source-unknown``, ``deploy-ref-ambiguous``,
        ``deploy-ref-unknown``, ``source-not-git`` or ``git-unavailable``.
    """
    if shutil.which("git") is None:
        raise PipelineError("git-unavailable", "git executable not found on PATH")
    sources = declared_sources(pipeline)
    wanted: dict[str, str] = {}
    for request in requests:
        if request.source is None:
            tops = {_toplevel(path) for path in sources.values()}
            if not sources or len(tops) != 1:
                raise PipelineError(
                    "deploy-ref-ambiguous",
                    "a bare --ref REV needs a pipeline whose sources are one "
                    f"repository; it declares {sorted(sources) or 'no source'}: "
                    "use --ref SOURCE=REV",
                )
            wanted.update(dict.fromkeys(sources, request.rev))
        elif request.source not in sources:
            raise PipelineError(
                "deploy-ref-source-unknown",
                f"--ref names source {request.source!r}; the pipeline declares "
                f"{sorted(sources) or 'no source'}",
            )
        else:
            wanted[request.source] = request.rev
    checkouts: dict[str, SourceCheckout] = {}
    for name, rev in wanted.items():
        path = sources[name]
        top = _toplevel(path)
        if top is None or top != path:
            raise PipelineError(
                "source-not-git",
                f"source {name!r} is not the top level of a git work tree",
            )
        commit = (
            _git(
                top,
                "rev-parse",
                "--verify",
                "-q",
                "--end-of-options",
                f"{rev}^{{commit}}",
                check=False,
            )
            .decode(errors="replace")
            .strip()
        )
        if not _SHA.fullmatch(commit):
            raise PipelineError(
                "deploy-ref-unknown",
                f"source {name!r}: revision {rev!r} is not a commit in its repository "
                "(fetch it first)",
            )
        checkouts[name] = SourceCheckout(name, top, rev, commit)
    return checkouts


@dataclass
class SourceCheckouts:
    """Sources pinned to commits, materialised in temporary worktrees.

    Use as a context manager (or call :meth:`close`): the worktrees are
    removed on exit, whatever happened.
    """

    checkouts: dict[str, SourceCheckout]
    model_source: str | None = None
    say: Callable[[str], None] = field(default=lambda _line: None, repr=False)
    _directory: Path | None = field(default=None, repr=False)
    _trees: list[tuple[Path, Path]] = field(default_factory=list, repr=False)

    def __bool__(self) -> bool:
        return bool(self.checkouts)

    def __enter__(self) -> SourceCheckouts:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------ materialise
    def materialise(self) -> None:
        """Check every pinned commit out (one worktree per repository and commit)."""
        from piceli.artifacts.source_identity import SourceIdentityError
        from piceli.tempfiles import make_directory, track

        self._directory = make_directory("ref").resolve()
        # A terminating signal also drops the worktrees, not just the files.
        track(self._directory, self.close)
        hooks = self._directory / "hooks"
        hooks.mkdir()
        shared: dict[tuple[Path, str], Path] = {}
        try:
            for checkout in self.checkouts.values():
                key = (checkout.repository, checkout.commit)
                if key not in shared:
                    root = self._directory / f"{len(shared)}-{checkout.name}"
                    self.say(
                        f"[inputs] {checkout.name}: checking out {checkout.commit[:12]}"
                        f" ({checkout.rev})"
                    )
                    try:
                        _git(
                            checkout.repository,
                            "-c",
                            f"core.hooksPath={hooks}",
                            "-c",
                            "advice.detachedHead=false",
                            "worktree",
                            "add",
                            "--detach",
                            "--quiet",
                            str(root),
                            checkout.commit,
                            timeout=CHECKOUT_SECONDS,
                        )
                    except SourceIdentityError as error:
                        if error.code is not None:  # git-unavailable, git-timed-out
                            raise PipelineError(error.code, str(error)) from None
                        raise PipelineError(
                            "deploy-ref-checkout-failed", str(error)
                        ) from None
                    self._trees.append((checkout.repository, root))
                    shared[key] = root
                checkout.root = shared[key]
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Remove every worktree and the temporary directory (idempotent)."""
        trees, self._trees = self._trees, []
        for repository, root in trees:
            try:
                _git(
                    repository,
                    "worktree",
                    "remove",
                    "--force",
                    "--force",
                    str(root),
                    timeout=CHECKOUT_SECONDS,
                )
            except Exception:
                shutil.rmtree(root, ignore_errors=True)
                try:
                    _git(repository, "worktree", "prune", check=False)
                except Exception:  # cleanup is best effort
                    pass
        if self._directory is not None:
            from piceli.tempfiles import discard

            discard(self._directory)
            self._directory = None
        for checkout in self.checkouts.values():
            checkout.root = None

    # ----------------------------------------------------------------- reads
    def roots(self) -> dict[str, Path]:
        """Source name → its checkout directory."""
        return {
            name: item.root
            for name, item in self.checkouts.items()
            if item.root is not None
        }

    def remap(self, path: Path) -> Path:
        """``path`` inside a pinned repository → the same path in its checkout."""
        resolved = path.expanduser().resolve()
        best: tuple[int, Path] | None = None
        for item in self.checkouts.values():
            if item.root is None or not resolved.is_relative_to(item.repository):
                continue
            depth = len(item.repository.parts)
            if best is None or depth > best[0]:
                best = (depth, item.root / resolved.relative_to(item.repository))
        return best[1] if best is not None else path

    def describe(self) -> dict[str, dict[str, Any]]:
        """Source name → ``{"ref", "commit"}`` (plan, journal, receipts)."""
        return {name: item.describe() for name, item in sorted(self.checkouts.items())}

    def hashed(self) -> dict[str, str]:
        """Source name → commit: what the combined plan hash covers."""
        return {name: item.commit for name, item in sorted(self.checkouts.items())}

    def load_build(
        self, build: Build
    ) -> tuple[BuildSpec, InputsSpec | None, InputsLock | None]:
        """The build's spec, inputs and lock as the pinned commits hold them."""
        from piceli.artifacts.build_spec import BuildSpec
        from piceli.artifacts.source_identity import InputsLock, InputsSpec

        if build.path is not None:
            spec = BuildSpec.from_toml(self.remap(build.path))
            base = build.path.resolve().parent
        else:
            assert build.document is not None
            base = (build.base or Path.cwd()).resolve()
            spec = BuildSpec.from_dict(build.document, self.remap(base))
        if build.platform is not None:
            spec = spec.with_platform(build.platform)
        if len(spec.platforms) != 1:
            raise PipelineError(
                "pipeline-invalid",
                f"build {spec.name!r} targets {len(spec.platforms)} platforms at the "
                "pinned commit; a pipeline build must target exactly one",
            )
        inputs = None
        if spec.inputs is not None:
            declared = Path(spec.inputs).expanduser()
            original = declared if declared.is_absolute() else base / declared
            parsed = InputsSpec.from_toml(self.remap(original))
            # Relative source paths keep resolving from the file on disk; the
            # pinned sources are read from their checkouts.
            inputs = dataclasses.replace(
                parsed, base=original.resolve().parent, roots=self.roots()
            )
        lock = (
            InputsLock.from_json(self.remap(build.lock).read_text())
            if build.lock is not None
            else None
        )
        return spec, inputs, lock

    # ----------------------------------------------------------------- model
    def check_model(self, directory: Path | None) -> str | None:
        """Refuse when the model's Python files differ from their pinned commit.

        Returns the source whose commit the model was checked against, or
        ``None`` when the model is not inside a pinned repository.
        """
        if directory is None:
            return None
        top = _toplevel(directory)
        pinned = sorted(
            (item for item in self.checkouts.values() if item.repository == top),
            key=lambda item: item.name,
        )
        if top is None or not pinned:
            return None
        relative = directory.resolve().relative_to(top).as_posix()
        pattern = "*.py" if relative == "." else f"{relative}/*.py"
        for item in pinned:
            changed = _git(
                top,
                "diff",
                "--name-only",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                item.commit,
                "--",
                f":(top,glob){pattern}",
            ).decode(errors="replace")
            files = sorted(line for line in changed.splitlines() if line)
            if files:
                raise PipelineError(
                    "deploy-ref-model-differs",
                    f"the pipeline's Python files differ from source {item.name!r} at "
                    f"{item.commit[:12]} ({', '.join(files[:5])}"
                    + (" …" if len(files) > 5 else "")
                    + "): commit them, or deploy that commit from a checkout of it",
                )
        self.model_source = pinned[0].name
        return self.model_source


def open_checkouts(
    pipeline: Pipeline,
    requests: Sequence[RefRequest],
    *,
    say: Callable[[str], None] | None = None,
) -> SourceCheckouts:
    """Resolve, check the model against, and materialise the requested refs."""
    checkouts = SourceCheckouts(resolve_refs(pipeline, requests))
    if say is not None:
        checkouts.say = say
    checkouts.check_model(pipeline.base)
    checkouts.materialise()
    return checkouts


def recorded_requests(refs: Mapping[str, Any]) -> tuple[RefRequest, ...]:
    """The requests that re-open a journaled run's commits (``--resume``)."""
    return tuple(RefRequest(name, str(commit)) for name, commit in refs.items())
