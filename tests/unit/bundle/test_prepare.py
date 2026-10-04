"""``prepare.sh``: generated in POSIX sh, run against a stub kubectl, never printing values."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from piceli import Random, Secrets, Sops
from piceli.bundle.prepare import (
    PrepareError,
    SecretPlan,
    client_inputs,
    input_variable,
    quote,
    script,
    secret_plans,
)
from tests.unit.bundle.fixtures import app, components

# A kubectl stand-in: Secrets are directories under $FAKE_OUT; `create` copies
# every --from-file into one; it records each call in $FAKE_OUT/calls.
STUB = r"""#!/bin/sh
out="$FAKE_OUT"
printf '%s\n' "$*" >> "$out/calls"
case "$*" in
  "get namespace"*) [ -z "${FAKE_NO_NAMESPACE:-}" ] ;;
  *"get secret"*) n=$(printf '%s' "$*" | sed 's/.*get secret \([^ ]*\).*/\1/'); [ -d "$out/secrets/$n" ] ;;
  *"create secret generic"*)
    n=$(printf '%s' "$*" | sed 's/.*generic \([^ ]*\).*/\1/')
    mkdir -p "$out/secrets/$n"
    for a; do case "$a" in --from-file=*) kv=${a#--from-file=}; cp "${kv#*=}" "$out/secrets/$n/${kv%%=*}";; esac; done ;;
  *) exit 0 ;;
esac
"""


def _plans() -> tuple[list[SecretPlan], dict]:
    shop, generators = app()
    return secret_plans(components(shop, generators)), generators.generators


def _run(
    tmp_path: Path, text: str, *args: str, **env: str
) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "prepare.sh"
    path.write_text(text)
    stub = tmp_path / "kubectl"
    stub.write_text(STUB)
    stub.chmod(0o755)
    (tmp_path / "out").mkdir(exist_ok=True)
    (tmp_path / "tmp").mkdir(exist_ok=True)
    return subprocess.run(
        ["sh", str(path), *args],
        capture_output=True,
        text=True,
        timeout=120,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "KUBECTL": str(stub),
            "FAKE_OUT": str(tmp_path / "out"),
            "TMPDIR": str(tmp_path / "tmp"),
            **env,
        },
        check=False,
    )


def test_secret_plans_bind_each_key_to_an_output() -> None:
    plans, _ = _plans()
    assert [plan.public() for plan in plans] == [
        {
            "name": "shop-private",
            "type": "Opaque",
            "keys": {
                "ca.crt": "tls.ca.crt",
                "tls.crt": "tls.cache.crt",
                "tls.key": "tls.cache.key",
                "token": "token",
                "url": "url",
            },
        }
    ]


def test_an_unbound_secret_key_is_refused() -> None:
    components = [
        {
            "name": "x",
            "resources": [
                {
                    "manifest": {
                        "apiVersion": "v1",
                        "kind": "Secret",
                        "metadata": {"name": "x"},
                        "data": {"k": "<private>"},
                    },
                    "secret_bindings": [],
                }
            ],
        }
    ]
    with pytest.raises(PrepareError) as caught:
        secret_plans(components)
    assert caught.value.code == "bundle-secret-unsupported"


def test_quote_survives_any_text() -> None:
    text = 'it\'s {a} $HOME `x` "y"\nz'
    done = subprocess.run(
        ["sh", "-c", f"printf '%s' {quote(text)}"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout == text


@pytest.mark.skipif(
    shutil.which("shellcheck") is None, reason="shellcheck not installed"
)
def test_the_script_passes_shellcheck(tmp_path: Path) -> None:
    plans, generators = _plans()
    path = tmp_path / "prepare.sh"
    path.write_text(script("shop", plans, generators))
    done = subprocess.run(
        ["shellcheck", "-s", "sh", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not installed")
def test_prepare_creates_every_secret_once_and_prints_no_value(tmp_path: Path) -> None:
    plans, generators = _plans()
    text = script("shop", plans, generators)
    done = _run(tmp_path, text, "shop")
    assert done.returncode == 0, done.stderr
    made = tmp_path / "out" / "secrets" / "shop-private"
    assert sorted(item.name for item in made.iterdir()) == [
        "ca.crt",
        "tls.crt",
        "tls.key",
        "token",
        "url",
    ]
    token = (made / "token").read_text()
    assert (
        len(token) == 43 and "=" not in token and "\n" not in token
    )  # token_urlsafe(32)
    assert (made / "url").read_text() == f"redis://shop:{token}@cache:6379/0"
    # The leaf is signed by the CA and names its DNS name.
    verify = subprocess.run(
        ["openssl", "verify", "-CAfile", str(made / "ca.crt"), str(made / "tls.crt")],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert verify.returncode == 0, verify.stdout + verify.stderr
    text_cert = subprocess.run(
        ["openssl", "x509", "-in", str(made / "tls.crt"), "-noout", "-text"],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    assert "DNS:cache" in text_cert
    # No value reaches stdout, stderr or kubectl's arguments.
    calls = (tmp_path / "out" / "calls").read_text()
    for value in (token, (made / "tls.key").read_text().splitlines()[1]):
        assert value not in done.stdout + done.stderr + calls
    assert "app.kubernetes.io/part-of=shop" in calls
    # Nothing is left in TMPDIR.
    assert list((tmp_path / "tmp").iterdir()) == []
    # Idempotent: a second run changes nothing.
    again = _run(tmp_path, text, "shop")
    assert again.returncode == 0 and "nothing to do" in again.stdout
    assert (made / "token").read_text() == token


def test_prepare_refuses_a_partial_state_and_names_only_secrets(tmp_path: Path) -> None:
    generators = Secrets(a=Random(16), b=Random(16))
    plans = [
        SecretPlan("one", "Opaque", (("a", "a"),)),
        SecretPlan("two", "Opaque", (("b", "b"),)),
    ]
    (tmp_path / "out" / "secrets" / "one").mkdir(parents=True)
    done = _run(tmp_path, script("shop", plans, generators.generators), "shop")
    assert done.returncode == 1
    assert "present: one" in done.stderr and "missing: two" in done.stderr
    assert not (tmp_path / "out" / "secrets" / "two").exists()


def test_prepare_needs_the_namespace_and_one_argument(tmp_path: Path) -> None:
    plans, generators = _plans()
    text = script("shop", plans, generators)
    assert _run(tmp_path, text).returncode == 1
    missing = _run(tmp_path, text, "shop", FAKE_NO_NAMESPACE="1")
    assert missing.returncode == 1 and "create it first" in missing.stderr


def test_external_sources_are_files_the_client_names(tmp_path: Path) -> None:
    generators = Secrets(db=Sops("secrets.enc.yaml", "db.password")).generators
    plans = [SecretPlan("db", "Opaque", (("password", "db"),))]
    assert client_inputs(plans, generators) == [
        {"generator": "db", "type": "sops", "variable": "PICELI_INPUT_DB"}
    ]
    assert input_variable("api-key.v2") == "PICELI_INPUT_API_KEY_V2"
    text = script("shop", plans, generators)
    assert _run(tmp_path, text, "shop").returncode == 1
    value = tmp_path / "db.txt"
    value.write_text("from-the-client")
    done = _run(tmp_path, text, "shop", PICELI_INPUT_DB=str(value))
    assert done.returncode == 0, done.stderr
    assert (
        tmp_path / "out" / "secrets" / "db" / "password"
    ).read_text() == "from-the-client"


def test_an_app_without_secrets_gets_a_script_that_does_nothing(tmp_path: Path) -> None:
    done = _run(tmp_path, script("shop", [], {}), "shop")
    assert done.returncode == 0 and "no generated Secrets" in done.stdout


def test_template_outputs_come_after_their_inputs() -> None:
    plans, generators = _plans()
    text = script("shop", plans, generators)
    assert text.index("# token (random)") < text.index("# url (template)")
    assert text.index("# user (static)") < text.index("# url (template)")
