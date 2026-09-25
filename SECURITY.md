# Security policy

Piceli changes Kubernetes clusters, registries and nodes and handles secret
material, so we take reports seriously.

## Supported versions

Piceli is pre-1.0. Security fixes go into the latest minor release only.

| Version | Supported |
| --- | --- |
| latest `0.x` release | yes |
| older releases | no, upgrade first |

## Release provenance

Releases are built and uploaded by the `Release` GitHub Actions workflow
(`.github/workflows/release.yml`) from `main`, with PyPI trusted publishing:
no long-lived upload token exists. From 0.5.1 on, every wheel and sdist on
PyPI carries a [PEP 740](https://peps.python.org/pep-0740/) attestation signed
with Sigstore for that workflow's identity; PyPI shows it on each file's page
("Provenance"). To check a file yourself:

```sh
pip install pypi-attestations
pypi-attestations verify pypi --repository https://github.com/pynenc/piceli \
  pypi:piceli-0.5.1-py3-none-any.whl
```

Earlier releases (0.5.0 and before) have no attestations. The `v<version>` git
tag is pushed only after PyPI lists every file of the version, and is never
moved.

## Reporting a vulnerability

Report privately through
[GitHub security advisories](https://github.com/pynenc/piceli/security/advisories/new).
Do not open a public issue, and do not include real kubeconfigs, tokens or
secret values: a minimal reproduction with generic names is enough.

Include the Piceli version, the command, its JSON output and exit code, and
what an attacker could do. You will get an acknowledgement within a week.
Fixes are released as a patch version with a changelog entry; reporters are
credited unless they ask otherwise.

## What counts

Examples of in-scope problems:

- a secret value printed, logged, journaled or stored outside the secret store;
- a command that reads `~/.kube/config`, `KUBECONFIG` or the current context
  without being given an explicit kubeconfig and context;
- a change to a cluster, registry or node without an approved plan hash;
- an exec credential plugin that runs without `allow_exec`, or despite an
  `exec_sha256` mismatch;
- path traversal or code execution from a spec, receipt or archive that
  Piceli reads.
