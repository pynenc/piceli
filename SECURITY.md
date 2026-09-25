# Security policy

Piceli changes Kubernetes clusters, registries and nodes and handles secret
material, so we take reports seriously.

## Supported versions

Piceli is pre-1.0. Security fixes go into the latest minor release only.

| Version | Supported |
| --- | --- |
| latest `0.x` release | yes |
| older releases | no, upgrade first |

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
