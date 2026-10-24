"""``piceli dev nix build`` and the cache's secrets (0.24.0).

``piceli dev nix build ATTR`` builds a flake output of the source as a
development run on a docker or native build host whose profile uses the
host's Nix (``DevProfile.for_nix()``). With ``--publish MODULE:ATTR`` (a
:class:`~piceli.dev.nix.NixCache`) it plans first: the plan names the
commit, the output, the host, the cache and the key, and nothing runs
until ``--approve`` gives its digest. Then the build runs isolated as any
development run (no key, no cloud credential); after it, on the same host,
a separate short step (:mod:`piceli.dev.nix_publish`) receives the signing
key and the S3 keys on its stdin, signs and pushes the closure, and this
client signs and uploads the build manifest.

``piceli secrets nix-cache NAME --generate`` keeps the cache's key pair as
a local credential and prints the public key; ``piceli secrets s3 NAME
--prompt`` keeps a bucket's S3 access keys.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from piceli.cli_contract import EXIT_APPROVAL, emit_json, fail, reject, say
from piceli.k8s.cli.cluster import JsonOption
from piceli.k8s.cli.dev import requester_label, run_summary

nix_app = typer.Typer(
    rich_markup_mode=None,
    help="Nix builds on build hosts and a signed binary cache (0.24.0).",
    no_args_is_help=True,
)

#: An image is often a few GiB; the run's artifacts may be this large.
IMAGE_MAX_BYTES = 32 << 30
#: ``--image`` writes ``NAME.image.json`` next to the run's directory.
IMAGE_SCHEMA = "piceli.nix-image.v1"

DevOption = Annotated[
    str,
    typer.Option(
        "--cluster",
        "--dev",
        envvar="PICELI_DEV_CLUSTER",
        help="MODULE:ATTR of a piceli.infra.DevBuilds (no cluster needed), or "
        "of a Cluster with dev=DevBuilds(...) (or PICELI_DEV_CLUSTER)",
        show_default=False,
    ),
]


def load_dev(ref: str) -> Any:
    """The ``DevBuilds`` that ``ref`` names: a bare one, or a ``Cluster``'s."""
    from piceli.app.render import RenderError, load_target
    from piceli.cli_contract import describe_user_error
    from piceli.dev.model import DevBuilds
    from piceli.errors import ERRORS
    from piceli.infra import Cluster

    try:
        value = load_target(ref, Path.cwd())
    except RenderError as error:
        reject("cluster-not-found", str(error))
    except Exception as error:  # the module raised while importing
        code = getattr(error, "code", None)
        if isinstance(code, str) and code in ERRORS:
            reject(code, str(error))
        reject(
            "cluster-load-failed",
            f"importing {ref} failed: {describe_user_error(error)}",
        )
    if isinstance(value, DevBuilds):
        return value
    if not isinstance(value, Cluster):
        reject("cluster-not-found", f"{ref} is not a piceli.infra.DevBuilds or Cluster")
    if value.dev is None:
        reject(
            "dev-not-enabled",
            f"{value.name} declares no development builds: add "
            "Cluster(dev=DevBuilds(hosts=[...], profiles=[DevProfile.for_nix()])), "
            "or give a DevBuilds",
        )
    return value.dev


def load_cache(ref: str) -> Any:
    """The ``NixCache`` that ``ref`` (``MODULE:ATTR``) names."""
    from piceli.app.render import load_target
    from piceli.cli_contract import describe_user_error
    from piceli.dev.nix import NixCache

    try:
        value = load_target(ref, Path.cwd())
    except Exception as error:
        code = getattr(error, "code", None)
        reject(
            code if code == "dev-nix-cache-invalid" else "dev-nix-cache-invalid",
            f"loading {ref} failed: {describe_user_error(error)}",
        )
    if not isinstance(value, NixCache):
        reject("dev-nix-cache-invalid", f"{ref} is not a piceli.infra.NixCache")
    return value


def _profile(dev: Any, name: str | None) -> Any:
    from piceli.dev.model import DevError

    if name is None:
        found = next((item for item in dev.profiles if item.nix), None)
        if found is None:
            reject(
                "dev-nix-invalid",
                "no profile uses the host's Nix: declare DevProfile.for_nix()",
            )
        name = found.name
    try:
        profile = dev.profile(name)
    except DevError as error:
        reject(error.code, str(error))
    if not profile.nix:
        reject("dev-nix-invalid", f"profile {name} does not use Nix (nix=True)")
    return profile


def _host(dev: Any, name: str | None) -> Any:
    """``--host``, else the first docker or native host that takes runs
    (0.25.0: not ``runs=False``): the plan names it."""
    if name is not None:
        chosen = dev.host(name)
        if chosen is None:
            reject("dev-host-unknown", f"no build host {name!r} is declared")
    else:
        chosen = next((h for h in dev.hosts if h.kind != "cluster" and h.runs), None)
        if chosen is None:
            reject(
                "dev-nix-host-unsupported",
                "declare a docker or native build host (BuildHost.docker/native)",
            )
    if chosen.kind == "cluster":
        reject(
            "dev-nix-host-unsupported",
            f"{chosen.name} is the cluster's builder node; Nix builds run on "
            "docker or native hosts",
        )
    return chosen


def _secrets(cache: Any) -> tuple[str, tuple[str, str] | None]:
    """The signing key (checked against the cache's public key) and S3 keys."""
    from piceli.dev.model import DevError
    from piceli.dev.nix import public_key
    from piceli.infra.machines.credentials import load_credential
    from piceli.infra.machines.model import InfraError

    try:
        trusted = cache.trusted_key()
        secret = load_credential(cache.signing_credential, "nix-signing-key")
        keys = (
            load_credential(cache.write_credentials, "s3-keys")
            if cache.write_credentials
            else None
        )
        derived = public_key(secret)
    except (InfraError, DevError) as error:
        reject(error.code, str(error))
    if derived != trusted:
        reject(
            "dev-nix-key-mismatch",
            f"credential {cache.signing_credential} is not the key of "
            f"NixCache(public_key={cache.key_name}:...)",
        )
    pair = None
    if keys is not None:
        access, _, secret_access = keys.partition(":")
        pair = (access, secret_access)
    return secret, pair


@nix_app.command("build")
def build(
    attr: Annotated[
        str,
        typer.Argument(
            help="The flake output of the source: ATTR or .#ATTR "
            "(nixosConfigurations.HOST.config.system.build.toplevel)",
            show_default=False,
        ),
    ],
    cluster: DevOption,
    ref: Annotated[
        str | None,
        typer.Option("--ref", help="Build this commit (required with --publish)"),
    ] = None,
    worktree: Annotated[
        Path | None,
        typer.Option("--worktree", help="Build this working tree (default: here)"),
    ] = None,
    repo: Annotated[
        Path, typer.Option("--repo", help="The repository with --ref (default: here)")
    ] = Path("."),
    name: Annotated[
        str | None,
        typer.Option(
            "--name",
            help="The manifest's name (default: HOST of nixosConfigurations.HOST, "
            "HOST-image with --image)",
        ),
    ] = None,
    profile: Annotated[
        str | None,
        typer.Option("--profile", help="A Nix profile (default: the first nix=True)"),
    ] = None,
    host: Annotated[
        str | None,
        typer.Option(
            "--host", help="A docker or native build host (default: the first)"
        ),
    ] = None,
    image: Annotated[
        bool,
        typer.Option(
            "--image",
            help="The output is a disk image: copy its file back with its sha256",
        ),
    ] = False,
    publish: Annotated[
        str | None,
        typer.Option(
            "--publish",
            help="MODULE:ATTR of a piceli.infra.NixCache: sign and push the "
            "closure, upload the manifest (needs --approve)",
        ),
    ] = None,
    tag_pattern: Annotated[
        str | None,
        typer.Option(
            "--tag-pattern",
            help="With --publish: the pointer names the highest version tag at "
            "the commit matching this NixHost Tag pattern (web-v*), compared "
            "after its prefix (default: --ref when it is a tag, else the first "
            "tag at the commit)",
        ),
    ] = None,
    branch: Annotated[
        str | None,
        typer.Option(
            "--branch",
            help="With --publish: also point refs/heads/BRANCH at this build "
            "(default: --ref when it names a branch)",
        ),
    ] = None,
    approve: Annotated[
        str | None,
        typer.Option("--approve", help="The publish plan's digest"),
    ] = None,
    release: Annotated[
        str | None,
        typer.Option(
            "--release",
            help="With --publish: the host of a NixRelease with gate=Promotion(...) "
            "in --composition; the publish needs its promotion gate to pass",
        ),
    ] = None,
    composition: Annotated[
        Path | None,
        typer.Option(
            "--composition",
            help="The composition module declaring the gated NixRelease (default: "
            "the --publish module; --release's default: infra.py)",
        ),
    ] = None,
    gitops_kubeconfig: Annotated[
        Path | None,
        typer.Option(
            "--kubeconfig",
            help="With --release: reach the controller with this kubeconfig "
            "(default: the composition's cluster's credential profile)",
        ),
    ] = None,
    gitops_context: Annotated[
        str | None, typer.Option("--context", help="With --kubeconfig: its context")
    ] = None,
    gitops_state_dir: Annotated[
        Path | None,
        typer.Option(
            "--state-dir", help="With --release: a local controller's state directory"
        ),
    ] = None,
    gitops_namespace: Annotated[
        str,
        typer.Option("--namespace", help="With --release: the controller's namespace"),
    ] = "piceli-system",
    inputs: Annotated[
        list[str] | None,
        typer.Option(
            "--input",
            help="A private flake input to fetch on the build host before the "
            "build, NAME or NAME=CREDENTIAL (a git-token in this machine's "
            "piceli secrets; default: git); repeat. The NixRelease of the "
            "composition that publishes this output adds its own",
        ),
    ] = None,
    artifacts_dir: Annotated[
        Path, typer.Option("--artifacts-dir", help="Where an image lands (RUN/)")
    ] = Path("piceli-dev-artifacts"),
    queue_timeout: Annotated[
        int, typer.Option("--queue-timeout", help="Seconds to wait for a slot", min=1)
    ] = 3600,
    quiet: Annotated[
        bool, typer.Option("--quiet", help="Do not stream the build's log")
    ] = False,
    as_json: JsonOption = False,
) -> None:
    """Build a flake output on a Nix build host; with --publish, sign and push it."""
    from piceli.dev.client import RunRequest
    from piceli.dev.client import run as run_on
    from piceli.dev.model import DevError
    from piceli.dev.nix import (
        RESULT_SCHEMA,
        branch_of,
        build_argv,
        check_name,
        default_name,
        image_of,
        image_patterns,
        installable,
        plan_digest,
        publish_plan,
        ref_name_ok,
        store_path_of,
        tag_of,
    )
    from piceli.dev.pack import SourceRequest, repository_name, resolve, toplevel

    dev = load_dev(cluster)
    try:
        target = installable(attr)
        if ref is not None and worktree is not None:
            raise DevError("dev-run-invalid", "give --ref or --worktree, not both")
        root_dir = toplevel(repo if ref is not None else (worktree or Path(".")))
        rev = resolve(root_dir, ref) if ref is not None else None
        if tag_pattern is not None:
            from piceli.hosts.model import HostError, Tag

            try:
                Tag(tag_pattern)
            except HostError:
                raise DevError(
                    "dev-nix-invalid", "--tag-pattern is a glob of tag names (web-v*)"
                ) from None
        tag = tag_of(root_dir, ref, rev, tag_pattern) if rev is not None else None
        if branch is None and ref is not None:
            branch = branch_of(root_dir, ref)
        elif branch is not None and not ref_name_ok(branch):
            raise DevError("dev-nix-invalid", "--branch is a branch name")
        label = name or default_name(target, image)
        if label is not None:
            check_name(label)
    except DevError as error:
        reject(error.code, str(error))
    chosen_profile = _profile(dev, profile)
    chosen = _host(dev, host)
    cache = load_cache(publish) if publish is not None else None
    plan: dict[str, Any] | None = None
    # 0.26.0: the private inputs fetched on the build host before the build.
    private = _private_inputs(
        inputs or [], publish, composition, cache, target, root_dir, rev,
        dev_ref=cluster,
    )  # fmt: skip
    if release is not None and cache is None:
        reject("dev-nix-invalid", "--release gates a --publish")
    if cache is not None:
        if rev is None:
            reject("dev-nix-invalid", "--publish builds a commit: give --ref")
        if label is None:
            reject("dev-nix-invalid", "--publish needs --name (the manifest's name)")
        if cache.scheme == "https":
            reject(
                "dev-nix-cache-invalid",
                "an https:// cache is read-only; publish to its s3:// or file:// URL",
            )
        try:
            plan = publish_plan(
                target=target, name=label, commit=rev, tag=tag, host=chosen.name,
                profile=chosen_profile.name, image=image, cache=cache,
                branch=branch,
            )  # fmt: skip
        except DevError as error:
            reject(error.code, str(error))
        gated = ""
        covering = (
            _named_release(release, composition or Path("infra.py"), target)
            if release is not None
            else _covering(str(publish), composition, cache, target)
        )
        if private:  # the approval covers which credential each input uses
            plan["inputs"] = private
        if covering is not None:  # a gated host: the gate always runs
            plan["pinned"] = _gate(
                covering, root_dir, str(rev),
                kubeconfig=gitops_kubeconfig, context=gitops_context,
                state_dir=gitops_state_dir, namespace=gitops_namespace,
            )  # fmt: skip
            gated = (
                f" --release {release} --composition {composition or 'infra.py'}"
                if release is not None
                else f" --composition {composition}"
                if composition is not None
                else ""
            ) + (
                f" --state-dir {gitops_state_dir}"
                if gitops_state_dir is not None
                else f" --kubeconfig {gitops_kubeconfig} --context {gitops_context}"
                if gitops_kubeconfig is not None
                else ""
            )
        digest = plan_digest(plan)
        if approve is None:
            command = f"piceli dev nix build {target} --cluster {cluster} --ref {ref} --publish {publish} --host {chosen.name}{gated}"
            if name:
                command += f" --name {name}"
            if image:
                command += " --image"
            if tag_pattern is not None:
                command += f" --tag-pattern '{tag_pattern}'"
            if plan["branch"] is not None:
                command += f" --branch {plan['branch']}"
            for item in inputs or ():
                command += f" --input {item}"
            say(f"publish {target} at {rev[:12]} to {cache.store_url}")
            say(f"  signed by {cache.key_name}; manifest {plan['manifest']}")
            for key in plan["pointers"]:
                say(f"  pointer {key}")
            for item in plan.get("inputs") or ():
                say(f"  input {item['input']} {item['url']} fetched first on the "
                    f"build host with its credential {item['credential']}")  # fmt: skip
            if plan.get("pinned"):
                pinned = plan["pinned"]
                say(f"  pinned {pinned['input']} {pinned['commit'][:12]} verified on "
                    f"{pinned['environment']} (run {pinned['verified_run']})")  # fmt: skip
            say("approve with:")
            say(f"  {command} --approve {digest}")
            emit_json(
                {
                    "schema": RESULT_SCHEMA,
                    "state": "approval-required",
                    "plan": plan,
                    "digest": digest,
                    "approve_command": f"{command} --approve {digest}",
                }
            )
            raise typer.Exit(EXIT_APPROVAL)
        if approve != digest:
            reject(
                "dev-nix-plan-changed",
                "the publish plan changed since it was approved; review it again",
            )
        # Read now, so a missing or wrong key refuses before any build.
        secret, s3_keys = _secrets(cache)
    elif approve is not None:
        reject("dev-nix-invalid", "--approve approves a --publish plan")
    from piceli.dev.hosts import DockerPort, NativePort, probe

    state = probe(chosen)
    if not state.up or state.quarantined is not None:
        reject(
            "dev-host-unavailable",
            f"build host {chosen.name}: {state.detail or state.quarantined}",
        )
    import contextlib

    held = contextlib.ExitStack()  # 0.26.0: the inputs' GC roots, until built
    if private:
        fetcher = _fetcher(chosen, chosen_profile)
        roots = held.enter_context(fetcher.roots())  # 0.26.2: docker roots_dir
        try:
            _fetch_private(private, fetcher, root_dir, rev, roots)
        except BaseException:  # a refusal exits: the roots still go
            held.close()
            raise
    request = RunRequest(
        root=SourceRequest(repository_name(root_dir), root_dir, rev),
        command=build_argv(target),
        cwd=".",
        profile=chosen_profile.name,
        requester=requester_label(None),
        artifacts=image_patterns() if image else [],
        artifacts_dir=artifacts_dir,
        queue_timeout=float(queue_timeout),
        artifact_max_bytes=IMAGE_MAX_BYTES if image else None,
    )
    seen: list[str] = []

    def log(line: str) -> None:
        seen.append(line)
        if not quiet:
            say(line)

    port: Any = (
        DockerPort(chosen, dev) if chosen.kind == "docker" else NativePort(chosen, dev)
    )
    try:
        with held:
            result = run_on(port, dev, request, say=say, log=log, host=chosen)
    except DevError as error:
        reject(error.code, str(error))
    say(run_summary(result))
    body: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "installable": target,
        "name": label,
        "commit": rev,
        "tag": tag,
        "host": chosen.name,
        "run": result,
    }
    if result.get("state") != "passed":
        for line in result.get("log_tail") or ():
            say(f"  | {line}")
        fail(
            str(result.get("reason") or "dev-run-failed"),
            "the Nix build did not pass",
            **body,
        )
    store_path = store_path_of(seen) or store_path_of(result.get("log_tail") or ())
    if store_path is None:
        fail("dev-nix-no-output", "the build printed no store path", **body)
    found_image = image_of(result.get("artifacts") or ()) if image else None
    if image and found_image is None:
        fail("dev-nix-no-output", "the output holds no disk image file", **body)
    body.update({"store_path": store_path, "image": found_image, "publish": None})
    say(f"built {store_path}")
    if found_image is not None:
        found_image["build"] = str(
            _image_sidecar(artifacts_dir, label or "image", found_image, store_path,
                           rev, target,
                           built_on=_built_on(chosen, chosen_profile))
        )  # fmt: skip
        _keep_image(chosen, chosen_profile, label or "image", store_path)
        say(f"image {found_image['file']} (sha256 {found_image['sha256']})")
        say(f"  Image.from_build(NAME, {found_image['build']!r}) reads it")
    if cache is not None and plan is not None:
        body["publish"] = _publish(
            cache, plan, store_path, chosen, chosen_profile, found_image,
            secret, s3_keys,
        )  # fmt: skip
        del secret
    body["state"] = "published" if body["publish"] else "built"
    if as_json:
        emit_json(body)


def _private_inputs(
    flags: list[str],
    publish: str | None,
    composition: Path | None,
    cache: Any,
    target: str,
    root: Path,
    rev: str | None,
    *,
    dev_ref: str | None = None,
) -> list[dict[str, str]]:
    """The private inputs of this build (0.26.0,
    :func:`piceli.dev.nix_inputs.covered`): ``--input`` and, when the
    composition (``--composition``, else the ``--publish`` module, else
    0.26.2 the ``--dev`` module) has a NixRelease of this output (and cache),
    its gate's input, its declared inputs and the inputs whose repository is
    a Source of it. 0.26.2: without ``--publish``, a build of another output
    of the same flake (an ``--image``) covers what every NixRelease there
    covers, so it fetches its inputs as the release path does."""
    from piceli.dev.nix_inputs import (
        DEFAULT_CREDENTIAL,
        check_declared,
        covered,
        lock_nodes,
    )

    declared: dict[str, str] = {}
    gates: list[str] = []
    sources: list[tuple[str, str]] = []
    release = _release_of(publish, composition, cache, target, dev_ref=dev_ref)
    if release is not None:
        found, sources = release
        for item in found:
            declared = dict(item.inputs) | declared
            if item.gate is not None:
                gates.append(item.gate.input)
    try:
        flagged = check_declared(
            {
                name: credential or "git"
                for name, _, credential in (item.partition("=") for item in flags)
            },
            "--input",
        )
    except ValueError as error:
        reject("dev-nix-invalid", str(error))
    lock = _lock_of(root, rev)
    nodes, names = lock_nodes(lock)
    for name in flagged:
        if name not in names and name not in nodes:
            reject(
                "dev-nix-invalid",
                f"--input {name}: flake.lock "
                + (f"at {rev[:12]} " if rev else "")
                + "locks no such input",
            )
    gate = gates[0] if gates else None
    extra = {name: DEFAULT_CREDENTIAL for name in gates[1:] if name not in declared}
    return covered(
        lock, gate=gate, sources=sources, declared=extra | declared | flagged
    )


def _lock_of(root: Path, rev: str | None) -> bytes | None:
    import subprocess

    from piceli.dev.nix_inputs import LOCK_FILE

    if rev is None:
        lock = root / LOCK_FILE
        return lock.read_bytes() if lock.is_file() else None
    shown = subprocess.run(
        ["git", "show", f"{rev}:{LOCK_FILE}"],
        cwd=root, capture_output=True, check=False,
    )  # fmt: skip
    return shown.stdout if shown.returncode == 0 else None


def _release_of(
    publish: str | None,
    composition: Path | None,
    cache: Any,
    target: str,
    *,
    dev_ref: str | None = None,
) -> tuple[list[Any], list[tuple[str, str]]] | None:
    """The NixReleases of ``target`` (and ``cache``) in the composition (one,
    the first; 0.26.2: without a cache and none of ``target``, every one
    there), and the composition's sources ``(key, url)``; ``None``: none."""
    from piceli.cli_contract import describe_user_error
    from piceli.infra.composition import _import, composition_from
    from piceli.infra.nix_releases import NixRelease

    ref = publish if publish is not None else dev_ref
    if composition is None and ref is None:
        return None
    path = composition or Path(str(ref).rsplit(":", 1)[0])
    if composition is None and (path.suffix != ".py" or not path.is_file()):
        return None
    try:
        module = _import(path.resolve())
    except Exception as error:  # never build past a composition it cannot read
        reject(
            "composition-invalid",
            f"importing {path} failed: {describe_user_error(error)}",
        )
    every = [value for value in vars(module).values() if isinstance(value, NixRelease)]
    releases = [
        value
        for value in every
        if value.host.attribute == target
        and (
            cache is None
            or (
                value.host.cache.store_url == cache.store_url
                and value.host.cache.public_key == cache.public_key
            )
        )
    ][:1]
    if not releases and cache is None:
        releases = every
    if not releases:
        return None
    try:
        sources = list(composition_from(module, default_name=path.stem).sources)
    except Exception:  # not a whole composition: the releases' own sources
        sources = [item.source for item in releases]
    return releases, [(source.key, source.url) for source in sources]


def _fetch_private(
    private: list[dict[str, str]],
    fetcher: Any,
    root: Path,
    rev: str | None,
    roots: Any = None,
) -> None:
    """Fetch the private inputs on the build host with this machine's
    credentials, before the build (else ``infra-nix-input-unreachable``)."""
    from piceli.dev.model import DevError
    from piceli.dev.nix_inputs import resolve

    try:
        pairs = resolve(private, _lock_of(root, rev))
        fetcher(pairs, say, roots=roots)
    except DevError as error:
        reject(error.code, str(error))


def _fetcher(host: Any, profile: Any) -> Any:
    """The private inputs' fetch on ``host`` (else ``dev-nix-invalid``)."""
    from piceli.dev.model import DevError
    from piceli.dev.nix_inputs import fetch_inputs
    from piceli.dev.nix_step import step_image

    try:
        return fetch_inputs(host, step_image(host, profile))
    except DevError as error:
        reject(error.code, str(error))


def _named_release(release: str, composition: Path, target: str) -> Any:
    """``(host, gate, composition)`` of ``--release HOST``."""
    from piceli.k8s.cli.promote import gated_release

    found, owner = gated_release(composition, release)
    if found.attribute != target:
        reject(
            "dev-nix-invalid",
            f"--release {release} builds {found.attribute}, not {target}",
        )
    return found.name, found.gate, owner


def _covering(publish: str, composition: Path | None, cache: Any, target: str) -> Any:
    """``(host, gate, composition or None)`` of the gated NixRelease (or
    NixHost with ``gate=``) that covers this publish: the same cache and
    attribute, declared in ``--composition`` or the ``--publish`` module.
    ``None``: no gated host is published."""
    from piceli.cli_contract import describe_user_error
    from piceli.hosts.model import NixHost
    from piceli.infra.composition import _import, composition_from
    from piceli.infra.nix_releases import NixRelease

    path = composition or Path(publish.rsplit(":", 1)[0])
    if composition is None and (path.suffix != ".py" or not path.is_file()):
        return None
    try:
        module = _import(path.resolve())
    except Exception as error:  # never publish past a gate it cannot read
        reject(
            "composition-invalid",
            f"importing {path} failed: {describe_user_error(error)}",
        )
    found = []
    for value in vars(module).values():
        host = value.host if isinstance(value, NixRelease) else value
        gate = getattr(value, "gate", None)
        if (
            isinstance(value, NixRelease | NixHost)
            and gate is not None
            and host.attribute == target
            and host.cache.store_url == cache.store_url
            and host.cache.public_key == cache.public_key
        ):
            found.append((host.name, gate))
    if not found:
        return None
    try:
        owner = composition_from(module, default_name=path.stem)
    except Exception:  # not a composition: --state-dir or --kubeconfig reach it
        owner = None
    return (*found[0], owner)


def _gate(covering: Any, root: Path, rev: str, **reach: Any) -> dict[str, Any]:
    """The promotion gate of a hand-run publish: ``flake.lock`` at ``rev``
    pins the input to a commit the environment verified (else a refusal;
    an unreadable history refuses too)."""
    import subprocess

    from piceli.infra.promotion import LOCK_FILE, PromotionError, locked_rev, verify
    from piceli.k8s.cli.promote import read_history

    name, gate, owner = covering
    history = read_history(owner, **reach)
    shown = subprocess.run(
        ["git", "show", f"{rev}:{LOCK_FILE}"],
        cwd=root, capture_output=True, check=False,
    )  # fmt: skip
    try:
        lock = shown.stdout if shown.returncode == 0 else None
        return verify(gate, locked_rev(lock, gate.input), history, host=name)
    except PromotionError as error:
        reject(error.code, str(error), **error.detail)


def _keep_image(host: Any, profile: Any, name: str, store_path: str) -> None:
    """0.26.2: on a docker host with ``roots_dir``, a GC root of the image's
    build there until the next one of ``name``, for ``infra apply``'s upload."""
    from piceli.dev.nix_inputs import hold_root
    from piceli.dev.nix_step import step_image

    link = hold_root(host, step_image(host, profile), name, store_path, say)
    if link is not None:
        say(f"  kept on {host.name} by the GC root {link}")


def _built_on(host: Any, profile: Any) -> dict[str, Any] | None:
    """How ``piceli infra apply`` reaches the image on a remote docker host
    (0.25.3: it uploads from there): the host, its CLI and context, the run
    image (it has Python). ``None`` for a local or native host: the local
    copy is the image."""
    image = getattr(host, "image", None) or getattr(profile, "image", None)
    if getattr(host, "kind", None) != "docker" or not host.context or not image:
        return None
    return {"host": host.name, "kind": "docker", "context": host.context,
            "cli": host.cli, "image": str(image)}  # fmt: skip


def _store_file(image: dict[str, Any], store_path: str) -> str | None:
    """The image file in the host's store: the store path plus its path
    under the out-link (``piceli-nix-out/nixos.qcow2``)."""
    from piceli.dev.nix import OUT_LINK

    parts = Path(str(image["file"])).parts
    if OUT_LINK not in parts:
        return None
    rest = parts[len(parts) - parts[::-1].index(OUT_LINK) :]
    return "/".join([store_path, *rest]) if rest else None


def _image_sidecar(
    directory: Path,
    name: str,
    image: dict[str, Any],
    store_path: str,
    rev: str | None,
    target: str,
    *,
    built_on: dict[str, Any] | None = None,
) -> Path:
    """``DIR/NAME.image.json``: what ``Image.from_build`` reads (the image's
    path relative to it, its sha256 and format, the store path, the rev;
    0.25.3: the build host it can be uploaded from and the file there)."""
    import json
    import os

    path = directory / f"{name}.image.json"
    store_file = _store_file(image, store_path)
    extra: dict[str, Any] = {}
    if built_on is not None and store_file is not None:
        extra = {"built_on": built_on, "store_file": store_file}
    body = extra | {
        "schema": IMAGE_SCHEMA,
        "file": os.path.relpath(image["file"], directory),
        "sha256": image["sha256"],
        "format": image["format"],
        "bytes": image["bytes"],
        "store_path": store_path,
        "commit": rev,
        "installable": target,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.partial")
    staged.write_text(json.dumps(body, sort_keys=True, indent=2) + "\n")
    os.replace(staged, path)
    return path


def _publish(
    cache: Any,
    plan: dict[str, Any],
    store_path: str,
    chosen: Any,
    profile: Any,
    found_image: dict[str, Any] | None,
    secret: str,
    s3_keys: tuple[str, str] | None,
) -> dict[str, Any]:
    """Sign and push on the host, then sign and upload the manifest here."""
    from piceli.dev import nix
    from piceli.dev.model import DevError
    from piceli.dev.nix_step import step_image

    def push(request: Any, say_line: Any) -> Any:
        return nix.run_publisher(
            nix.publisher_argv(chosen, step_image(chosen, profile)),
            request,
            say=say_line,
        )

    try:
        return nix.publish_closure(
            cache, plan, store_path, chosen.name, found_image, secret, s3_keys,
            say=say, push=push,
        )  # fmt: skip
    except DevError as error:
        fail(error.code, str(error))


@nix_app.command("gc")
def gc(
    cache_ref: Annotated[
        str,
        typer.Option(
            "--cache",
            help="MODULE:ATTR of the piceli.infra.NixCache",
            show_default=False,
        ),
    ],
    repo: Annotated[
        Path,
        typer.Option("--repo", help="The repository whose tags and main count"),
    ] = Path("."),
    keep_tags: Annotated[
        int,
        typer.Option("--keep-tags", help="Keep the builds of the newest N tags", min=0),
    ] = 5,
    tag_pattern: Annotated[
        list[str] | None,
        typer.Option(
            "--tag-pattern",
            help="Keep the newest --keep-tags versions of each NixHost Tag "
            "pattern (web-v*, compared after its prefix) instead of the newest "
            "tags by date; repeat it for each host of the repository",
        ),
    ] = None,
    main: Annotated[
        str, typer.Option("--main", help="The branch whose current commit stays")
    ] = "main",
    grace: Annotated[
        int,
        typer.Option(
            "--grace",
            help="Seconds: younger objects stay (a publish in progress)",
            min=0,
        ),
    ] = 3600,
    approve: Annotated[
        str | None, typer.Option("--approve", help="The gc plan's digest")
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """Remove builds of old commits from a Nix cache: plan, then --approve."""
    import time

    from piceli.dev.model import DevError
    from piceli.dev.nix import (
        CacheObjects,
        gc_apply,
        gc_plan,
        kept_commits,
        plan_digest,
    )
    from piceli.dev.pack import toplevel
    from piceli.infra.machines.credentials import load_credential
    from piceli.infra.machines.model import InfraError

    cache = load_cache(cache_ref)
    try:
        cache.trusted_key()  # a cache without its public key has no builds
        patterns = tuple(tag_pattern or ())
        for pattern in patterns:
            from piceli.hosts.model import HostError, Tag

            try:
                Tag(pattern)
            except HostError:
                raise DevError(
                    "dev-nix-invalid", "--tag-pattern is a glob of tag names (web-v*)"
                ) from None
        root = toplevel(repo)
        kept = kept_commits(root, keep_tags, main, patterns)
        keys = None
        if cache.scheme == "s3":
            access, _, secret = load_credential(
                str(cache.write_credentials), "s3-keys"
            ).partition(":")
            keys = (access, secret)
        objects = CacheObjects(cache, keys)
        plan = gc_plan(
            objects, kept, keep_tags=keep_tags, main=main,
            grace_seconds=float(grace), now=time.time(), tag_patterns=patterns,
        )  # fmt: skip
    except (DevError, InfraError) as error:
        reject(error.code, str(error))
    digest = plan_digest(plan)
    body = {**plan, "digest": digest}
    say(
        f"{cache.store_url}: keep {len(kept)} commit(s), "
        f"{plan['kept_paths']} store path(s); delete {len(plan['delete'])} object(s)"
    )
    if not plan["delete"]:
        emit_json({**body, "state": "clean"})
        return
    if approve is None:
        command = (
            f"piceli dev nix gc --cache {cache_ref} --repo {repo} "
            f"--keep-tags {keep_tags} --main {main} --grace {grace}"
            + "".join(f" --tag-pattern '{pattern}'" for pattern in patterns)
        )
        say("approve with:")
        say(f"  {command} --approve {digest}")
        emit_json(
            {
                **body,
                "state": "approval-required",
                "approve_command": f"{command} --approve {digest}",
            }
        )
        raise typer.Exit(EXIT_APPROVAL)
    if approve != digest:
        reject(
            "dev-nix-plan-changed",
            "what gc would delete changed since it was approved; review it again",
        )
    try:
        deleted = gc_apply(objects, plan)
    except DevError as error:
        fail(error.code, str(error))
    say(f"deleted {deleted} object(s)")
    emit_json({**body, "state": "deleted", "deleted": deleted})


# ------------------------------------------------------------- secrets


def nix_cache_secret(
    context: typer.Context,
    name: Annotated[str, typer.Argument(help="Credential name (also the key's name)")],
    generate: Annotated[
        bool, typer.Option("--generate", help="Generate a new key pair")
    ] = False,
    prompt: Annotated[
        bool,
        typer.Option(
            "--prompt",
            help="Read an existing secret key (NAME:base64) from stdin",
        ),
    ] = False,
    key_name: Annotated[
        str | None,
        typer.Option("--key-name", help="The key's name in signatures (default: NAME)"),
    ] = None,
    replace: Annotated[
        bool,
        typer.Option(
            "--replace",
            help="Replace an existing key (hosts that trust the old one refuse "
            "new builds)",
        ),
    ] = False,
) -> None:
    """A Nix binary cache's signing key: generate or import it; print its public key."""
    from piceli.dev.model import DevError
    from piceli.dev.nix import generate_key, public_key
    from piceli.infra.machines.credentials import (
        credential_present,
        load_credential,
        save_credential,
    )
    from piceli.infra.machines.model import InfraError
    from piceli.k8s.cli.infra import _guard, _no_secret_arguments, _read_secret

    _no_secret_arguments(context)
    if generate and prompt:
        reject("infra-prompt-required", "pass one of --prompt or --generate")
    with _guard():
        try:
            if not generate and not prompt:
                public = public_key(load_credential(name, "nix-signing-key"))
                state = "present"
            else:
                if credential_present(name) and not replace:
                    reject(
                        "infra-credential-exists",
                        f"credential {name!r} exists; hosts trust its public key "
                        "(--replace if you mean it)",
                    )
                value = (
                    generate_key(key_name or name)
                    if generate
                    else _read_secret("Nix secret key (not echoed): ")
                )
                public = public_key(value)
                save_credential(name, "nix-signing-key", value)
                del value
                state = "stored"
        except (DevError, InfraError) as error:
            reject(error.code, str(error))
    say(f"credential {name} (nix-signing-key); the secret key was not printed")
    say(f"public key: {public}")
    emit_json(
        {"state": state, "name": name, "kind": "nix-signing-key", "public_key": public}
    )


def s3_secret(
    context: typer.Context,
    name: Annotated[str, typer.Argument(help="Credential name, e.g. cache-s3")],
    prompt: Annotated[
        bool,
        typer.Option(
            "--prompt", help="Read the access key id, then the secret, from stdin"
        ),
    ] = False,
    from_env: Annotated[
        bool,
        typer.Option(
            "--from-env",
            help="Read AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY instead of "
            "stdin (for scripts)",
        ),
    ] = False,
) -> None:
    """Store a bucket's S3 access keys, part by part (never printed).

    Asks for the S3 access key id (echoed: an identifier), then the S3 secret
    access key (not echoed); piped, one per line. --from-env reads them from
    AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY instead. Each part is checked
    before anything is stored (not empty, no space around it, no ':' inside),
    and they are stored under their own names. Running it again replaces them.
    """
    from piceli.infra.machines.credential_parts import SHAPES
    from piceli.infra.machines.credentials import credential_present, save_parts
    from piceli.k8s.cli.infra import _ask_parts, _guard, _no_secret_arguments

    _no_secret_arguments(context, from_env=from_env)
    if prompt == from_env:
        reject(
            "infra-prompt-required",
            "pass --prompt (the keys are read from stdin) or --from-env",
        )
    shape = SHAPES["s3"]
    with _guard():
        replaced = credential_present(name)
        save_parts(name, "s3", _ask_parts(shape, from_env=from_env))
    say(
        f"stored credential {name} (s3-keys): {shape.names()}; the secret was not printed"
    )
    emit_json({"state": "stored", "name": name, "kind": "s3-keys",
               "parts": shape.keys, "replaced": replaced})  # fmt: skip


def git_token_secret(
    context: typer.Context,
    name: Annotated[
        str, typer.Argument(help="Credential name (NixRelease(inputs=) names it; git)")
    ],
    host: Annotated[
        str,
        typer.Option(
            "--host", help="The Git host it is for (github.com); used for no other"
        ),
    ],
    prompt: Annotated[
        bool,
        typer.Option(
            "--prompt", help="Read the token from stdin (typed without echo, or piped)"
        ),
    ] = False,
    username: Annotated[
        str,
        typer.Option("--username", help="The Git user name sent with the token"),
    ] = "git",
) -> None:
    """Store a Git token a build host fetches private flake inputs with (never printed).

    0.26.0. The build host fetches each private input of a Nix build with it
    before the build (git+https through GIT_ASKPASS, git+ssh of the same host
    over HTTPS, github:/gitlab: as Nix's access-tokens), never inside the
    build. Running it again replaces it.
    """
    from piceli.dev.nix_inputs import KIND, save_git_token
    from piceli.infra.machines.credentials import credential_present
    from piceli.infra.machines.model import InfraError
    from piceli.k8s.cli.infra import _guard, _no_secret_arguments, _read_secret

    _no_secret_arguments(context)
    if not prompt:
        reject(
            "infra-prompt-required",
            "pass --prompt: the token is read from stdin (no echo on a terminal)",
        )
    with _guard():
        replaced = credential_present(name)
        token = _read_secret(f"Git token for {host} (not echoed): ")
        try:
            save_git_token(name, host, username, token)
        except InfraError as error:
            reject(error.code, str(error))
        del token
    say(
        f"stored credential {name} ({KIND}) for {host.lower()}; the token was not printed"
    )
    emit_json({"state": "stored", "name": name, "kind": KIND,
               "host": host.lower().split(":", 1)[0], "replaced": replaced})  # fmt: skip


def git_ssh_key_secret(
    context: typer.Context,
    name: Annotated[
        str, typer.Argument(help="Credential name (NixRelease(inputs=) names it; git)")
    ],
    host: Annotated[
        str,
        typer.Option("--host", help="The Git host it is for (github.com); no other"),
    ],
    file: Annotated[
        Path | None,
        typer.Option("--file", help="The deploy key: an OpenSSH private key file"),
    ] = None,
    known_hosts: Annotated[
        Path | None,
        typer.Option(
            "--known-hosts",
            help="That host's known_hosts lines (ssh-keyscan HOST); without it "
            "the host's key is taken on first use, each time",
        ),
    ] = None,
) -> None:
    """Store a read-only deploy key a build host fetches private git+ssh flake inputs with.

    0.26.0. The build host fetches each private git+ssh input of a Nix build
    with it before the build (GIT_SSH_COMMAND with the key in a 0600 file of
    a temporary directory), never inside the build. Prints the name and the
    host, never the key. Running it again replaces it.
    """
    from piceli.dev.nix_inputs import SSH_KIND, save_git_ssh_key
    from piceli.infra.machines.credentials import credential_present
    from piceli.infra.machines.model import InfraError
    from piceli.k8s.cli.infra import _guard, _no_secret_arguments

    _no_secret_arguments(context)
    if file is None:
        reject("infra-prompt-required", "pass --file PATH: the deploy key's file")
    with _guard():
        replaced = credential_present(name)
        try:
            key = file.expanduser().read_text()
            pinned = known_hosts.expanduser().read_text() if known_hosts else None
            save_git_ssh_key(name, host, key, pinned)
        except OSError:
            reject(
                "infra-credential-invalid", "the key or known_hosts file is unreadable"
            )
        except InfraError as error:
            reject(error.code, str(error))
        del key
    say(
        f"stored credential {name} ({SSH_KIND}) for {host.lower()}; the key was not printed"
    )
    emit_json({"state": "stored", "name": name, "kind": SSH_KIND,
               "host": host.lower().split(":", 1)[0],
               "known_hosts": known_hosts is not None, "replaced": replaced})  # fmt: skip


def register(dev_app: typer.Typer) -> None:
    from piceli.k8s.cli.cluster import secrets_app

    dev_app.add_typer(nix_app, name="nix")
    settings: dict[str, Any] = {
        "allow_extra_args": True,
        "ignore_unknown_options": True,
    }
    secrets_app.command("nix-cache", context_settings=settings)(nix_cache_secret)
    secrets_app.command("s3", context_settings=settings)(s3_secret)
    secrets_app.command("git-token", context_settings=settings)(git_token_secret)
    secrets_app.command("git-ssh-key", context_settings=settings)(git_ssh_key_secret)
