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
    """``--host``, else the first docker or native host: the plan names it."""
    if name is not None:
        chosen = dev.host(name)
        if chosen is None:
            reject("dev-host-unknown", f"no build host {name!r} is declared")
    else:
        chosen = next((h for h in dev.hosts if h.kind != "cluster"), None)
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
        secret = load_credential(cache.signing_credential, "nix-signing-key")
        keys = (
            load_credential(cache.write_credentials, "s3-keys")
            if cache.write_credentials
            else None
        )
        derived = public_key(secret)
    except (InfraError, DevError) as error:
        reject(error.code, str(error))
    if derived != cache.public_key:
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
        tag = tag_of(root_dir, ref, rev) if rev is not None else None
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
        digest = plan_digest(plan)
        if approve is None:
            command = f"piceli dev nix build {target} --cluster {cluster} --ref {ref} --publish {publish} --host {chosen.name}"
            if name:
                command += f" --name {name}"
            if image:
                command += " --image"
            if plan["branch"] is not None:
                command += f" --branch {plan['branch']}"
            say(f"publish {target} at {rev[:12]} to {cache.store_url}")
            say(f"  signed by {cache.key_name}; manifest {plan['manifest']}")
            for key in plan["pointers"]:
                say(f"  pointer {key}")
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
                           rev, target)
        )  # fmt: skip
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


def _image_sidecar(
    directory: Path,
    name: str,
    image: dict[str, Any],
    store_path: str,
    rev: str | None,
    target: str,
) -> Path:
    """``DIR/NAME.image.json``: what ``Image.from_build`` reads (the image's
    path relative to it, its sha256 and format, the store path, the rev)."""
    import json
    import os

    path = directory / f"{name}.image.json"
    body = {
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
    from piceli.dev.model import DevError
    from piceli.dev.nix import (
        manifest,
        publisher_argv,
        run_publisher,
        sign_manifest,
        upload_manifest,
        upload_pointers,
    )

    say(f"publishing {store_path} to {cache.store_url} (on {chosen.name})")
    request = {
        "store_path": store_path,
        "store_url": cache.store_url,
        "signing_key": secret,
        "s3": None
        if s3_keys is None
        else {"access_key_id": s3_keys[0], "secret_access_key": s3_keys[1]},
    }
    try:
        outcome = run_publisher(
            publisher_argv(chosen, str(profile.image)), request, say=say
        )
        del request
        signed = sign_manifest(
            manifest(
                name=plan["name"], commit=plan["commit"], tag=plan["tag"],
                target=plan["attribute"],
                published=outcome, host=chosen.name, key=cache.key_name,
                image=found_image,
            ),
            secret,
        )  # fmt: skip
        url = upload_manifest(cache, signed, s3_keys=s3_keys)
        pointers = upload_pointers(
            cache, signed, plan.get("pointers") or [], secret, s3_keys=s3_keys
        )
    except DevError as error:
        fail(error.code, str(error))
    say(f"published; manifest {url}")
    for body in pointers:
        say(f"  {body['ref']} -> {str(body['commit'])[:12]} ({body['published_at']})")
    return {
        "cache": cache.store_url,
        "public_key": cache.public_key,
        "nar_hash": outcome["nar_hash"],
        "nar_size": outcome["nar_size"],
        "signatures": outcome.get("signatures") or [],
        "system": outcome.get("system"),
        "closure_size": signed.get("closure_size"),
        "manifest": plan["manifest"],
        "manifest_url": url,
        "manifest_body": signed,
        "pointers": [
            {k: body[k] for k in ("ref", "commit", "published_at")} for body in pointers
        ],
    }


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
        root = toplevel(repo)
        kept = kept_commits(root, keep_tags, main)
        keys = None
        if cache.scheme == "s3":
            access, _, secret = load_credential(
                str(cache.write_credentials), "s3-keys"
            ).partition(":")
            keys = (access, secret)
        objects = CacheObjects(cache, keys)
        plan = gc_plan(
            objects, kept, keep_tags=keep_tags, main=main,
            grace_seconds=float(grace), now=time.time(),
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
) -> None:
    """Store a bucket's S3 access keys (an access key id and its secret)."""
    from piceli.infra.machines.credentials import save_credential
    from piceli.k8s.cli.infra import _guard, _no_secret_arguments, _read_secret

    _no_secret_arguments(context)
    if not prompt:
        reject("infra-prompt-required", "pass --prompt: the keys are read from stdin")
    with _guard():
        access = _read_secret("S3 access key id: ").strip()
        secret = _read_secret("S3 secret access key (not echoed): ").strip()
        if not access or ":" in access or not secret:
            reject(
                "infra-credential-invalid",
                "give the access key id, then the secret, on two lines",
            )
        save_credential(name, "s3-keys", f"{access}:{secret}")
        del secret
    say(f"stored credential {name} (s3-keys); the secret was not printed")
    emit_json({"state": "stored", "name": name, "kind": "s3-keys"})


def register(dev_app: typer.Typer) -> None:
    from piceli.k8s.cli.cluster import secrets_app

    dev_app.add_typer(nix_app, name="nix")
    settings: dict[str, Any] = {
        "allow_extra_args": True,
        "ignore_unknown_options": True,
    }
    secrets_app.command("nix-cache", context_settings=settings)(nix_cache_secret)
    secrets_app.command("s3", context_settings=settings)(s3_secret)
