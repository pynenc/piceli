# Manage secrets: generate, template, import, fetch, rotate

This page shows how to give a release every secret value it needs (passwords,
connection strings, config files that embed a password, a private CA and its
certificates) without writing any of them yourself, how to take values from
an external source (a SOPS file, HashiCorp Vault, AWS Secrets Manager), and how
to import, inspect and rotate them.

**Maturity: preview.** The `[secrets.*]` generator types and the
`piceli release secret show` command may still change before 1.0. The origin
labels and error codes listed here are what scripts should match on.

## How it works

You declare **generators** in `release.toml`. When `plan` creates a release,
Piceli produces their values inside the private secret store (the state
directory, owner-only). Your composition function never sees a value: it gets
opaque references from `ctx.secret(name)` and binds them to manifest fields
with `ResourceIntent.with_secret(pointer, reference)`. The values are written
into the cluster only when an approved plan runs.

Values are produced **only after the plan is accepted**: if `plan` refuses
(for example because an object needs adoption), no value is generated, no
import source is read, `openssl` does not run and the store gets no new version.
External sources are the exception to "no source is read": they are read at
every plan to detect a changed value, but nothing is stored until the plan is
accepted (see {ref}`external-secret-sources`).

A new release **carries over** every value from the latest release made with
the same generator settings, so values are stable across releases until you
rotate them.

## Prerequisites

- A working `release.toml` and composition (see {doc}`release_cli`).
- For `tls-self-signed` and `tls-ca`: an `openssl` binary at an absolute path
  (default `/usr/bin/openssl`; LibreSSL works too).
- For external sources: the `sops` binary (`sops`), a reachable Vault and a
  token (`vault`), or `pip install "piceli[aws]"` and AWS credentials
  (`aws-secrets-manager`). See {ref}`external-secret-sources`.

## Steps

1. **Declare the generators.** This example gives a cache a password, a
   connection string, a config file and TLS certificates signed by one private
   CA:

   ```toml
   [secrets.cache_password]
   type = "random"
   bytes = 32

   [secrets.cache_url]
   type = "template"
   template = "rediss://:{cache_password}@cache:6380/0"

   [secrets.cache_conf]
   type = "template"
   template = """
   tls-port 6380
   port 0
   requirepass {cache_password}
   tls-cert-file /run/private/tls.crt
   tls-key-file /run/private/tls.key
   tls-ca-cert-file /run/private/ca.crt
   """

   [secrets.ca]
   type = "tls-ca"
   leaves = { cache = ["cache", "cache.shop.svc"], api = ["api"] }
   ```

2. **Bind the outputs in the composition.** Each generator has one or more
   outputs (the table below lists them). Bind them into a Secret; your
   containers mount it as files or read keys as environment variables, and
   init containers only need to copy files:

   ```python
   keys = {
       "url": "cache_url",
       "redis.conf": "cache_conf",
       "ca.crt": "ca.ca.crt",
       "tls.crt": "ca.cache.crt",
       "tls.key": "ca.cache.key",
   }
   secret = ResourceIntent.from_manifest(
       {
           "apiVersion": "v1",
           "kind": "Secret",
           "metadata": {"name": "cache-auth-1", "namespace": ctx.namespace},
           "data": {key: "<private>" for key in keys},
       }
   )
   for key, output in keys.items():
       secret = secret.with_secret(f"/data/{key}", ctx.secret(output))
   ```

   Every exposed output must be bound, except outputs a template uses
   (`cache_password` above reaches the cluster through `cache_url` and
   `cache_conf`, so binding it is optional).

3. **Plan and apply.**

   ```console
   $ piceli release plan --spec release.toml
   release shop-5c1e0f7a9d2b (create, apply): 2 create
      create Secret/cache-auth-1
      create Deployment/cache
     secret ca: generated
     secret cache_conf: rendered
     secret cache_password: generated
     secret cache_url: rendered
   plan hash: 9f3c…d41a (valid until 2026-09-24T17:24:59+00:00)
   $ piceli release apply --spec release.toml --approve 9f3c…d41a
   apply shop-5c1e0f7a9d2b: ready
   ```

   The next release shows `secret cache_password: carried:shop-5c1e0f7a9d2b`.

## Generators

| `type` | Produces | Outputs (`ctx.secret(...)`) |
| --- | --- | --- |
| `random` | `token_urlsafe(bytes)` (default 32 bytes) | `<name>` |
| `tls-self-signed` | an RSA key and a self-signed certificate | `<name>.crt`, `<name>.key` |
| `tls-ca` | a private CA and leaf certificates it signs | `<name>.ca.crt`, `<name>.<leaf>.crt`, `<name>.<leaf>.key` (the CA key, `<name>.ca.key`, is internal) |
| `template` | text with placeholders for other outputs | `<name>` |
| `import` | an existing value from a file, an environment variable or a live Secret | `<name>` |
| `static` | a public value (a user name, a port) | `<name>` |
| `sops` | one value of a SOPS-encrypted file, or the whole file | `<name>` |
| `vault` | one key of a HashiCorp Vault KV v2 secret | `<name>` |
| `aws-secrets-manager` | an AWS Secrets Manager secret, or one key of its JSON value | `<name>` |

Every generator takes `encoding = "base64"` (the default, for `Secret.data`)
or `"raw"` (for `stringData`; the value must be UTF-8 text).

### `tls-ca`

```toml
[secrets.ca]
type = "tls-ca"
common_name = "shop private CA"     # default "piceli private CA"
ca_days = 3650                      # CA validity (default 3650)
days = 365                          # leaf validity (default 365, at most ca_days)
rsa_bits = 2048                     # 2048, 3072 or 4096
openssl = "/usr/bin/openssl"        # absolute path; add openssl_sha256 to pin the binary
[secrets.ca.leaves]
cache = { dns_names = ["cache", "cache.shop.svc"], ip_addresses = ["10.0.0.7"] }
api = ["api"]                       # short for { dns_names = ["api"] }
```

Clients trust one file, `ca.ca.crt`. Leaves are server and client
certificates (`serverAuth`, `clientAuth`) with the DNS names and IPs as
subject alternative names. Adding a leaf later keeps the CA and the other
leaves and signs only the new one (origin `signed:<release>`). Changing the
CA settings, or `--rotate ca`, creates a new CA and new leaves. The CA key
never reaches a composition; the owner can read it with `secret show`.

Piceli runs `openssl req` and `openssl x509 -req` with an explicit argument
list (no shell) in a private temporary directory that is deleted afterwards.

### `template`

```toml
[secrets.cache_url]
type = "template"
template = "rediss://:{cache_password}@cache:6380/0"
```

A placeholder is `{output}` or `{secret:output}`, where `output` is any
exposed output from the table above: `{cache_password}`, `{ca.ca.crt}`,
`{web-tls.crt}`. `{{` and `}}` are literal braces; any other brace is an error.
Templates are rendered inside the private store in dependency order, and a
template may use another template. When a value a template uses changes (by
rotation, for example), the template is rendered again with the new value.
Use a template to build a CA bundle from several certificates:
`template = "{ca.ca.crt}{old-ca.ca.crt}"`.

### `import`

```toml
[secrets.cache_password]
type = "import"
secret = { name = "legacy-cache", key = "password" }   # a live Secret in the target namespace
# file = "secrets/cache-password.txt"                  # or a file, relative to the spec
# env = "CACHE_PASSWORD"                               # or an environment variable
# trim_newline = true                                  # file/env: drop one trailing newline
# rotate = "random"                                    # what --rotate does: "random" or "reimport"
# bytes = 32                                           # size of a random rotation
```

Use an import to keep a service with existing data working across its first
release: the value it already uses is imported and becomes the **first
version**. A live Secret is read through the release's own kubeconfig and
context, and only when a release is created. Later releases carry the value
over, so the source can be removed after the first release. Changing the
source (another file, variable or Secret) imports again.

`--rotate NAME` replaces an imported value: with `rotate = "random"` (the
default) Piceli generates a new random token; with `rotate = "reimport"` it
reads the source again (useful when another system rotates the value).

### `static`

```toml
[secrets.cache_user]
type = "static"
value = "shop"
```

A public value that templates and compositions can reference like the others.
It is stored like a secret but is not secret: it appears in `release.toml`.

(external-secret-sources)=
## External sources

**Maturity: preview.** `sops`, `vault` and `aws-secrets-manager` take a value
that another system owns. They follow the same opaque-reference model as the
generators: the composition gets a reference, the value goes only into the
private store and the cluster.

How they differ from `import`:

- **Read at every plan.** A source may have been rotated upstream, so `plan`
  (and `diff`) reads every external source before it names the release.
  `--rollback-to` does not read them: it re-applies the stored versions.
- **A new version only when the value changed.** Each value is reduced to a
  keyed digest: HMAC-SHA256 under a random key that Piceli creates in the
  state directory (`secret-sources.key`, owner-only). The digest is part of
  the release fingerprint and of the carry-over bookkeeping. An unchanged
  value re-plans the same release and stores nothing; a changed value names
  a new release that stores a new version (origin `fetched`) and carries the
  other values over. Without the key, the digest says nothing about the value.
  Deleting the key only means the next plan stores the values again.
- **A refused plan stores nothing.** Values stay in memory until the plan is
  accepted, as with every generator.
- **Never printed.** Values never reach the plan, the journal, receipts,
  logs or error messages; `sops`'s error output is discarded and Vault or AWS
  error bodies are never shown, only a category (`HTTP 403`,
  `AccessDeniedException`).

### `sops`

```toml
[secrets.db_password]
type = "sops"
file = "secrets/prod.enc.yaml"   # relative to the spec
key = "db.password"              # or ["db", "password.v2"] when a key has a dot
# format = "yaml"                # yaml, json, dotenv, ini; "binary" takes the whole file (no key)
# sops = "sops"                  # a name looked up in PATH, or an absolute path
# sops_sha256 = "sha256:…"       # pin the binary's content
# pass_env = ["SOPS_AGE_KEY_FILE"]   # variables sops may see besides PATH and HOME
# timeout_seconds = 30
```

Piceli runs `sops --decrypt --output-type json [--input-type FORMAT] FILE`
with an explicit argument list (no shell), no stdin, a timeout and an 8 MB
output limit, in the file's directory. The process gets only `PATH`, `HOME`,
`LANG=C` and the variables named in `pass_env`, so age, PGP and cloud KMS
keys work as they do for `sops` itself (`~/.config/sops/age/keys.txt`,
`SOPS_AGE_KEY_FILE`, `GNUPGHOME`, `AWS_PROFILE`…) while nothing else of the
environment leaks in. A key must name a scalar (a string, number or boolean).

### `vault`

```toml
[secrets.api_token]
type = "vault"
address = "https://vault.example:8200"
mount = "secret"                 # the KV v2 mount (default "secret")
path = "shop/api"
key = "token"
token_file = "/run/vault/token"  # or: token_env = "VAULT_TOKEN"
# namespace = "team-a"           # Vault Enterprise / HCP namespace
# version = 3                    # pin a KV version; default: the latest
# ca_file = "certs/vault-ca.pem" # a private CA; TLS is always verified
# timeout_seconds = 10
```

One `GET /v1/<mount>/data/<path>` over verified TLS. The token is read from
`token_file` (a Vault agent sink, for example) or from the environment
variable named by `token_env`, and is sent only in the `X-Vault-Token`
header: it is never in the spec, a command line or a message. There is no
option to skip TLS verification; `http://` is accepted only for a loopback
address (`vault server -dev`). Redirects are not followed and proxies are not
used.

### `aws-secrets-manager`

```toml
[secrets.cloud_key]
type = "aws-secrets-manager"
secret_id = "shop/cloud"         # a name or an ARN
region = "eu-west-1"
key = "key"                      # optional: one key of a JSON SecretString
# version_stage = "AWSPREVIOUS"  # or version_id = "…"; default AWSCURRENT
# profile = "deploy"             # a profile of the AWS config files
# endpoint_url = "https://vpce-….secretsmanager.eu-west-1.vpce.amazonaws.com"
# timeout_seconds = 10
```

Needs `pip install "piceli[aws]"` (botocore, imported only when such a secret
is read). Credentials come from botocore's standard chain: environment,
`profile`, SSO, web identity (EKS, GitHub OIDC), instance or task role. A
`SecretBinary` value is taken as bytes (use the default `base64` encoding).

AWS Secrets Manager is the cloud manager Piceli supports first: it is the
most widely used one, botocore is a single optional dependency whose
credential chain already covers CI (OIDC) and in-cluster roles, and its
`Stubber` lets Piceli test it without a network.

### In a pipeline

```python
from piceli.pipeline import AwsSecret, Secrets, Sops, Template, Vault

secrets = Secrets(
    db_password=Sops(
        "secrets/prod.enc.yaml", "db.password", pass_env=["SOPS_AGE_KEY_FILE"]
    ),
    db_url=Template("postgres://shop:{db_password}@db/shop"),
    api_token=Vault(
        "https://vault.example:8200", "shop/api", "token", token_file="/run/vault/token"
    ),
    cloud_key=AwsSecret("shop/cloud", region="eu-west-1", key="key"),
)
app.secret("external-1", {"db": secrets.ref("db_url")})
```

### When the value changes

A changed value is a new version in a new release. Secrets are retained
objects, so bind it into a Secret with a **new name**, as for `--rotate`:
bump the generation in `[values]` (`external-1` → `external-2`) and plan.
Under the same name, `apply` stops with `retained-content-precondition-failed`
and writes nothing. `--rotate` refuses an external source
(`secret-rotation-refused`): rotate it at the source and plan again.
`--rollback-to` re-applies the earlier release with its stored version.

## Inspect a value

```text
piceli release secret show NAME --spec release.toml [--key KEY] [--release NAME] [--reveal] [--json]
```

| Invocation | Prints |
| --- | --- |
| `secret show cache_password --json` | metadata only (see below); never a value |
| `secret show cache_password --reveal` | the value, exactly, on stdout |
| `secret show cache_password` on a terminal | asks you to type the secret name, then prints the value |
| `secret show cache_password` without a terminal | refuses with `secret-reveal-required` (exit 2) |
| `secret show ca --reveal --key ca.crt` | one output of a generator with several |
| `secret show ca --json --reveal` | metadata plus `values` (text, or `{"base64": …}` for binary data) |

Contract:

- Reads the local state directory only; never contacts the cluster and never
  writes. The value goes to stdout only; nothing is written to stderr, logs,
  the journal or the catalog.
- Defaults to the selected (last ready) release, otherwise the newest release;
  `--release NAME` picks another. Values are decoded (no base64).
- `--key` takes the output key within the generator: `crt`, `key`,
  `ca.crt`, `ca.key`, `<leaf>.crt`, `<leaf>.key`.
- Revealing is an owner action. Agents must not run it with `--reveal`.
- Metadata fields: `secret`, `release`, `type`, `encoding`, `keys`,
  `internal`, `origin`, `first_release`, `first_origin`, and `source`
  (imports and external sources), `rotate` (imports) or `depends_on`
  (templates).
- Exit codes: `0` shown, `2` rejected: `{"state": "rejected", "reason":
  "<code>", "message": …}` on stdout (the value is never part of it).

## Rotate a value

```console
$ piceli release plan --spec release.toml --rotate cache_password
  secret cache_password: rotated
  secret cache_url: rendered
```

Rotation always creates a new release. Secrets are retained objects: Piceli
never rewrites an existing Secret, so put the rotated value into a Secret with
a new name (for example bump `cache-auth-1` to `cache-auth-2` through
`[values]`) and point the workloads at it. See the rotation notes in
{doc}`release_cli`. Templates and static values are not rotated themselves:
rotate what they use, or edit the spec.

## Origins in the plan

`plan` prints one line per generator, and the JSON output has the same values
under `secrets`:

| Origin | Meaning |
| --- | --- |
| `generated` | new value (first release, or the generator's settings changed) |
| `imported` | read from the import source; this is the first version |
| `rotated` | new value because of `--rotate` |
| `carried:<release>` | the same value as in that release |
| `signed:<release>` | `tls-ca`: CA and unchanged leaves from that release, new leaves signed |
| `rendered` | a template, rendered from its current inputs |
| `fetched` | an external source: the first value, or a value that changed at the source |
| `static` | the value from the spec |

## If it fails

Refusals exit with code `2` and print `{"state": "rejected", "reason":
"<code>", "message": …}` on stdout (0.4.0 changed this from
`{"state": "refused", …}`; see {ref}`release-contract-changes`). No secret
version is left behind by a refused plan.

| Code | Cause | Fix |
| --- | --- | --- |
| `secret-template-invalid` | A template has a stray `{` or `}` or a malformed placeholder. | Write `{{`/`}}` for literal braces; placeholders are `{output}`. |
| `secret-unknown-reference` | A placeholder names no exposed output (a typo, a multi-output generator without a key, or the internal CA key). | Use a name from the outputs table, for example `{ca.ca.crt}`; the message lists the valid keys. |
| `secret-dependency-cycle` | Templates reference each other in a loop (the message shows the loop). | Break the loop. |
| `secret-import-unavailable` | The import source is missing, empty or unreadable: file, variable, Secret or key, or the cluster refused the read (the category, such as `rbac-denied`, is shown). The value is never shown. | Provide the source, fix the name or key, or grant `get` on that Secret. Retrying after the fix is safe. |
| `secret-not-text` | A `raw` value or a template input is not UTF-8. | Use `encoding = "base64"`, or do not template binary data. |
| `secret-rotation-refused` | `--rotate` named a `template`, `static` or external-source generator. | Rotate the values the template uses, edit the static value, or rotate an external value at its source. |
| `secret-source-auth-failed` | Vault answered 401/403 or the token file/variable is missing; AWS denied the request or found no credentials; `sops` could not decrypt the data key (exit 128). | Provide the token, AWS credentials or profile, or the key `sops` needs (`pass_env`). |
| `secret-source-not-found` | The SOPS file, Vault path, AWS secret or the key inside it does not exist, is empty or is not a scalar. | Fix `file`, `path`, `secret_id` or `key`. |
| `secret-source-tool-missing` | `sops` is not found or not executable, or `piceli[aws]` is not installed. | Install `sops` (or set `sops` to its absolute path), or `pip install "piceli[aws]"`. |
| `secret-source-timeout` | `sops`, Vault or AWS did not answer within `timeout_seconds`. | Check connectivity or raise `timeout_seconds`; retrying is safe. |
| `secret-source-failed` | A network or TLS verification error (`tls-verify-failed`: set `ca_file`), an unexpected HTTP status or AWS error, `sops` failed or does not match `sops_sha256`, or a malformed response. | Read the category in the message and fix the source; retrying is safe. |
| `secret-generator-failed` | `openssl` failed, timed out or did not match `openssl_sha256`. | Check the `openssl` path and pin; the message shows the end of openssl's error output. |
| `secret-reveal-required` | `secret show` was asked for a value without `--reveal` and without a terminal to confirm on. | Add `--reveal` (owner only), or use `--json` for metadata. |
| `secret-key-required` | `secret show` without `--json` for a generator with several outputs. | Add `--key`, for example `--key ca.crt`. |
| `secret-not-found` | `secret show` named an unknown secret, key or release, or no release exists yet. | Check the name; the message lists what exists. |

A composition that does not bind a declared output is refused with
`declared secret inputs are not bound by the composition: […]`.
