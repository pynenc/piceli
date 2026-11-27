# Scanning images for known vulnerabilities (0.94.0)

Piceli scans every image it builds, mirrors or copies for known CVEs
(Common Vulnerabilities and Exposures) before the image is used. A
**critical** vulnerability that already has a fixed version stops the
image. Everything else is counted and recorded.

| When | What is scanned | Before |
| --- | --- | --- |
| A host build (`piceli artifacts build-spec run`, `piceli build job`, the GitOps controller) | Each image's OCI archive, one per platform | It is pushed or written to the receipt |
| A mirror (`mirror=[…]`) | The copy in the target registry, each platform | The mirror is recorded as ready |
| `piceli artifacts copy` | The source digest, each platform | Anything is copied |

The scanner is [Grype](https://github.com/anchore/grype) 0.122.1 (Apache
2.0). Piceli pins it by version and by the sha256 of each release tarball,
downloads it once into the cache (`<cache>/toolchains/grype/`) and refuses
a tarball that does not match (`image-scanner-checksum`).

## The rule

A finding fails the image when:

- its severity is **Critical**, and
- its fix state is `fixed`: the advisory names the versions that fix it.

A Critical finding with no fix yet, and every High, Medium or Low one, does
not fail the image. They are counted by severity in the receipt.

A failure is the error `image-vulnerable` (exit `1`). Its `details` list
each image (`image`, `digest`, `platform`) and each finding: `id`,
`severity`, `package`, `installed`, `fixed_in`, `waived` (and
`waiver_expired` when a waiver ran out). The build log has one line per
image:

```text
[piceli] scan web: 1 fixable critical (2 critical, 1 high) in 4.2s (db 2026-11-26T04:12:34Z)
```

## The vulnerability database

Grype keeps its database in `<cache>/grype-db` and updates it before each
scan. The scan needs HTTPS to the database server and to `github.com` (the
first time, to download Grype). A database that cannot be fetched, or is too
old, fails the scan (`image-scan-failed`): scanning fails closed, it never
skips. The receipt records when the database was built (`db_built`).

## Waivers and turning it off

Accept a known finding with a waiver: an id, a reason and a date. After that
date (inclusive) the waiver no longer applies, and the error says it
expired. A waiver may name a related id (a GHSA advisory and its CVE).

In a `host-build.toml`, add a `[scan]` table:

```toml
[scan]
enabled = true
ignore = [
  { id = "CVE-2026-0001", reason = "the parser is never called", until = "2027-01-31" },
]
```

The table is part of the spec, so a new waiver changes the plan hash.

For `piceli artifacts copy`, put the same table in a file of its own and
pass `--scan-policy FILE` (also part of the copy plan). For mirrors, name
that file in `PICELI_SCAN_POLICY` in the environment of `piceli deploy` (or
of the controller).

Turning scanning off is always explicit, and recorded:

- a build: `[scan] enabled = false`;
- a copy: `--no-scan`;
- a mirror: a policy file with `enabled = false`.

The receipt then says `"scan": "off"` (and a build's provenance too).

## Receipts

A build receipt gets `scans`, per image (`<image>/<os>-<arch>` for several
platforms):

```json
{"scanner": "grype", "version": "0.122.1", "pinned": true,
 "db_built": "2026-11-26T04:12:34Z", "fail_on": "critical-fixed",
 "critical_fixable": 0,
 "counts": {"critical": 1, "high": 3, "medium": 7, "low": 2, "negligible": 0, "unknown": 0},
 "waived": ["CVE-2026-0001"]}
```

Next to the SBOM (software bill of materials) the build writes
`attestations/<image>.scan.json`: the same summary, the waivers and the
Critical and High findings. The image's entry records its path and sha256
(`scan`), like the SBOM and the provenance. It holds no local path.

A mirror receipt and a copy receipt get `scan`: the same summary plus
`platforms` (each platform's digest, counts and `critical_fixable`).

## Other machines and tests

`PICELI_SCANNER=/path/to/grype` uses that executable instead of the pinned
one (`"pinned": false` in the receipt), for example a machine with no access
to `github.com` that has its own copy. `PICELI_SCAN_CACHE` moves the cache
mirrors and copies use (default `~/.cache/piceli/scan`; a host build uses
its own cache directory).

Registry credentials reach Grype only in its environment
(`GRYPE_REGISTRY_AUTH_*`); they are never printed, and its error output is
redacted of them.

## Errors

| Code | Meaning |
| --- | --- |
| `image-vulnerable` | An image has a Critical vulnerability with a fixed version, and no live waiver |
| `image-scan-failed` | The scan could not run: no database, a scanner failure, an unreadable report |
| `image-scanner-checksum` | The Grype download does not match its pinned sha256 |
| `scan-policy-invalid` | A `[scan]` table or policy file cannot be read |

`piceli explain <code>` prints the cause and the fix of each.
