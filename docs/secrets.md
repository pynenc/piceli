# Manage secrets: generate, template, import, fetch, rotate

This page shows how to give a release every secret value it needs (passwords,
connection strings, config files that embed a password, a private CA and its
certificates) without writing any of them yourself, how to take values from
an external source (a SOPS file, HashiCorp Vault, AWS Secrets Manager), and how
to import, inspect and rotate them. The local secrets a machine or a Nix host
needs (provider credentials, a cache's signing key, an age identity) are set
up with `piceli secrets status` and `setup`: see {ref}`host-secrets-setup`.

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

Workloads that read a Secret roll when its content changes, also when the
Secret keeps its name (for example one you deleted so the release creates it
again with the rotated value): their pod template carries
`piceli.io/secrets-digest`, a digest of keyed fingerprints of the values, so
the pods restart with the new value and a carried-over value rolls nothing.
See {ref}`roll-on-change`.

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
| `adopted` | read back from a live Secret an earlier release wrote (see below) |

## When the state starts empty

Generated values live in the state directory (`state_dir`, or the GitOps
controller's state volume). A new state directory knows none of them: a
clone on another machine, a lost volume, a controller rescheduled to another
node. Before 0.23.0 its first plan generated new values, and apply then
failed with `retained-content-precondition-failed` on every generated Secret
the earlier release had created, because Piceli never rewrites a retained
Secret. Deleting those Secrets by hand rotated every password, token and the
private CA.

Now the plan **adopts** them. When no release in the state records a
`random`, `tls-self-signed` or `tls-ca` generator, and a Secret the
composition declares is live with that generator's values, `plan` reads the
values from the Secret and records them in the state exactly as if it had
generated them. Nothing is rotated, deleted or written: the Secret plans as
`no-op`.

```console
$ piceli release plan --spec release.toml
  ...
  secret password: adopted from Secret credential (kept, not rotated)
  secret ca: adopted from Secret turn-auth (kept, not rotated)
```

The JSON output shows `"secrets": {"password": "adopted", ...}` and
`"secret_adoptions": [{"secret": "password", "from": ["credential"]}]`. The
plan itself records each adopted generator with the Secret's UID
(`secret_adoptions` in the plan), so the plan hash you approve covers the
adoption. `piceli deploy` prints `[plan] secret password: adopted from Secret
credential (no record in the state; values kept, not rotated)`, puts the same
list in its plan stage (and in the preview before images exist), and its
combined hash covers it. The release engine also logs one warning per
adopted generator. `piceli release secret show NAME --json` reports
`origin: adopted`; later releases carry the value as usual
(`carried:<release>`).

Only a Secret this release wrote is adopted. It must carry:

- the `piceli.io/owner` annotation equal to the release owner (`[release]
  owner`, or the pipeline's `owner`, by default the app name); an
  `inherited_owners` id does not count;
- a `managedFields` entry of the release's field manager (`[release]
  field_manager`, by default the owner);
- the target namespace (discovery reads only that namespace).

A Secret without these marks (unlabelled, or written by another app,
environment or tool) is never adopted: the plan and apply refuse it exactly
as before (`resource-requires-adoption` for another owner,
`retained-content-precondition-failed` for different content).

The values must also be values the generator could make. The plan refuses
with `secret-adoption-invalid` when the live Secret has another key set than
the composition declares, a `random` value has another length or characters
than `token_urlsafe(bytes)` gives, a certificate or key is not PEM, or two
Secrets hold different values for the same output. It refuses with
`secret-adoption-incomplete` when only some of a generator's values are live.
In both cases nothing is stored or written.

Adoption never replaces an explicit `--rotate`, and never applies when the
state already records the generator (its values then come from the state, or
new values are made when its settings changed). Imports, templates, static
values and external sources are not adopted: imports read their source
again, templates render from their (adopted) inputs.

**The CA key of a `tls-ca`.** The CA key is internal: Piceli never writes it
to a Secret, so an adopted CA has no key. The CA certificate and every leaf
are kept, and later releases carry them. Adding a leaf needs the key, so it
is refused with `secret-adoption-incomplete`: rotate the CA (`--rotate ca`)
into Secrets with new names when you need new leaves.

(host-secrets-setup)=
## Setting up a host's secrets

A declaration of machines and Nix hosts ({doc}`single_nixos_host`) needs
local secrets that never go into Git: the cloud's and the registrar's
credentials, the state passphrase, the cache's signing key, the host's age
identity, its first-boot user data and the cache's S3 key pairs. Two
commands set them up from the declaration itself (0.25.0). Neither prints a
value.

1. **See what the declaration needs.**

   ```console
   $ piceli secrets status web_1.py
   NAME             KIND             STATE       BY           NEXT
   infomaniak       provider-token   incomplete  owner        piceli secrets provider infomaniak --prompt
   porkbun          provider-token   missing     owner        piceli secrets provider porkbun --prompt
   example-cache-1  nix-signing-key  missing     piceli       piceli secrets nix-cache example-cache-1 --generate
   web-1-state      state-key        present     piceli       -
   web-1-age        age-identity     missing     piceli       piceli secrets age web-1-age --generate
   web-1-boot       user-data        missing     owner        piceli secrets user-data web-1-boot --prompt
   nix-cache-write  s3-keys          pending     infra-apply  piceli infra plan web_1.py:infra
   nix-cache-read   s3-keys          pending     infra-apply  piceli infra plan web_1.py:infra

   what each is for:
     infomaniak       openstack API [infomaniak: one part of two stored, re-run `piceli secrets provider infomaniak --prompt`]
     web-1-age        sops-nix on web-1; on host web-1 too: piceli host secrets web-1 puts it there; its age1 public key goes in .sops.yaml
     web-1-boot       user data of web-1; read once, when infra apply creates web-1: store a short-lived key right before that apply
     nix-cache-read   read S3 keys of bucket example-nix-cache; on host web-1 too: piceli host secrets web-1 writes it to /var/lib/piceli-host/cache.env [created by the next infra apply]
     ...
   backup: no backup yet; next: piceli secrets backup-key --out PATH --add
   warning: S3 key nix-cache-read (bucket example-nix-cache) is not read-only at openstack: hosts only read with it, but the cloud lets it write
   summary: 1 present, 2 pending, 4 missing, 1 incomplete; piceli secrets setup web_1.py walks through them
   ```

   It reads every declaration in the module (`Infrastructure`, `Bucket`,
   `NixCache`, `NixHost`, `NixRelease`, `DevBuilds`, and a composition's
   clusters, whose credentials are `piceli login` profiles), or one with
   `web_1.py:host`. Each secret is:

   | State | Meaning |
   | --- | --- |
   | `present` | Stored and complete. |
   | `pending` | A key pair the next `infra apply` creates: the infrastructure has not applied it yet. Not an error. |
   | `missing` | Not stored. A pair `infra apply` already created but this store lacks: the next `infra plan` stores it again from the state. |
   | `incomplete` | A credential that lacks a part (a Porkbun key pair stored as one value). |
   | `damaged` | Unreadable, readable by other users, of another kind, or a signing key whose public key is not `NixCache(public_key=)`. |
   | `not-needed` | The user data of a server that exists: a plan reads it only to create the server again. |

   `BY` says who makes it: `owner` (only you have it: you type it with
   `--prompt`), `piceli` (generated with `--generate`) or `infra-apply`
   (`piceli infra apply` creates and stores it). `NEXT` is the exact
   command. The backup line tells whether a backup exists and is newer than
   every secret (`stale` names the newer ones). Exit 0 means every secret
   is stored and complete, or pending; 1 that something is missing,
   incomplete or damaged. `--json` prints the same as one `piceli.secrets-status.v1`
   object (`secrets`, each with `state`, `created_by`, `next`, `purpose`,
   `where`, `build_host`, `detail`; `counts`, `complete`, `backup`,
   `warnings`).

2. **Walk through what is missing.** `piceli secrets setup web_1.py` takes
   the secrets in this order:

   1. Provider credentials, asked for part by part, as `piceli secrets
      provider` asks for them (an identifier echoed, a secret not).
   2. The keys Piceli generates, after a `[Y/n]` each: the state
      passphrase, the cache's signing key and the host's age identity. Only
      the public halves are printed; copy the signing key's into
      `NixCache(public_key=...)` and the `age1...` key into `.sops.yaml`.
   3. The user data, offered with a `[y/N]`: a bootstrap key in it should
      be short-lived (single-use, expiring in an hour), so the default is to
      store it later, right before `infra apply`.
   4. The S3 key pairs: `infra apply` creates them; nothing happens here.
   5. A backup when none is current: the offline key (`backup-key`), your
      own age recipient, then the backup itself. Each is skipped with an
      empty answer.

   ```console
   $ piceli secrets setup web_1.py
   1. Provider credentials: only you have them
     infomaniak: OpenStack application credential (openstack API); application credential ID and application credential secret
   Step 1 of 2: Application credential ID. Where: the cloud's dashboard (Horizon): …
   Application credential ID: 0123456789abcdef0123456789abcdef
   Step 2 of 2: Application credential secret. Where: …
   Application credential secret (not echoed):
     stored infomaniak: application credential ID and application credential secret; no value was printed
     ...
   2. Keys Piceli generates
     Generate nix-signing-key example-cache-1 now? [Y/n]: y
     generated example-cache-1 (nix-signing-key); the secret was not printed
     public key: example-cache-1:SRm8gJ5/1Ar3IoTsZn4jGAGWab2eao9d4zpNY1n6rec=
     put it in NixCache(public_key=...) in your declaration
     present: web-1-state (state-key)
     Generate age-identity web-1-age now? [Y/n]: y
     ...
   3. User data: needed only when infra apply creates the server
     Store web-1-boot now? [y/N]:
     later, right before infra apply: piceli secrets user-data web-1-boot --prompt
   4. S3 key pairs
     nix-cache-write: write S3 keys of bucket example-nix-cache, made by piceli infra apply; nothing to do here; next: piceli infra plan web_1.py:infra
     ...
   5. Backup
     Offline backup key: a path outside the store (a USB drive), empty to skip: /Volumes/usb/piceli-offline.key
     ...
   copy: example-cache-1:SRm8gJ5/1Ar3IoTsZn4jGAGWab2eao9d4zpNY1n6rec= into NixCache(public_key=...) (example-cache-1)
   copy: age1tyz57d7pampk4pt8ry6vcxgdalz4tpj48jnux3y92u44f09rtfusahd42n into .sops.yaml (web-1-age)
   done: every secret Piceli can set up now is stored
   ```

   A stored key is never replaced without a typed `y`: a damaged one is
   offered for replacement with `[y/N]`, and a signing key that only
   differs from the declaration prints its public key instead. Exit 0
   means everything is stored but what is left for later on purpose (the
   user data, the pairs `infra apply` makes); 1 that something still needs
   you.

3. **Copy the public keys** into the declaration and `.sops.yaml`, and run
   `piceli secrets status web_1.py` again: the signing key is `damaged`
   until `NixCache(public_key=)` names it.

4. **Right before the first apply**, store the user data (`piceli secrets
   user-data web-1-boot --prompt`), then plan and apply: `infra apply`
   stores the S3 pairs, and `status` shows them `present`.

5. **A build host that publishes releases.** With a `NixRelease(...,
   builder="mini")`, the cache's signing key and S3 write pair are made
   here (`setup`, `infra apply`), but `piceli builder serve --publish` on
   `mini` reads them from `mini`'s own store. Once they are present here,
   `status` gives the command that carries them there (`build_host:
   "mini"`), and the steps are in {ref}`build-host-secrets`.

Without a terminal (scripts, agents), `setup --yes` generates the keys and
reads provider parts piped one per line, in the order `status` lists them;
stdin at its end skips them, and the user data and the backup are left for
later. `--yes` never replaces a key. `--json` prints one object per step
(`state`: `stored`, `generated`, `copy`, `present`, `later`, `skipped`,
`needs-yes`, `kept`, `failed`), then a summary with `remaining`, `later` and
`copy`.

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
| `secret-adoption-invalid` | The state has no record of a generator, and the live Secret an earlier release wrote holds values it could not have made: another key set, a malformed value, or copies that differ. | Fix or delete the live Secret, or rotate the generator into a Secret with a new name. |
| `secret-adoption-incomplete` | Only some of a generator's values are live (an adopted CA has no key, so it cannot sign a new leaf). | Rotate the generator (`--rotate NAME`) into Secrets with new names. |

A composition that does not bind a declared output is refused with
`declared secret inputs are not bound by the composition: […]`.
