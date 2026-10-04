"""``prepare.sh``: the app's generated Secrets, made in the client's cluster.

A bundle carries no secret value. Each Secret the app declares is bound to
outputs of the pipeline's generators (``Secrets(Random, Template, TlsCa…)``);
:func:`script` writes a POSIX ``sh`` script that generates those values in the
client's cluster with ``openssl`` and creates the Secrets with ``kubectl``:

- ``Random(n)``: ``n`` random bytes, URL-safe base64 without padding (what
  ``secrets.token_urlsafe(n)`` gives the release engine);
- ``Static``: the declared public value;
- ``Template``: the text with its placeholders filled from the other values;
- ``TlsCa`` / self-signed TLS: a private CA and its leaf certificates (or one
  self-signed certificate), with the same ``openssl`` settings as the release
  engine; the CA key never leaves the script's private directory;
- any other source (``Sops``, ``Vault``, ``AwsSecret``, an import): a file the
  client names with ``PICELI_INPUT_<NAME>=path`` (never read by Piceli).

The script is idempotent and all-or-nothing: when every Secret exists it does
nothing; when none does it creates them all; when only some do it refuses and
names them (never their values). Values live only in a private temporary
directory (``umask 077``) removed on exit, and are never printed. ``kubectl``
is ``$KUBECTL`` (default ``kubectl``), so a context can be named explicitly
(``KUBECTL="kubectl --context my-cluster"``).

Importing this module is side-effect free.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from piceli.k8s.release_secret_spec import (
    ImportSecretSpec,
    RandomSecretSpec,
    StaticSecretSpec,
    TemplateSecretSpec,
    TlsCaSpec,
    TlsSelfSignedSpec,
    outputs,
    template_references,
)

_TOKEN = re.compile(r"\{\{|\}\}|\{(?:secret:)?([a-z][a-z0-9_.-]{0,190})\}")


class PrepareError(ValueError):
    """The app's Secrets cannot be prepared by a script. ``code`` is registered."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SecretPlan:
    """One Secret ``prepare.sh`` creates: its data keys and the outputs they hold."""

    name: str
    type: str
    keys: tuple[tuple[str, str], ...]

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "keys": dict(self.keys),
        }


def _unpointer(pointer: str) -> str | None:
    if not pointer.startswith("/data/"):
        return None
    return pointer[len("/data/") :].replace("~1", "/").replace("~0", "~")


def secret_plans(components: Sequence[Mapping[str, Any]]) -> list[SecretPlan]:
    """The Secrets of a render (``piceli.app.render.rendered``) and their bindings.

    :raises PrepareError: ``bundle-secret-unsupported`` when a Secret key is
        not bound to a generator output (a literal value).
    """
    plans = []
    for component in components:
        for resource in component["resources"]:
            manifest = resource["manifest"]
            if (manifest.get("apiVersion"), manifest.get("kind")) != ("v1", "Secret"):
                continue
            name = str((manifest.get("metadata") or {}).get("name"))
            bound: dict[str, str] = {}
            for binding in resource.get("secret_bindings") or ():
                key = _unpointer(str(binding.get("pointer") or ""))
                if key is not None and binding.get("input"):
                    bound[key] = str(binding["input"])
            keys = sorted(
                {*(manifest.get("data") or {}), *(manifest.get("stringData") or {})}
            )
            unbound = [key for key in keys if key not in bound]
            if unbound:
                raise PrepareError(
                    "bundle-secret-unsupported",
                    f"Secret/{name} key(s) {unbound} hold no generator output; a "
                    "bundle carries no values: bind them to Secrets(...) outputs "
                    "(Static for public values)",
                )
            plans.append(
                SecretPlan(
                    name,
                    str(manifest.get("type") or "Opaque"),
                    tuple(sorted(bound.items())),
                )
            )
    return sorted(plans, key=lambda item: item.name)


def _owners(generators: Mapping[str, Any]) -> dict[str, str]:
    return {
        output.name: name
        for name, spec in generators.items()
        for output in outputs(name, spec)
    }


def required_generators(
    plans: Sequence[SecretPlan], generators: Mapping[str, Any]
) -> list[str]:
    """Generator names the Secrets need, dependencies (template inputs) first.

    :raises PrepareError: ``bundle-secret-unsupported`` for an output no
        generator declares, or a template cycle.
    """
    owners = _owners(generators)
    order: list[str] = []
    visiting: set[str] = set()

    def visit(name: str) -> None:
        if name in order:
            return
        if name in visiting:
            raise PrepareError(
                "bundle-secret-unsupported", f"template cycle at {name!r}"
            )
        visiting.add(name)
        spec = generators[name]
        if isinstance(spec, TemplateSecretSpec):
            for reference in template_references(spec.template):
                owner = owners.get(reference)
                if owner is None:
                    raise PrepareError(
                        "bundle-secret-unsupported",
                        f"template {name!r} names {reference!r}, which no generator declares",
                    )
                visit(owner)
        visiting.discard(name)
        order.append(name)

    for plan in plans:
        for _, output in plan.keys:
            owner = owners.get(output)
            if owner is None:
                raise PrepareError(
                    "bundle-secret-unsupported",
                    f"Secret/{plan.name} uses {output!r}, which no generator declares",
                )
            visit(owner)
    return order


def input_variable(name: str) -> str:
    """The environment variable naming the file of a client-provided value."""
    return "PICELI_INPUT_" + re.sub(r"[^A-Z0-9]", "_", name.upper())


def client_inputs(
    plans: Sequence[SecretPlan], generators: Mapping[str, Any]
) -> list[dict[str, str]]:
    """Values the client provides as files (sources Piceli cannot generate)."""
    return [
        {
            "generator": name,
            "type": str(generators[name].type),
            "variable": input_variable(name),
        }
        for name in required_generators(plans, generators)
        if not _generated(generators[name])
    ]


def _generated(spec: Any) -> bool:
    return isinstance(
        spec,
        RandomSecretSpec
        | StaticSecretSpec
        | TemplateSecretSpec
        | TlsCaSpec
        | TlsSelfSignedSpec,
    )


# ---------------------------------------------------------------- script


def quote(text: str) -> str:
    """``text`` as one single-quoted ``sh`` word (newlines kept)."""
    return "'" + text.replace("'", "'\\''") + "'"


class _Files:
    """Output name → ``"$work/v<N>"`` (names never appear as paths)."""

    def __init__(self) -> None:
        self.paths: dict[str, str] = {}

    def __getitem__(self, output: str) -> str:
        if output not in self.paths:
            self.paths[output] = f'"$work/v{len(self.paths)}"'
        return self.paths[output]


def _names(dns: Sequence[str], ips: Sequence[str]) -> str:
    return ",".join([*(f"DNS:{item}" for item in dns), *(f"IP:{item}" for item in ips)])


def _config(lines: Sequence[str], path: str) -> list[str]:
    return [f"printf '%s\\n' {' '.join(quote(line) for line in lines)} > {path}"]


def _generator_lines(name: str, spec: Any, files: _Files) -> list[str]:
    lines = [f"# {name} ({spec.type})"]
    if isinstance(spec, RandomSecretSpec):
        lines.append(
            f"openssl rand {spec.bytes} | openssl base64 -A | tr '+/' '-_' | "
            f"tr -d '=\\n' > {files[name]}"
        )
    elif isinstance(spec, StaticSecretSpec):
        lines.append(f"printf '%s' {quote(spec.value)} > {files[name]}")
    elif isinstance(spec, TemplateSecretSpec):
        parts: list[str] = []
        position = 0
        for match in _TOKEN.finditer(spec.template):
            literal = spec.template[position : match.start()]
            if literal:
                parts.append(f"printf '%s' {quote(literal)}")
            token = match.group(0)
            if token in {"{{", "}}"}:
                parts.append(f"printf '%s' {quote(token[0])}")
            else:
                parts.append(f"cat {files[match.group(1)]}")
            position = match.end()
        if position < len(spec.template):
            parts.append(f"printf '%s' {quote(spec.template[position:])}")
        lines.append("{ " + "; ".join(parts) + "; } > " + files[name])
    elif isinstance(spec, TlsSelfSignedSpec):
        config = f'"$work/{name}.cnf"'
        lines += _config(
            [
                "[req]", "distinguished_name = dn", "x509_extensions = v3", "prompt = no",
                "[dn]", f"CN = {spec.dns_names[0]}", "[v3]",
                "subjectAltName = " + _names(spec.dns_names, spec.ip_addresses),
                "keyUsage = critical,digitalSignature,keyEncipherment",
                "extendedKeyUsage = serverAuth,clientAuth",
            ],
            config,
        )  # fmt: skip
        lines.append(
            f"openssl req -x509 -newkey rsa:{spec.rsa_bits} -nodes -sha256 -days {spec.days} "
            f"-config {config} -keyout {files[f'{name}.key']} -out {files[f'{name}.crt']} "
            f">/dev/null 2>&1 || fail {quote(f'openssl failed for {name}')}"
        )
    elif isinstance(spec, TlsCaSpec):
        ca_config = f'"$work/{name}.ca.cnf"'
        lines += _config(
            [
                "[req]", "distinguished_name = dn", "x509_extensions = v3", "prompt = no",
                "[dn]", f"CN = {spec.common_name}", "[v3]",
                "basicConstraints = critical,CA:TRUE,pathlen:0",
                "keyUsage = critical,keyCertSign,cRLSign", "subjectKeyIdentifier = hash",
            ],
            ca_config,
        )  # fmt: skip
        ca_crt, ca_key = files[f"{name}.ca.crt"], files[f"{name}.ca.key"]
        lines.append(
            f"openssl req -x509 -newkey rsa:{spec.rsa_bits} -nodes -sha256 -days {spec.ca_days} "
            f"-config {ca_config} -keyout {ca_key} -out {ca_crt} "
            f">/dev/null 2>&1 || fail {quote(f'openssl failed for {name} CA')}"
        )
        for leaf in sorted(spec.leaves):
            settings = spec.leaves[leaf]
            common = (settings.dns_names or settings.ip_addresses)[0]
            config = f'"$work/{name}.{leaf}.cnf"'
            request = f'"$work/{name}.{leaf}.csr"'
            lines += _config(
                [
                    "[req]", "distinguished_name = dn", "prompt = no", "[dn]",
                    f"CN = {common}", "[v3]", "basicConstraints = critical,CA:FALSE",
                    "subjectAltName = " + _names(settings.dns_names, settings.ip_addresses),
                    "keyUsage = critical,digitalSignature,keyEncipherment",
                    "extendedKeyUsage = serverAuth,clientAuth",
                    "authorityKeyIdentifier = keyid",
                ],
                config,
            )  # fmt: skip
            crt, key = files[f"{name}.{leaf}.crt"], files[f"{name}.{leaf}.key"]
            failed = quote(f"openssl failed for {name} leaf {leaf}")
            lines.append(
                f"openssl req -new -newkey rsa:{spec.rsa_bits} -nodes -sha256 "
                f"-config {config} -keyout {key} -out {request} >/dev/null 2>&1 || fail {failed}"
            )
            lines.append(
                f"openssl x509 -req -in {request} -CA {ca_crt} -CAkey {ca_key} "
                f'-set_serial "0x$(openssl rand -hex 16)" -days {spec.days} -sha256 '
                f"-extfile {config} -extensions v3 -out {crt} >/dev/null 2>&1 || fail {failed}"
            )
    else:
        variable = input_variable(name)
        lines.append(
            f'[ -n "${{{variable}:-}}" ] && [ -r "${{{variable}}}" ] || '
            f"fail {quote(f'set {variable} to a readable file holding {name}')}"
        )
        if isinstance(spec, ImportSecretSpec) and spec.trim_newline:
            lines.append(f'printf \'%s\' "$(cat "${{{variable}}}")" > {files[name]}')
        else:
            lines.append(f'cat "${{{variable}}}" > {files[name]}')
    return lines


def script(
    app: str,
    plans: Sequence[SecretPlan],
    generators: Mapping[str, Any],
) -> str:
    """The text of ``prepare.sh`` for ``plans`` (see the module docstring)."""
    files = _Files()
    order = required_generators(plans, generators)
    needs_openssl = any(
        isinstance(generators[name], RandomSecretSpec | TlsCaSpec | TlsSelfSignedSpec)
        for name in order
    )
    body = [
        "#!/bin/sh",
        f"# prepare.sh: create the generated Secrets of {app} (written by piceli bundle).",
        '# Usage: ./prepare.sh NAMESPACE   (KUBECTL="kubectl --context NAME" to pick a context)',
        "# Idempotent: does nothing when every Secret exists. Never prints a value.",
        "# shellcheck disable=SC2086",
        "set -eu",
        "umask 077",
        "",
        "fail() { printf 'prepare.sh: %s\\n' \"$1\" >&2; exit 1; }",
        '[ "$#" -eq 1 ] || fail "usage: $0 NAMESPACE"',
        'NAMESPACE="$1"',
        'KUBECTL="${KUBECTL:-kubectl}"',
        'command -v "${KUBECTL%% *}" >/dev/null 2>&1 || fail "kubectl not found"',
    ]
    if needs_openssl:
        body.append('command -v openssl >/dev/null 2>&1 || fail "openssl not found"')
    body += [
        '$KUBECTL get namespace "$NAMESPACE" -o name >/dev/null 2>&1 || '
        'fail "namespace $NAMESPACE not found: create it first"',
    ]
    if not plans:
        body.append(
            'echo "prepare.sh: this app has no generated Secrets; nothing to do"'
        )
        return "\n".join(body) + "\n"
    body += [
        "",
        f"secrets={quote(' '.join(plan.name for plan in plans))}",
        "present=''",
        "missing=''",
        "for name in $secrets; do",
        '  if $KUBECTL -n "$NAMESPACE" get secret "$name" -o name >/dev/null 2>&1; then',
        '    present="$present $name"',
        "  else",
        '    missing="$missing $name"',
        "  fi",
        "done",
        'if [ -z "$missing" ]; then',
        '  echo "prepare.sh: every Secret exists in $NAMESPACE; nothing to do"',
        "  exit 0",
        "fi",
        'if [ -n "$present" ]; then',
        '  fail "only some Secrets exist (present:$present; missing:$missing); delete the present ones and run again"',
        "fi",
        "",
        'work="$(mktemp -d "${TMPDIR:-/tmp}/prepare.XXXXXX")"',
        "trap 'rm -rf \"$work\"' EXIT",
        "trap 'exit 1' HUP INT TERM",
        "",
    ]
    for name in order:
        body += _generator_lines(name, generators[name], files)
    body.append("")
    for plan in plans:
        sources = " ".join(
            f"--from-file={quote(key)}={files[output]}" for key, output in plan.keys
        )
        body.append(
            f'$KUBECTL -n "$NAMESPACE" create secret generic {quote(plan.name)} '
            f"--type={quote(plan.type)} {sources} >/dev/null"
        )
        body.append(
            f'$KUBECTL -n "$NAMESPACE" label secret {quote(plan.name)} '
            f"{quote('app.kubernetes.io/part-of=' + app)} --overwrite >/dev/null"
        )
        body.append(f"echo {quote(f'prepare.sh: created Secret {plan.name}')}")
    body.append(f'echo "prepare.sh: {len(plans)} Secret(s) ready in $NAMESPACE"')
    return "\n".join(body) + "\n"
