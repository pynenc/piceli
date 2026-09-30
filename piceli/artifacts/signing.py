"""Signing published images with cosign and a key file (no transparency log).

:class:`CosignSigner` signs each digest a publish pushed (the image index,
every platform manifest and every attached SBOM and provenance) with
``cosign sign --key``. It is the least-dependency choice: one pinned binary
(cosign 3), a key pair the owner keeps, no identity provider and no public
service. Signatures are stored in the same repository in the Sigstore bundle
format as OCI referrers of the signed manifest (cosign 3's default), next to
the SBOM and provenance.

* The key file must be private (no group or other access). Its sha256 and the
  cosign binary's are part of the publish plan, so the approval names them.
* cosign runs with a private, temporary ``HOME`` and ``DOCKER_CONFIG``: the
  registry credentials are written there (mode 0600) for the call and removed
  after it, never passed as arguments. ``COSIGN_PASSWORD`` passes through
  from the caller's environment when set.
* A signing config without transparency log or timestamp authority is
  generated, so nothing is sent to the public Rekor log: the signature is
  verified with the public key alone.
* cosign's output is never printed or recorded; a failure is the fixed code
  ``sign-failed``.

Verify a published image::

    cosign verify --key cosign.pub --insecure-ignore-tlog=true \\
        registry.example/shop/api@sha256:<index digest>

Importing this module runs nothing.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from piceli.artifacts.plan import validate_digest
from piceli.artifacts.process import ToolPin
from piceli.artifacts.publish import PublishError
from piceli.artifacts.registry import RegistryCredentials, RegistryTarget
from piceli.tempfiles import temporary_directory

#: A signing config with no transparency log and no timestamp authority.
SIGNING_CONFIG = {
    "mediaType": "application/vnd.dev.sigstore.signingconfig.v0.2+json",
    "rekorTlogConfig": {},
    "tsaConfig": {},
}
_PASSTHROUGH = ("COSIGN_PASSWORD", "SSL_CERT_FILE", "NIX_SSL_CERT_FILE", "TMPDIR")
#: ``(argv, environment, timeout seconds) -> exit code``; output is discarded.
CosignRunner = Callable[[Sequence[str], Mapping[str, str], float], int]


def _run(argv: Sequence[str], environment: Mapping[str, str], timeout: float) -> int:
    try:
        result = subprocess.run(  # a pinned binary, a fixed argv
            list(argv),
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return -1
    return result.returncode


def private_key_sha256(path: Path) -> str:
    """The sha256 of a private (mode ``0600`` or stricter) key file."""
    if not isinstance(path, Path) or not path.is_absolute():
        raise PublishError("sign-key-invalid", "the key path must be absolute")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise PublishError("sign-key-invalid", "cannot read the key file") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            raise PublishError("sign-key-invalid", "not a key file")
        if info.st_mode & 0o077:
            raise PublishError(
                "sign-key-invalid", "the key file must not be group/world readable"
            )
        with os.fdopen(os.dup(fd), "rb") as stream:
            body = stream.read()
    finally:
        os.close(fd)
    import hashlib

    return "sha256:" + hashlib.sha256(body).hexdigest()


@dataclass
class CosignSigner:
    """Sign with ``cosign sign --key`` (see the module docstring)."""

    cosign: ToolPin
    key: Path = field(repr=False)
    credentials: RegistryCredentials | None = field(default=None, repr=False)
    ca_file: Path | None = field(default=None, repr=False)
    runner: CosignRunner = field(default=_run, repr=False)
    timeout: float = 300.0
    key_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        self.key_sha256 = private_key_sha256(self.key)

    def public(self) -> dict[str, Any]:
        return {
            "tool": "cosign",
            "tool_sha256": self.cosign.sha256,
            "key_sha256": self.key_sha256,
            "transparency_log": False,
        }

    def sign(
        self, target: RegistryTarget, repository: str, digests: Sequence[str]
    ) -> list[dict[str, Any]]:
        """Sign ``repository@digest`` for each digest; the signed references."""
        if private_key_sha256(self.key) != self.key_sha256:
            raise PublishError("sign-key-invalid", "the key changed since the plan")
        try:
            self.cosign.verify()
        except (OSError, ValueError):
            raise PublishError("tool-pin-mismatch") from None
        signed = []
        with temporary_directory("cosign") as home:
            environment = self._environment(home, target)
            config = home / "signing-config.json"
            config.write_text(json.dumps(SIGNING_CONFIG))
            for value in dict.fromkeys(digests):
                validate_digest(value)
                reference = f"{target.registry}/{repository}@{value}"
                argv = [
                    str(self.cosign.path),
                    "sign",
                    "--key",
                    str(self.key),
                    "--signing-config",
                    str(config),
                    "--yes",
                ]
                if not target.tls:
                    argv.append("--allow-http-registry")
                if self.ca_file is not None:
                    argv += ["--registry-cacert", str(self.ca_file)]
                if self.runner([*argv, reference], environment, self.timeout) != 0:
                    raise PublishError("sign-failed")
                signed.append({"digest": value, "tool": "cosign"})
        return signed

    def _environment(self, home: Path, target: RegistryTarget) -> dict[str, str]:
        docker = home / "docker"
        docker.mkdir(mode=0o700)
        auths: dict[str, Any] = {}
        credentials = self.credentials
        if credentials is not None and credentials.scheme == "basic":
            assert credentials.username is not None
            assert credentials.password is not None
            raw = f"{credentials.username}:{credentials.password}".encode()
            auths[target.registry] = {"auth": base64.b64encode(raw).decode()}
        elif credentials is not None:
            auths[target.registry] = {"registrytoken": credentials.token}
        path = docker / "config.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump({"auths": auths}, stream)
        environment = {
            "HOME": str(home),
            "DOCKER_CONFIG": str(docker),
            "PATH": os.defpath,
        }
        for name in _PASSTHROUGH:
            if name in os.environ:
                environment[name] = os.environ[name]
        return environment


def verify_argv(
    cosign: Path, public_key: Path, reference: str, *, http: bool = False
) -> list[str]:
    """The ``cosign verify`` command for a signature made by :class:`CosignSigner`."""
    argv = [
        str(cosign),
        "verify",
        "--key",
        str(public_key),
        "--insecure-ignore-tlog=true",
    ]
    if http:
        argv.append("--allow-http-registry")
    return [*argv, reference]
