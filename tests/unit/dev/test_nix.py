"""Nix builds on build hosts, the signed binary cache and manifests (0.24.0).

Fakes only: no network, no ``nix``. The opt-in test with the real ``nix`` is
``test_nix_real.py``.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.dev import nix
from piceli.dev.model import BuildHost, DevBuilds, DevError, DevProfile
from piceli.dev.nix import NixCache
from piceli.dev.nix_publish import RESULT_MARKER, copy_argv, path_info, publish

IMAGE = "ghcr.io/example/builder@sha256:" + "a" * 64
REV = "0123456789abcdef0123456789abcdef01234567"
STORE = "/nix/store/" + "a" * 32 + "-nixos-system-web-1-24.11"
SECRET = nix.generate_key("cache-1")
PUBLIC = nix.public_key(SECRET)


def _codes(call: Any) -> str:
    with pytest.raises(DevError) as raised:
        call()
    return raised.value.code


# ------------------------------------------------------------------ keys


def test_generated_keys_are_nix_keys_and_sign_and_verify() -> None:
    name, _, body = SECRET.partition(":")
    assert name == "cache-1" and len(base64.b64decode(body)) == 64
    assert PUBLIC.startswith("cache-1:") and len(base64.b64decode(PUBLIC[8:])) == 32
    # The secret key's second half is the public key, as in Nix.
    assert base64.b64decode(body)[32:] == base64.b64decode(PUBLIC[8:])
    signature = nix.sign(SECRET, b"message")
    assert signature.startswith("cache-1:")
    assert nix.verify(PUBLIC, b"message", signature)
    assert not nix.verify(PUBLIC, b"other", signature)
    other = nix.public_key(nix.generate_key("cache-1"))
    assert not nix.verify(other, b"message", signature)
    renamed = "cache-2:" + PUBLIC.partition(":")[2]
    assert not nix.verify(renamed, b"message", signature)
    assert _codes(lambda: nix.check_public_key("cache-1:AAAA")) == "dev-nix-key-invalid"
    assert _codes(lambda: nix.generate_key("bad name")) == "dev-nix-key-invalid"


# ------------------------------------------------------------------ cache


def test_a_cache_on_an_infomaniak_bucket_knows_its_s3_endpoint() -> None:
    from piceli.infra import Bucket, Infomaniak

    bucket = Bucket("nix-cache", Infomaniak(credentials="infomaniak"))
    cache = NixCache(
        PUBLIC,
        bucket=bucket,
        write_credentials="cache-s3-write",
    )
    assert cache.store_url == (
        "s3://nix-cache?endpoint=s3.pub1.infomaniak.cloud&region=us-east-1"
    )
    assert cache.key_name == "cache-1" and cache.signing_credential == "cache-1"
    assert cache.manifest_url("web-1", REV) == (
        f"https://s3.pub1.infomaniak.cloud/nix-cache/piceli/web-1/{REV}.json"
    )
    assert cache.describe() == {
        "store_url": cache.store_url,
        "public_key": PUBLIC,
        "signing_key": "cache-1",
        "write_credentials": "cache-s3-write",
    }


def test_cache_urls_and_refusals(tmp_path: Path) -> None:
    s3 = NixCache(
        PUBLIC,
        url="s3://bin?endpoint=s3.example.net&region=eu-1",
        write_credentials="k",
    )
    assert s3.store_url == "s3://bin?endpoint=s3.example.net&region=eu-1"
    local = NixCache(PUBLIC, url=f"file://{tmp_path}")
    assert local.store_url == f"file://{tmp_path}" and local.scheme == "file"
    assert NixCache(PUBLIC, url="https://cache.example.net").scheme == "https"
    for bad in (
        lambda: NixCache(PUBLIC),
        lambda: NixCache("nope", url=f"file://{tmp_path}"),
        lambda: NixCache(PUBLIC, url="s3://bin?endpoint=h"),  # no credentials
        lambda: NixCache(PUBLIC, url="s3://bin?secret-key=/k", write_credentials="k"),
        lambda: NixCache(PUBLIC, url="https://user:pw@cache.example.net"),
        lambda: NixCache(PUBLIC, url="ftp://cache.example.net"),
        lambda: NixCache(PUBLIC, url="file://relative"),
    ):
        assert _codes(bad) == "dev-nix-cache-invalid"


def test_a_bucket_without_a_known_endpoint_needs_one() -> None:
    from piceli.infra import Bucket, OpenStack

    provider = OpenStack(
        credentials="os", auth_url="https://keystone.example.net/v3", region="r1",
        public_network="ext", flavors=["small"],
    )  # fmt: skip
    bucket = Bucket("nix-cache", provider)
    assert (
        _codes(lambda: NixCache(PUBLIC, bucket=bucket, write_credentials="k"))
        == "dev-nix-cache-invalid"
    )
    cache = NixCache(
        PUBLIC, bucket=bucket, endpoint="s3.example.net", write_credentials="k"
    )
    assert cache.store_url.startswith("s3://nix-cache?endpoint=s3.example.net")


# ------------------------------------------------------------------ argv


def test_the_build_and_publish_commands_are_exact_argv() -> None:
    target = nix.installable("nixosConfigurations.web-1.config.system.build.toplevel")
    assert target == ".#nixosConfigurations.web-1.config.system.build.toplevel"
    assert nix.build_argv(target) == [
        "nix", "--extra-experimental-features", "nix-command flakes", "build",
        "--print-build-logs", "--out-link", "piceli-nix-out", "--print-out-paths",
        target,
    ]  # fmt: skip
    assert copy_argv("nix", "s3://bin?region=r", "/tmp/k/signing.key", STORE) == [
        "nix", "--extra-experimental-features", "nix-command flakes", "copy",
        "--to", "s3://bin?region=r&secret-key=/tmp/k/signing.key", STORE,
    ]  # fmt: skip
    assert copy_argv("nix", "file:///c", "/k", STORE)[-2] == "file:///c?secret-key=/k"
    assert nix.default_name(target, image=False) == "web-1"
    assert nix.default_name(target, image=True) == "web-1-image"
    assert nix.default_name(".#packages.x86_64-linux.default", False) is None
    for bad in ("-L", "github:o/r#x", ".#a..b", "a b", ".#$(x)"):
        assert _codes(lambda bad=bad: nix.installable(bad)) == "dev-nix-invalid"
    assert _codes(lambda: nix.check_name("Web_1")) == "dev-nix-invalid"
    lines = ["building...", STORE, "warning: x"]
    assert nix.store_path_of(lines) == STORE
    assert nix.store_path_of(["no path"]) is None
    assert "piceli-nix-out/*.qcow2" in nix.image_patterns()


def test_path_info_reads_both_json_shapes() -> None:
    entry = {"narHash": "sha256:x", "narSize": 3}
    assert path_info(json.dumps({STORE: entry}), STORE) == entry
    assert path_info(json.dumps([{"path": STORE, **entry}]), STORE)["narSize"] == 3
    assert path_info("not json", STORE) == {}


# ------------------------------------------------------------------ manifest


def _published() -> dict[str, Any]:
    return {
        "store_path": STORE,
        "nar_hash": "sha256:1b8m03r63zqhnjf7l5wnldhh7c134ap5vpj0850ymkq1iyzicy5s",
        "nar_size": 1234,
        "closure_size": 98765,
        "system": "x86_64-linux",
    }


def test_nar_hashes_take_the_narinfo_form() -> None:
    sri = "sha256-SILcrlSduzd4llxzRFBQ05O+lzrcZcZPC+bgMUVfO5U="
    nix32 = "sha256:159vbx2k3q761d7wcrfw7abvx4yka1848wswjrw3gfwxajpdr0j8"
    assert nix.nar_hash(sri) == nix32
    assert nix.nar_hash(nix32) == nix32


def test_the_manifest_is_signed_and_a_change_breaks_it() -> None:
    body = nix.manifest(
        name="web-1", commit=REV, tag="v1.4.0",
        target=".#nixosConfigurations.web-1.config.system.build.toplevel",
        published=_published(), host="nix-1", key="cache-1",
        image={"file": "/x/run/piceli-nix-out/nixos.qcow2", "format": "qcow2",
               "bytes": 9, "sha256": "f" * 64},
        now=dt.datetime(2026, 10, 22, 10, 0, tzinfo=dt.UTC),
    )  # fmt: skip
    assert body == {
        "schema": "piceli.nix-build.v1",
        "name": "web-1",
        "commit": REV,
        "tag": "v1.4.0",
        "attribute": ".#nixosConfigurations.web-1.config.system.build.toplevel",
        "store_path": STORE,
        "nar_hash": _published()["nar_hash"],
        "nar_size": 1234,
        "closure_size": 98765,
        "system": "x86_64-linux",
        "built_at": "2026-10-22T10:00:00Z",
        "host": "nix-1",
        "image": {"file": "nixos.qcow2", "format": "qcow2", "bytes": 9,
                  "sha256": "f" * 64},
        "signed_by": "cache-1",
    }  # fmt: skip
    signed = nix.sign_manifest(body, SECRET)
    assert nix.verify_manifest(signed, PUBLIC)
    assert SECRET.partition(":")[2] not in json.dumps(signed)
    for key, value in (
        ("store_path", STORE + "x"),
        ("commit", "f" * 40),
        ("tag", "v9"),
    ):
        assert not nix.verify_manifest({**signed, key: value}, PUBLIC)
    other = nix.public_key(nix.generate_key("cache-1"))
    assert not nix.verify_manifest(signed, other)
    assert not nix.verify_manifest({**signed, "schema": "x"}, PUBLIC)


def test_a_narinfo_signature_is_checked_like_nix() -> None:
    ref = "b" * 32 + "-glibc"
    fields = _published()
    fingerprint = (
        f"1;{STORE};{fields['nar_hash']};{fields['nar_size']};/nix/store/{ref}"
    )
    narinfo = "\n".join(
        [
            f"StorePath: {STORE}",
            "URL: nar/x.nar.xz",
            f"NarHash: {fields['nar_hash']}",
            f"NarSize: {fields['nar_size']}",
            f"References: {ref}",
            f"Sig: {nix.sign(SECRET, fingerprint.encode())}",
        ]
    )
    assert nix.verify_narinfo(narinfo, PUBLIC)
    assert not nix.verify_narinfo(
        narinfo.replace("NarSize: 1234", "NarSize: 1"), PUBLIC
    )


def test_verification_is_standard_library_ed25519() -> None:
    import ast
    import inspect

    from piceli.dev import nix_verify

    imported = {
        (node.module if isinstance(node, ast.ImportFrom) else alias.name).split(".")[0]
        for node in ast.walk(ast.parse(inspect.getsource(nix_verify)))
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert imported <= {"__future__", "base64", "datetime", "hashlib", "hmac",
                        "json", "re", "urllib", "collections", "typing"}  # fmt: skip
    # RFC 8032, section 7.1, test 1.
    public = bytes.fromhex(
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
    )
    signature = bytes.fromhex(
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
    )
    assert nix_verify.ed25519_verify(public, b"", signature)
    assert not nix_verify.ed25519_verify(public, b"x", signature)
    # And signatures made with the cryptography package (the signing side).
    for message in (b"", b"manifest", bytes(range(256))):
        sig = nix.sign(SECRET, message)
        assert nix_verify.verify(PUBLIC, message, sig)
        assert not nix_verify.verify(PUBLIC, message + b"!", sig)


# ------------------------------------------------------------------ upload


def test_the_manifest_goes_to_s3_with_a_sigv4_put_and_no_secret() -> None:
    cache = NixCache(
        PUBLIC,
        url="s3://bin?endpoint=s3.example.net&region=eu-1",
        write_credentials="k",
    )
    signed = nix.sign_manifest(
        nix.manifest(name="web-1", commit=REV, tag=None, target=".#x", published=_published(),
                     host="nix-1", key="cache-1"),
        SECRET,
    )  # fmt: skip
    sent: list[Any] = []

    class Response:
        status = 200

        def __enter__(self) -> Response:
            return self

        def read(self) -> bytes:
            return b""

        def __exit__(self, *_: Any) -> None:
            return None

    def opener(request: Any) -> Response:
        sent.append(request)
        return Response()

    url = nix.upload_manifest(
        cache, signed, s3_keys=("AKIDEXAMPLE", "s3-secret-value"), opener=opener,
        now=dt.datetime(2026, 10, 22, 10, 0, tzinfo=dt.UTC),
    )  # fmt: skip
    assert url == f"https://s3.example.net/bin/piceli/web-1/{REV}.json"
    request = sent[0]
    assert request.get_method() == "PUT" and request.full_url == url
    headers = {k.lower(): v for k, v in request.header_items()}
    assert headers["authorization"].startswith(
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20261022/eu-1/s3/aws4_request"
    )
    assert "s3-secret-value" not in json.dumps(headers)
    assert json.loads(request.data) == signed

    def refused(_request: Any) -> Response:
        raise OSError("403 Forbidden: s3-secret-value")

    with pytest.raises(DevError) as raised:
        nix.upload_manifest(
            cache, signed, s3_keys=("A", "s3-secret-value"), opener=refused
        )
    assert raised.value.code == "dev-nix-manifest-failed"
    assert "s3-secret-value" not in str(raised.value)


def test_sigv4_matches_botocore(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("botocore")
    import botocore.auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    moment = dt.datetime(2026, 10, 22, 10, 0, tzinfo=dt.UTC)

    class Frozen(dt.datetime):
        @classmethod
        def utcnow(cls) -> Any:
            return moment.replace(tzinfo=None)

        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return moment if tz else moment.replace(tzinfo=None)

    monkeypatch.setattr(botocore.auth.datetime, "datetime", Frozen)
    url = "https://s3.example.net/bin/piceli/web-1/x.json"
    ours = nix._sigv4(
        "PUT", url, b"{}", region="eu-1", access_key="AKID", secret_key="SECRET",
        now=moment, content_type="application/json",
    )  # fmt: skip
    request = AWSRequest(
        method="PUT", url=url, data=b"{}",
        headers={"content-type": "application/json",
                 "x-amz-content-sha256": ours["x-amz-content-sha256"]},
    )  # fmt: skip
    botocore.auth.S3SigV4Auth(Credentials("AKID", "SECRET"), "s3", "eu-1").add_auth(
        request
    )
    assert ours["authorization"] == request.headers["Authorization"]


def test_the_manifest_goes_into_a_file_cache(tmp_path: Path) -> None:
    cache = NixCache(PUBLIC, url=f"file://{tmp_path}")
    signed = {"name": "web-1", "commit": REV, "schema": "x"}
    url = nix.upload_manifest(cache, signed, s3_keys=None)
    assert url == f"file://{tmp_path}/piceli/web-1/{REV}.json"
    assert json.loads((tmp_path / "piceli" / "web-1" / f"{REV}.json").read_text()) == (
        signed
    )


# ------------------------------------------------------------------ publisher


def test_the_publisher_signs_on_upload_and_removes_the_key() -> None:
    seen: list[dict[str, Any]] = []

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        env = kwargs["env"]
        entry: dict[str, Any] = {"argv": argv, "aws": env.get("AWS_SECRET_ACCESS_KEY")}
        if argv[3] == "copy":
            key_file = argv[5].rpartition("secret-key=")[2]
            mode = stat.S_IMODE(os.stat(key_file).st_mode)
            parent = stat.S_IMODE(os.stat(os.path.dirname(key_file)).st_mode)
            entry.update(key=Path(key_file).read_text(), mode=mode, parent=parent,
                         key_file=key_file)  # fmt: skip
            seen.append(entry)
            return subprocess.CompletedProcess(
                argv, 0, b"", f"copying {SECRET} s3-secret\n".encode()
            )
        seen.append(entry)
        if "--store" in argv:
            info = {STORE: {"narHash": "sha256:abc", "narSize": 7,
                            "signatures": ["cache-1:sig"]}}  # fmt: skip
            return subprocess.CompletedProcess(argv, 0, json.dumps(info).encode(), b"")
        if argv[3] == "path-info":
            assert "-S" in argv
            info = {
                STORE: {"deriver": "/nix/store/" + "c" * 32 + "-x.drv",
                        "closureSize": 99}
            }  # fmt: skip
            return subprocess.CompletedProcess(argv, 0, json.dumps(info).encode(), b"")
        show = {"/nix/store/x.drv": {"system": "x86_64-linux"}}
        return subprocess.CompletedProcess(argv, 0, json.dumps(show).encode(), b"")

    emitted: list[str] = []
    outcome = publish(
        {
            "store_path": STORE,
            "store_url": "s3://bin?endpoint=s3.example.net&region=eu-1",
            "signing_key": SECRET,
            "s3": {"access_key_id": "AKID", "secret_access_key": "s3-secret"},
        },
        runner=runner, emit=emitted.append,
        environ={"PATH": "/usr/bin", "AWS_PROFILE": "elsewhere"},
    )  # fmt: skip
    # The test machine's PATH may have no nix: the publisher looks in NIX_PATHS.
    if outcome.get("reason") == "dev-tool-missing":
        pytest.skip("no nix binary to point at on this machine")
    assert outcome == {
        "state": "published",
        "store_path": STORE,
        "nar_hash": "sha256:abc",
        "nar_size": 7,
        "signatures": ["cache-1:sig"],
        "system": "x86_64-linux",
        "closure_size": 99,
    }
    copy = seen[0]
    assert copy["key"] == SECRET and copy["mode"] == 0o600 and copy["parent"] == 0o700
    assert copy["aws"] == "s3-secret"
    assert not Path(copy["key_file"]).exists()
    assert not Path(copy["key_file"]).parent.exists()
    text = "\n".join(emitted) + json.dumps(outcome)
    assert SECRET not in text and "s3-secret" not in text
    assert seen[1]["argv"][-3:] == [
        "--store",
        "s3://bin?endpoint=s3.example.net&region=eu-1",
        STORE,
    ]


def test_a_failed_copy_is_a_code_and_still_removes_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import piceli.dev.nix_publish as module

    monkeypatch.setattr(module, "find_nix", lambda _env: "/fake/nix")
    files: list[str] = []

    def runner(argv: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
        files.append(argv[5].rpartition("secret-key=")[2])
        return subprocess.CompletedProcess(argv, 1, b"", b"error: 403")

    outcome = publish(
        {"store_path": STORE, "store_url": "file:///c", "signing_key": SECRET},
        runner=runner, emit=lambda _l: None, environ={},
    )  # fmt: skip
    assert outcome == {
        "state": "failed",
        "store_path": STORE,
        "reason": "dev-nix-publish-failed",
    }
    assert not Path(files[0]).exists()


def test_run_publisher_masks_and_reads_the_result_line() -> None:
    said: list[str] = []
    request = {"store_path": STORE, "signing_key": SECRET,
               "s3": {"access_key_id": "A", "secret_access_key": "s3-secret"}}  # fmt: skip
    inputs: list[bytes] = []

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        inputs.append(kwargs["input"])
        line = RESULT_MARKER + json.dumps({"state": "published", "nar_hash": "h"})
        return subprocess.CompletedProcess(
            argv, 0, line.encode(), f"leak {SECRET} s3-secret".encode()
        )

    outcome = nix.run_publisher(["python3"], request, runner=runner, say=said.append)
    assert outcome["nar_hash"] == "h"
    assert json.loads(inputs[0])["signing_key"] == SECRET  # stdin only
    assert SECRET not in "\n".join(said) and "s3-secret" not in "\n".join(said)

    def broken(argv: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
        line = RESULT_MARKER + json.dumps(
            {"state": "failed", "reason": "dev-nix-publish-failed"}
        )
        return subprocess.CompletedProcess(argv, 1, line.encode(), b"")

    with pytest.raises(DevError) as raised:
        nix.run_publisher(["python3"], request, runner=broken)
    assert raised.value.code == "dev-nix-publish-failed"


def test_the_publisher_runs_where_the_build_ran() -> None:
    docker = BuildHost.docker("nix-1", context="ssh://builder@nix-1")
    argv = nix.publisher_argv(docker, IMAGE)
    assert argv[:7] == ["docker", "--host", "ssh://builder@nix-1", "run", "--rm", "-i",
                        "--user"]  # fmt: skip
    joined = " ".join(argv[:-1])
    for flag in ("-v /nix:/nix:ro", "-e NIX_REMOTE=daemon", "--cap-drop ALL",
                 f"--entrypoint python3 {IMAGE} -I -c"):  # fmt: skip
        assert flag in joined, flag
    assert "RESULT_MARKER" in argv[-1]  # the publisher's source, no secret
    native = nix.publisher_argv(BuildHost.native("laptop"), IMAGE)
    assert native[1:3] == ["-I", "-c"]
    assert (
        _codes(lambda: nix.publisher_argv(BuildHost.cluster("builder-1"), IMAGE))
        == "dev-nix-host-unsupported"
    )


# ------------------------------------------------------------------ profile and host


def test_a_nix_profile_mounts_the_hosts_nix_into_docker_runs() -> None:
    from piceli.dev.hosts import ContainerCli, DockerPort, Entry
    from piceli.dev.jobs import run_job
    from tests.unit.dev.test_hosts import FakeCli

    profile = DevProfile.for_nix()
    assert profile.describe()["nix"] is True and profile.tools == ("nix",)
    assert "nix" not in DevProfile("rust").describe()
    host = BuildHost.docker("nix-1", context="ssh://builder@nix-1")
    dev = DevBuilds(node=None, image=IMAGE, profiles=[profile], hosts=[host])
    assert DevBuilds.from_dict(dev.describe()).profile("nix").nix is True
    fake = FakeCli()
    port = DockerPort(host, dev, cli=ContainerCli(host, runner=fake))
    job = run_job(
        dev, dev.profile("nix"), run="r-1",
        spec={"command": nix.build_argv(".#x"), "cwd": "app", "lineage_key": "app-nix",
              "sources": []},
        requester="agent-1", priority="agent",
    )  # fmt: skip
    port.create(job)
    assert port._claim(
        0, Entry("r-1", "agent-1", "agent", "2026-10-22T10:00:00.000Z"), job, IMAGE
    )
    argv = fake.calls[-1]
    joined = " ".join(argv)
    assert "-v /nix:/nix:ro" in joined and "-e NIX_REMOTE=daemon" in joined
    assert argv[-1].startswith('export PATH="$PATH:/nix/var/nix/profiles/default/bin')
    plain = DevBuilds(
        node=None, image=IMAGE, profiles=[DevProfile("rust")], hosts=[host]
    )
    job = run_job(
        plain, plain.profile(None), run="r-2",
        spec={"command": ["true"], "cwd": "app", "lineage_key": "k", "sources": []},
        requester="a", priority="agent",
    )  # fmt: skip
    port = DockerPort(host, plain, cli=ContainerCli(host, runner=fake))
    port.create(job)
    port._claim(1, Entry("r-2", "a", "agent", "2026-10-22T10:00:00.000Z"), job, IMAGE)
    assert "/nix" not in " ".join(fake.calls[-1])


# ------------------------------------------------------------------ CLI


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import piceli.dev.client as client
    import piceli.dev.hosts as hosts
    import piceli.k8s.cli.dev_nix as dev_nix
    from piceli.infra import Cluster, Node
    from piceli.infra.machines.credentials import save_credential
    from tests.unit.dev.test_pack import repo

    monkeypatch.setenv("PICELI_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    host = BuildHost.native("laptop", state_dir=str(tmp_path / "state"))
    dev = DevBuilds(
        node="builder-1", image=IMAGE, profiles=[DevProfile.for_nix()], hosts=[host]
    )
    cluster = Cluster(
        "c", api="https://10.255.0.1:6443", credentials="c",
        nodes=[Node("builder-1", arch="amd64", roles=["builder"])], dev=dev,
    )  # fmt: skip
    cache_dir = tmp_path / "cache"
    cache = NixCache(PUBLIC, url=f"file://{cache_dir}")
    real_load_dev = dev_nix.load_dev  # another ref than the fixture's loads for real
    monkeypatch.setattr(
        dev_nix,
        "load_dev",
        lambda ref: cluster.dev if ref == "infra.py:c" else real_load_dev(ref),
    )
    monkeypatch.setattr(dev_nix, "load_cache", lambda _ref: cache)
    monkeypatch.setattr(hosts, "probe", lambda h, **_: hosts.HostState(h, True))
    app = repo(tmp_path / "app", {"flake.nix": "{ outputs = _: {}; }"})
    monkeypatch.chdir(app)
    image_file = tmp_path / "out" / "piceli-nix-out" / "nixos.qcow2"
    image_file.parent.mkdir(parents=True)
    image_file.write_bytes(b"qcow2 bytes")
    runs: list[Any] = []

    def fake_run(port: Any, dev: Any, request: Any, **kwargs: Any) -> dict[str, Any]:
        runs.append(request)
        kwargs["log"](STORE)
        artifacts = [str(image_file)] if request.artifacts else []
        return {"run": "r-1", "state": "passed", "exit_code": 0, "durations": {},
                "artifacts": artifacts, "log_tail": [STORE]}  # fmt: skip

    monkeypatch.setattr(client, "run", fake_run)
    published: list[dict[str, Any]] = []

    def fake_publisher(argv: list[str], request: Any, **_: Any) -> dict[str, Any]:
        published.append({"argv": argv, **request})
        return {"state": "published", "store_path": request["store_path"],
                "nar_hash": "sha256:abc", "nar_size": 7,
                "signatures": ["cache-1:x"], "system": "x86_64-linux",
                "closure_size": 4096}  # fmt: skip

    monkeypatch.setattr(nix, "run_publisher", fake_publisher)
    save_credential("cache-1", "nix-signing-key", SECRET)
    return {"runs": runs, "published": published, "cache": cache_dir, "app": app,
            "save": save_credential}  # fmt: skip


def _invoke(*args: str, input: str | None = None) -> Any:
    from piceli.k8s.cli import app

    return CliRunner().invoke(app, list(args), input=input)


ATTR = "nixosConfigurations.web-1.config.system.build.toplevel"


def test_a_build_without_publish_is_a_plain_run(cli_env: dict[str, Any]) -> None:
    result = _invoke("dev", "nix", "build", ATTR, "--cluster", "infra.py:c", "--json")
    assert result.exit_code == 0, result.stdout + result.stderr
    body = json.loads(result.stdout)
    assert body["state"] == "built" and body["store_path"] == STORE
    assert body["publish"] is None and body["image"] is None and body["commit"] is None
    request = cli_env["runs"][0]
    assert request.command == nix.build_argv(f".#{ATTR}")
    assert request.profile == "nix" and request.artifacts == []
    assert cli_env["published"] == []


def test_publish_plans_first_then_runs_only_the_approved_digest(
    cli_env: dict[str, Any],
) -> None:
    from tests.unit.dev.test_pack import git

    git(cli_env["app"], "tag", "v1.0.0")
    args = ("dev", "nix", "build", ATTR, "--cluster", "infra.py:c", "--ref", "HEAD",
            "--publish", "infra.py:cache", "--image", "--json")  # fmt: skip
    planned = _invoke(*args)
    assert planned.exit_code == 3, planned.stdout + planned.stderr
    assert json.loads(planned.stdout)["plan"]["tag"] == "v1.0.0"
    plan = json.loads(planned.stdout)
    assert plan["state"] == "approval-required" and cli_env["runs"] == []
    assert plan["plan"]["name"] == "web-1-image" and plan["plan"]["host"] == "laptop"
    assert plan["plan"]["cache"]["public_key"] == PUBLIC
    assert plan["approve_command"].endswith(f"--approve {plan['digest']}")
    wrong = _invoke(*args, "--approve", "sha256:" + "0" * 64)
    assert wrong.exit_code == 2 and json.loads(wrong.stdout)["reason"] == (
        "dev-nix-plan-changed"
    )
    assert cli_env["runs"] == []
    done = _invoke(*args, "--approve", plan["digest"])
    assert done.exit_code == 0, done.stdout + done.stderr
    body = json.loads(done.stdout)
    assert body["state"] == "published" and body["store_path"] == STORE
    import hashlib

    assert body["image"]["sha256"] == hashlib.sha256(b"qcow2 bytes").hexdigest()
    rev = plan["plan"]["commit"]
    manifest = json.loads(
        (cli_env["cache"] / "piceli" / "web-1-image" / f"{rev}.json").read_text()
    )
    assert nix.verify_manifest(manifest, PUBLIC)
    assert (
        manifest["store_path"] == STORE and manifest["image"]["file"] == "nixos.qcow2"
    )
    assert body["publish"]["public_key"] == PUBLIC
    assert cli_env["runs"][0].root.ref == rev
    assert cli_env["published"][0]["signing_key"] == SECRET
    secret_body = SECRET.partition(":")[2]
    for text in (planned.stdout, planned.stderr, done.stdout, done.stderr):
        assert secret_body not in text


def test_builds_need_no_cluster_and_an_image_leaves_its_sidecar(
    cli_env: dict[str, Any], tmp_path: Path
) -> None:
    import hashlib

    from piceli.infra import Image
    from piceli.infra.machines.model import InfraError

    module = tmp_path / "builds.py"
    module.write_text(
        "from piceli.infra import BuildHost, DevBuilds, DevProfile\n"
        f"builds = DevBuilds(node=None, image={IMAGE!r},\n"
        f"    hosts=[BuildHost.native('laptop', state_dir={str(tmp_path / 'state')!r})],\n"
        "    profiles=[DevProfile.for_nix()])\n"
        "nothing = 1\n"
    )
    attr = "nixosConfigurations.web-1.config.system.build.openstackImage"
    out = tmp_path / "images"
    result = _invoke("dev", "nix", "build", attr, "--cluster", f"{module}:builds",
                     "--image", "--artifacts-dir", str(out), "--json")  # fmt: skip
    assert result.exit_code == 0, result.stdout + result.stderr
    body = json.loads(result.stdout)
    sidecar = out / "web-1-image.image.json"
    assert body["image"]["build"] == str(sidecar)
    written = json.loads(sidecar.read_text())
    digest = hashlib.sha256(b"qcow2 bytes").hexdigest()
    assert written["schema"] == "piceli.nix-image.v1" and written["sha256"] == digest
    assert written["store_path"] == STORE and written["format"] == "qcow2"
    image = Image.from_build("web-1-nixos", sidecar)
    assert image.sha256 == digest and Path(str(image.file)).read_bytes() == (
        b"qcow2 bytes"
    )
    refused = _invoke("dev", "nix", "build", attr, "--dev", f"{module}:nothing")
    assert json.loads(refused.stdout)["reason"] == "cluster-not-found"
    for bad in ("{", json.dumps({**written, "sha256": "x"}),
                json.dumps({**written, "file": "/etc/passwd"})):  # fmt: skip
        sidecar.write_text(bad)
        with pytest.raises(InfraError) as raised:
            Image.from_build("web-1-nixos", sidecar)
        assert raised.value.code == "infra-image-build-invalid"
    with pytest.raises(InfraError):
        Image.from_build("web-1-nixos", tmp_path / "missing.json")


def test_publish_refusals(cli_env: dict[str, Any]) -> None:
    base = ("dev", "nix", "build", ATTR, "--cluster", "infra.py:c")
    cases = {
        ("--publish", "infra.py:cache"): "dev-nix-invalid",  # no --ref
        ("--approve", "sha256:x"): "dev-nix-invalid",
        ("--host", "builder-1"): "dev-nix-host-unsupported",
        ("--host", "ghost"): "dev-host-unknown",
        ("--name", "Bad_Name"): "dev-nix-invalid",
    }
    for extra, code in cases.items():
        result = _invoke(*base, *extra)
        assert result.exit_code == 2, (extra, result.stdout)
        assert json.loads(result.stdout)["reason"] == code, extra
    bad = _invoke("dev", "nix", "build", "-L", "--cluster", "infra.py:c")
    assert bad.exit_code == 2


def test_a_key_that_is_not_the_caches_is_refused_before_building(
    cli_env: dict[str, Any],
) -> None:
    cli_env["save"]("cache-1", "nix-signing-key", nix.generate_key("cache-1"))
    args = ("dev", "nix", "build", ATTR, "--cluster", "infra.py:c", "--ref", "HEAD",
            "--publish", "infra.py:cache")  # fmt: skip
    digest = json.loads(_invoke(*args).stdout)["digest"]
    result = _invoke(*args, "--approve", digest)
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reason"] == "dev-nix-key-mismatch"
    assert cli_env["runs"] == []


def test_secrets_nix_cache_generates_prints_the_public_key_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PICELI_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    made = _invoke("secrets", "nix-cache", "cache-2", "--generate")
    assert made.exit_code == 0, made.stdout + made.stderr
    body = json.loads(made.stdout)
    path = tmp_path / "credentials" / "cache-2.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    stored = json.loads(path.read_text())
    assert stored["kind"] == "nix-signing-key"
    assert body["public_key"] == nix.public_key(stored["value"])
    secret_body = stored["value"].partition(":")[2]
    assert secret_body not in made.stdout + made.stderr
    shown = _invoke("secrets", "nix-cache", "cache-2")
    assert json.loads(shown.stdout) == {**body, "state": "present"}
    again = _invoke("secrets", "nix-cache", "cache-2", "--generate")
    assert again.exit_code == 2
    assert json.loads(again.stdout)["reason"] == "infra-credential-exists"
    imported = _invoke(
        "secrets", "nix-cache", "cache-3", "--prompt", input=SECRET + "\n"
    )
    assert json.loads(imported.stdout)["public_key"] == PUBLIC
    assert SECRET not in imported.stdout + imported.stderr
    refused = _invoke("secrets", "nix-cache", "cache-4", SECRET)
    assert refused.exit_code == 2 and SECRET not in refused.stdout + refused.stderr


def test_secrets_s3_stores_both_keys_and_prints_neither(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PICELI_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    result = _invoke("secrets", "s3", "cache-s3", "--prompt", input="AKID\ns3-secret\n")
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "s3-secret" not in result.stdout + result.stderr
    from piceli.infra.machines.credentials import load_credential

    assert load_credential("cache-s3", "s3-keys") == "AKID:s3-secret"
    missing = _invoke("secrets", "s3", "cache-s3", "--prompt", input="AKID\n")
    assert missing.exit_code == 2


# ------------------------------------------------------------------ retention


def _narinfo(part: str, refs: list[str]) -> str:
    return (
        "\n".join(
            [
                f"StorePath: /nix/store/{part}-p",
                f"URL: nar/{part}.nar.xz",
                "NarHash: sha256:x",
                "NarSize: 1",
                f"References: {' '.join(f'{r}-p' for r in refs)}",
            ]
        )
        + "\n"
    )


def _gc_world(tmp_path: Path) -> dict[str, Any]:
    """A repository with tags v1..v3 and a newer main, and a file cache with
    one manifest per commit; main's closure reaches v1's path."""
    from tests.unit.dev.test_pack import git

    source = tmp_path / "app"
    source.mkdir()
    git(source, "init", "-q", "-b", "main")
    git(source, "config", "user.email", "dev@example.com")
    git(source, "config", "user.name", "Dev")
    commits = []
    for index in range(4):
        (source / "f").write_text(str(index))
        git(source, "add", "-A")
        env = {**os.environ, "GIT_COMMITTER_DATE": f"2026-01-0{index + 1}T00:00:00Z"}
        subprocess.run(
            ["git", "-C", str(source), "commit", "-q", "-m", f"c{index}"],
            check=True, env=env,
        )  # fmt: skip
        commits.append(git(source, "rev-parse", "HEAD"))
        if index < 3:
            git(source, "tag", f"v{index + 1}")
    cache = tmp_path / "cache"
    parts = [c * 32 for c in "abcd"]
    shared, orphan, young = "e" * 32, "f" * 32, "g" * 32
    refs = {0: [shared], 1: [shared], 2: [], 3: [parts[0]]}
    for index, commit in enumerate(commits):
        body = {"store_path": f"/nix/store/{parts[index]}-p"}
        (cache / "piceli" / "web-1").mkdir(parents=True, exist_ok=True)
        (cache / "piceli" / "web-1" / f"{commit}.json").write_text(json.dumps(body))
        (cache / f"{parts[index]}.narinfo").write_text(
            _narinfo(parts[index], refs[index])
        )
    for part in (shared, orphan, young):
        (cache / f"{part}.narinfo").write_text(_narinfo(part, []))
    (cache / "nar").mkdir()
    for part in [*parts, shared, orphan, young]:
        (cache / "nar" / f"{part}.nar.xz").write_text("nar")
    (cache / f"{orphan}.ls").write_text("{}")
    old = 1_000_000.0
    for path in cache.rglob("*"):
        if path.is_file() and young not in path.name:
            os.utime(path, (old, old))
    return {"source": source, "cache": cache, "commits": commits, "parts": parts,
            "shared": shared, "orphan": orphan, "young": young}  # fmt: skip


def test_gc_keeps_the_last_tags_main_and_everything_they_reach(
    tmp_path: Path,
) -> None:
    import time

    world = _gc_world(tmp_path)
    kept = nix.kept_commits(world["source"], 2, "main")
    c = world["commits"]
    assert kept == {c[1]: ["v2"], c[2]: ["v3"], c[3]: ["main"]}
    cache = NixCache(PUBLIC, url=f"file://{world['cache']}")
    objects = nix.CacheObjects(cache)
    plan = nix.gc_plan(
        objects, kept, keep_tags=2, main="main", grace_seconds=3600, now=time.time()
    )
    orphan, parts = world["orphan"], world["parts"]
    # v1's manifest goes; its path stays: main's closure reaches it.
    assert plan["delete"] == sorted(
        [f"piceli/web-1/{c[0]}.json", f"{orphan}.narinfo", f"{orphan}.ls",
         f"nar/{orphan}.nar.xz"]
    )  # fmt: skip
    assert f"{parts[0]}.narinfo" not in plan["delete"]
    assert plan["kept_paths"] == 6  # b, c, d, a (via main), shared, the young one
    assert nix.gc_apply(objects, plan) == 4
    assert not (world["cache"] / f"{orphan}.narinfo").exists()
    assert (world["cache"] / f"nar/{world['young']}.nar.xz").exists()
    again = nix.gc_plan(
        objects, kept, keep_tags=2, main="main", grace_seconds=3600, now=time.time()
    )
    assert again["delete"] == []


def test_gc_command_plans_then_deletes_the_approved_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import piceli.k8s.cli.dev_nix as dev_nix

    world = _gc_world(tmp_path)
    cache = NixCache(PUBLIC, url=f"file://{world['cache']}")
    monkeypatch.setattr(dev_nix, "load_cache", lambda _ref: cache)
    args = ("dev", "nix", "gc", "--cache", "infra.py:cache", "--repo",
            str(world["source"]), "--json")  # fmt: skip
    planned = _invoke(*args)
    assert planned.exit_code == 3, planned.stdout + planned.stderr
    plan = json.loads(planned.stdout)
    # Default: 5 tags and main keep every manifest; only the orphan goes.
    assert plan["keep_tags"] == 5 and plan["main"] == "main"
    assert not any(key.startswith("piceli/") for key in plan["delete"])
    assert (world["cache"] / f"{world['orphan']}.narinfo").exists()
    wrong = _invoke(*args, "--approve", "sha256:" + "0" * 64)
    assert json.loads(wrong.stdout)["reason"] == "dev-nix-plan-changed"
    done = _invoke(*args, "--approve", plan["digest"])
    assert done.exit_code == 0, done.stdout + done.stderr
    assert json.loads(done.stdout)["deleted"] == 3
    clean = _invoke(*args)
    assert clean.exit_code == 0 and json.loads(clean.stdout)["state"] == "clean"


def test_s3_listing_and_deletes_are_signed_requests() -> None:
    cache = NixCache(
        PUBLIC, url="s3://bin?endpoint=s3.example.net&region=eu-1",
        write_credentials="w",
    )  # fmt: skip
    calls: list[Any] = []
    pages = [
        b"<ListBucketResult xmlns='http://s3.amazonaws.com/doc/2006-03-01/'>"
        b"<IsTruncated>true</IsTruncated><NextContinuationToken>t1</NextContinuationToken>"
        b"<Contents><Key>a.narinfo</Key><LastModified>2026-10-01T00:00:00.000Z</LastModified></Contents>"
        b"</ListBucketResult>",
        b"<ListBucketResult><IsTruncated>false</IsTruncated>"
        b"<Contents><Key>nar/a.nar.xz</Key><LastModified>2026-10-01T00:00:00Z</LastModified></Contents>"
        b"</ListBucketResult>",
    ]

    class Response:
        def __init__(self, status: int, body: bytes) -> None:
            self.status, self.body = status, body

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def read(self) -> bytes:
            return self.body

    def opener(request: Any) -> Response:
        calls.append(request)
        if request.get_method() == "GET":
            return Response(200, pages[len(calls) - 1])
        return Response(204, b"")

    objects = nix.CacheObjects(cache, ("AKID", "s3-secret"), opener=opener)
    listed = objects.list()
    assert [key for key, _ in listed] == ["a.narinfo", "nar/a.nar.xz"]
    assert "continuation-token=t1" in calls[1].full_url
    objects.delete("nar/a.nar.xz")
    assert calls[-1].get_method() == "DELETE"
    assert calls[-1].full_url == "https://s3.example.net/bin/nar/a.nar.xz"
    for request in calls:
        headers = dict(request.header_items())
        assert "AKID/" in headers["Authorization"]
        assert "s3-secret" not in json.dumps(headers)


# ------------------------------------------------------------------ 0.26.0: inputs

PRIVATE_LOCK = json.dumps(
    {
        "version": 7,
        "root": "root",
        "nodes": {
            "root": {"inputs": {"app": "app"}},
            "app": {
                "locked": {
                    "type": "git",
                    "url": "ssh://git@github.com/org/app",
                    "rev": "c" * 40,
                    "narHash": "sha256-AAAA",
                }
            },
        },
    }
)


def test_private_inputs_are_fetched_on_the_host_before_the_build(
    cli_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from piceli.dev import nix_inputs
    from tests.unit.dev.test_pack import commit

    commit(cli_env["app"], {"flake.lock": PRIVATE_LOCK})
    nix_inputs.save_git_token("app-token", "github.com", "git", "tok-" + "z" * 20)
    fetched: list[dict[str, Any]] = []

    def fake_fetcher(argv: Any, request: Any, tried: Any, **_: Any) -> Any:
        fetched.append({"request": request, "tried": tried,
                        "runs_before": len(cli_env["runs"])})  # fmt: skip
        return {"state": "fetched", "inputs": [{"name": "app"}]}

    monkeypatch.setattr(nix_inputs, "run_fetcher", fake_fetcher)
    args = ("dev", "nix", "build", ATTR, "--cluster", "infra.py:c", "--ref", "HEAD",
            "--publish", "infra.py:cache", "--input", "app=app-token", "--json")  # fmt: skip
    planned = _invoke(*args)
    assert planned.exit_code == 3, planned.stdout + planned.stderr
    plan = json.loads(planned.stdout)
    assert plan["plan"]["inputs"] == [
        {"input": "app", "url": "git+ssh://git@github.com/org/app",
         "credential": "app-token", "why": "declared"},
    ]  # fmt: skip
    assert "--input app=app-token" in plan["approve_command"]
    done = _invoke(*args, "--approve", plan["digest"])
    assert done.exit_code == 0, done.stdout + done.stderr
    ((call),) = fetched
    assert call["runs_before"] == 0 and len(cli_env["runs"]) == 1
    assert call["request"]["inputs"][0]["mode"] == "askpass"
    assert "z" * 20 not in done.stdout + done.stderr


def test_an_unreachable_input_refuses_before_the_build(
    cli_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from piceli.dev import nix_inputs
    from piceli.dev.model import DevError
    from tests.unit.dev.test_pack import commit

    commit(cli_env["app"], {"flake.lock": PRIVATE_LOCK})

    def unreachable(argv: Any, request: Any, tried: Any, **_: Any) -> Any:
        raise DevError("infra-nix-input-unreachable", f"app {tried[0]['url']} (...)")

    monkeypatch.setattr(nix_inputs, "run_fetcher", unreachable)
    refused = _invoke("dev", "nix", "build", ATTR, "--cluster", "infra.py:c",
                      "--input", "app", "--json")  # fmt: skip
    assert refused.exit_code == 2
    assert json.loads(refused.stdout)["reason"] == "infra-nix-input-unreachable"
    assert cli_env["runs"] == []
    unknown = _invoke("dev", "nix", "build", ATTR, "--cluster", "infra.py:c",
                      "--input", "nope", "--json")  # fmt: skip
    assert unknown.exit_code == 2
    assert json.loads(unknown.stdout)["reason"] == "dev-nix-invalid"


# ------------------------------------------------------------------ 0.26.2

MACHINES = """
from piceli.infra import NixCache, NixHost, NixRelease, Source, Tag

hosts = Source("https://example.org/org/hosts.git", name="hosts")
cache = NixCache("PUBLIC", url="file:///nonexistent/cache")
web = NixHost("web-1", server="ssh://root@web-1.example.org", flake=".#web-1",
              follow=Tag("web-v*"), cache=cache)
web_release = NixRelease(web, source=hosts, builder="laptop",
                         inputs={"app": "app-read"})
"""


def test_an_image_build_fetches_the_inputs_the_composition_releases_cover(
    cli_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production report: ``dev nix build .#web-1-image --image --dev
    machines.py:builds`` fetched no private input (no ``--input``, no
    ``--publish``), where the release of the same flake fetched it first."""
    import piceli.k8s.cli.dev_nix as dev_nix
    from piceli.dev import nix_inputs
    from tests.unit.dev.test_pack import commit

    commit(cli_env["app"], {"flake.lock": PRIVATE_LOCK,
                            "machines.py": MACHINES.replace("PUBLIC", PUBLIC)})  # fmt: skip
    loaded = dev_nix.load_dev
    monkeypatch.setattr(dev_nix, "load_dev", lambda _ref: loaded("infra.py:c"))
    fetched: list[dict[str, Any]] = []

    def fake_fetcher(argv: Any, request: Any, tried: Any, **_: Any) -> Any:
        fetched.append(request)
        return {"state": "fetched", "inputs": [{"name": "app"}]}

    monkeypatch.setattr(nix_inputs, "run_fetcher", fake_fetcher)
    done = _invoke("dev", "nix", "build", "packages.x86_64-linux.web-1-image",
                   "--image", "--dev", "machines.py:builds", "--json")  # fmt: skip
    assert done.exit_code == 0, done.stdout + done.stderr
    ((request),) = fetched
    (item,) = request["inputs"]
    assert item["name"] == "app" and item["locked"]["rev"] == "c" * 40
    assert item["locked"]["narHash"] == "sha256-AAAA"


def test_every_path_fetches_a_git_input_with_its_whole_history() -> None:
    from piceli.dev import nix_fetch

    locked = {"type": "git", "url": "ssh://git@github.com/org/app",
              "rev": "c" * 40, "revCount": 7, "narHash": "sha256-AAAA",
              "shallow": True}  # fmt: skip
    attrs = nix_fetch.fetch_attrs(locked)
    assert attrs == {**locked, "shallow": False}
    assert '\\"shallow\\":false' in nix_fetch.fetch_expr(locked).replace(" ", "")
    forge = {"type": "github", "owner": "org", "repo": "app", "rev": "c" * 40}
    assert nix_fetch.fetch_attrs(forge) == forge
