# Publish and sign release files

Since 0.32.0. Release downloads (archives, a checksum list, its signature)
can live in the registry your images already use, each file addressed by its
own sha256, and be signed with a key kept in Piceli's secret store. Anyone
verifies the signature with OpenSSH alone.

```sh
# 1. A signing key (once): stored owner-only, only the public key is printed.
piceli secrets signing-key release-signing --generate --comment release@example.org
piceli secrets signing-key release-signing | jq -r .allowed_signers > dist/allowed_signers

# 2. Sign the checksum list: writes dist/SHA256SUMS.sig.
(cd dist && sha256sum *.tar.gz > SHA256SUMS)
piceli sign-file dist/SHA256SUMS --key release-signing

# 3. Publish the files: without --approve it prints the plan (exit 3).
piceli artifacts publish-files dist/* \
    --to oci://ghcr.io/example/tool --tag 1.4.0-downloads \
    --artifact-type application/vnd.example.tool.downloads.v1 \
    --docker-config ~/.docker/config.json
piceli artifacts publish-files dist/* … --approve sha256:<plan digest> --out receipt.json
```

## What `publish-files` pushes

- **One layer per file, stored as it is** (no tar, no compression), with
  media type `application/octet-stream` and the annotation
  `org.opencontainers.image.title` set to the file's name. The layer's
  digest is the file's sha256: the value in `SHA256SUMS` is the address.
- **One OCI image manifest** with `artifactType` (your `--artifact-type`)
  and the empty config (`application/vnd.oci.empty.v1+json`). It holds no
  time, so the same files give the same manifest digest every time, and a
  second publish pushes nothing.
- **The tag last.** A tag that names another manifest is refused before
  anything is pushed (`publish-tag-exists`) unless `--move-tag` is part of
  the approved plan; the receipt then records the previous digest.

Without `--approve` the command prints the plan: every file's name, size and
digest, the target, the tag, the manifest digest and the plan's digest. It
contacts no registry. With `--approve` it pushes exactly that plan: each file
is hashed again just before it is sent, and one that changed is refused
(`publish-input-changed`). Only blobs the registry lacks are uploaded; then
the tag, the manifest and every blob are read back.

The receipt (`--out`, `piceli.publish-files-receipt.v1`) lists the manifest
digest and every file's name, size, digest and URL. Credentials
(`--docker-config` or `--credentials`, as for {doc}`publishing_images`) are
never printed or written to it.

## Fetching one file by its digest

A registry serves each file at
`https://<registry>/v2/<repository>/blobs/sha256:<hex>`. A public repository
needs no account, but most registries ask even anonymous clients for a
token first:

```sh
# Piceli: anonymous token as the registry asks; written only if the digest matches.
piceli artifacts fetch-file --from oci://ghcr.io/example/tool \
    --digest sha256:<hex> --out tool-linux-x86_64.tar.gz

# curl, GitHub Container Registry (a public package): any bearer token is
# accepted for public pulls, the answer redirects to the blob storage.
curl -fL -H "Authorization: Bearer QQ==" \
    -o tool-linux-x86_64.tar.gz \
    https://ghcr.io/v2/example/tool/blobs/sha256:<hex>

# curl, any registry with the token flow:
TOKEN=$(curl -fs "https://ghcr.io/token?scope=repository:example/tool:pull" | jq -r .token)
curl -fL -H "Authorization: Bearer $TOKEN" -o FILE https://ghcr.io/v2/example/tool/blobs/sha256:<hex>
```

Check the bytes against the digest you committed (`fetch-file` does). In
Nix, a fixed-output derivation fetches it the same way:

```nix
pkgs.fetchurl {
  url = "https://ghcr.io/v2/example/tool/blobs/sha256:<hex>";
  sha256 = "<hex>";
  curlOptsList = [ "-H" "Authorization: Bearer QQ==" ];
}
```

On GitHub, a package is private until you make it public in its settings.

## The signing key

`piceli secrets signing-key NAME` keeps an ssh-ed25519 key in the local
secret store (`$PICELI_CREDENTIALS_DIR`, a `0700` directory, the key in a
`0600` file), like every other stored key:

- `--generate` creates one; `--file KEY` imports an existing ed25519 private
  key without a passphrase (OpenSSH or PKCS#8 format). Either prints only
  the public key and its `allowed_signers` line (principal: `--comment`,
  default the name). Without them it only reads and prints the public key.
  Replacing a key needs `--replace`.
- `piceli secrets backup` includes it, and `piceli secrets delete NAME
  --kind signing-key` removes it.
- Declare it in a module so `piceli secrets status` and `setup` list it:

  ```python
  from piceli.artifacts.file_signing import SigningKey

  release_key = SigningKey("release-signing", public_key="ssh-ed25519 AAAA…")
  ```

  A stored key whose public key is not the declared one is reported
  `damaged`; `setup --yes` generates a missing one.

The key has no passphrase: the store's file permissions protect it, as they
do every other stored secret. To keep a key with a passphrase, leave it in
an SSH agent instead (`ssh-add KEY`) and sign with `--agent KEY.pub`: Piceli
sends the agent the data to sign and gets the signature back; the key never
leaves the agent.

## Signing and verifying

```sh
piceli sign-file FILE... --key NAME          # a stored key
piceli sign-file FILE... --agent KEY.pub     # a key in ssh-agent ($SSH_AUTH_SOCK or --agent-socket)
```

Each `FILE.sig` is the signature `ssh-keygen -Y sign -n file -f KEY FILE`
would write, byte for byte (Ed25519 signatures are deterministic).
`--namespace` changes the namespace (`file` by default). Verify with:

```sh
ssh-keygen -Y verify -f allowed_signers -I release@example.org -n file \
    -s SHA256SUMS.sig < SHA256SUMS
sha256sum -c SHA256SUMS
```

## Errors

| Code | Meaning |
| --- | --- |
| `publish-invalid` | Bad `--to`/`--tag`/`--artifact-type`, not a regular file, two files with the same name |
| `publish-not-approved` | `--approve` is not this plan's digest |
| `publish-tag-exists` | The tag names another manifest (a new tag, or `--move-tag`) |
| `publish-input-changed` | A file changed after the plan was printed |
| `publish-verification-failed` | What the registry serves differs from what was pushed |
| `blob-digest-mismatch`, `blob-not-found` | `fetch-file`: the bytes do not match the digest, or there is no such blob |
| `infra-credential-missing`, `infra-credential-unsafe`, `infra-credential-exists`, `infra-credential-invalid` | The stored key is absent, readable by others, would be replaced, or is not an ed25519 key without a passphrase |
| `sign-file-invalid`, `sign-key-invalid`, `signing-agent-failed` | `sign-file` could not read a file, the public key, or get a signature from the agent |

`piceli explain <code>` prints the cause and the fix of each.
