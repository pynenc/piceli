# Cluster init

```{admonition} Maturity: preview
:class: note

New in 0.14.0. Its parameters may still change in a minor release, with a changelog entry. See the {doc}`roadmap` for every feature's status.
```

A cluster is declared once, in Python, next to the environments it runs:
its API, the credential profile that reaches it, its nodes and their roles,
the in-cluster registry, where the GitOps controller and the UI run.
`piceli cluster init` sets it up in one approved plan, and `piceli cluster
status` shows what is there.

```python
# infra.py
from piceli import Registry
from piceli.infra import Cluster, Controller, Node, Ui

my_cluster = Cluster(
    "my-cluster",
    api="https://10.0.0.10:6443",  # checked against the profile
    credentials="my-cluster",  # a `piceli login` profile, never in Git
    nodes=[
        Node("node-1", arch="amd64", roles=["builder", "controller", "registry"]),
        Node("node-2", arch="arm64", roles=["workloads"]),
        Node("node-3", arch="arm64", roles=["workloads", "branch-envs"]),
    ],
    storage_class="local-path",
    registry=Registry.in_cluster(on="node-1", storage="50Gi"),
    controller=Controller(
        on="node-1", poll="1m", image="registry.example.com/piceli@sha256:…"
    ),
    ui=Ui(access="forward"),  # piceli access ui
)
```

```console
$ piceli login my-cluster --kubeconfig ~/clusters/my-cluster.yaml --context admin
$ piceli cluster init infra.py:my_cluster                 # plan, exit 3, prints the hash
$ piceli cluster init infra.py:my_cluster --approve sha256:…
$ piceli secrets git --cluster infra.py:my_cluster --prompt   # type the token
$ piceli cluster status infra.py:my_cluster
```

## The declaration

| Field | Meaning |
| --- | --- |
| `Cluster(name, api=, credentials=)` | `credentials` names a credential profile (`piceli login NAME …`). Before anything is read, the profile's kubeconfig context must point at `api` (scheme, host and port), otherwise `cluster-api-mismatch`. `--profile NAME` uses another profile for one command. Inside a pod the profile is the pod's service account and `api` is not compared. |
| `Node(name, arch=, roles=[…])` | `name` is the node's `kubernetes.io/hostname`; `arch` must match the node (`cluster-node-arch-mismatch`). Each role becomes the label `piceli.io/role-<role>=true`; `builder` also sets `piceli.io/builder=true`, which cluster builds select by default. A declared node missing from the cluster is refused (`cluster-node-missing`). |
| `registry=Registry.in_cluster(on=NODE, …)` | The {doc}`cluster_registry`, with its node mirrors (containerd or k3s, per node). |
| `controller=Controller(on=NODE, poll="1m")` | Where the GitOps controller runs. Init installs its foundation; `piceli gitops enable` adds its configuration and the Deployment, pinned to `on`. |
| `ui=Ui(access="forward")` | The UI, installed without OIDC or any exposed Service; reach it with `piceli access ui` (see {doc}`ui`). It runs a Piceli image pinned by digest: `Ui(image=…)`, else `Controller(image=…)` (`ui-install-image-unpinned` otherwise), on `Ui(on=)`, else the controller's node. |
| `storage_class=` | The StorageClass of the controller's state claim. |

Lists may be written as lists; they are stored as tuples. Every value is
checked when the module is imported (`cluster-invalid`).

## What init does

One plan with one hash covers:

1. **Node labels**: the role labels above and, when the registry picks its
   node agent per node (`node_mirror="auto"`, the default),
   `piceli.io/runtime=k3s|containerd` from the node's reported runtime.
   The annotation `piceli.io/managed-labels` records which labels init set,
   so when a role is removed a re-run removes that label and nothing else
   (labels set by hand stay).
2. **The in-cluster registry** and its node agents in `piceli-system`.
3. **The controller's foundation** in `piceli-system`: ServiceAccount,
   Role and RoleBinding, the ClusterRoles (`piceli-gitops`,
   `piceli-gitops-deployer`) and the state claim (see {doc}`gitops`).
4. **The UI** (after the controller's objects): its Deployment, ClusterIP
   Service, ServiceAccount and read-only RBAC, the empty launch Secret and
   the controller's request inbox (see {doc}`ui`).
5. The ConfigMap `piceli-system/piceli-cluster` with the declaration
   (`piceli.cluster.v1`, no credentials), which the controller and the UI
   read.

Without `--approve` it prints the plan (each node's label changes, each
object's `create`, `apply` or `no-op`) and its hash, exit 3. With the hash it
applies, then waits up to `--wait` seconds (default 120) for every node's
mirror agent to report. Init is idempotent: a re-run with nothing to change
prints `unchanged` (exit 0); after a change it plans only that.

## k3s nodes and restarts

On k3s nodes the registry's agent merges the mirror into
`/etc/rancher/k3s/registries.yaml` and also writes `hosts.toml` into k3s's
own `certs.d` (see {doc}`cluster_registry`). k3s reads `registries.yaml` only
when it starts, but every current k3s configures containerd to read
`certs.d`, so the mirror works at once; `registries.yaml` keeps it when k3s
restarts.

**Piceli never restarts k3s.** When a node's k3s does not read `certs.d`
(an old k3s), its agent reports `restart: needed`; `cluster init` and
`cluster status` list those nodes (`restart_needed`). Restart k3s there when
convenient: `systemctl restart k3s` on servers, `systemctl restart k3s-agent`
on agents (running pods keep running). A `registries.yaml` the agent cannot
edit without rewriting it (flow style or JSON) is left untouched and
reported `unmergeable`.

## Git credentials

```console
$ piceli secrets git --cluster infra.py:my_cluster --prompt [--username git]
Git token (not echoed):
created Secret piceli-system/piceli-build-git (keys: password, username); the token was not printed
```

The token is typed without echo (or piped on stdin: `pass show git-token |
piceli secrets git … --prompt`). It is never an argument or an environment
variable: an extra argument or `PICELI_GIT_TOKEN` is refused
(`secrets-token-refused`) without echoing what was passed. The command
writes the Secret `piceli-build-git` (`username`, `password`) in
`piceli-system`: the one Git credential the controller mounts and every
cluster build Job clones with. It creates or updates the Secret without a
plan (there is nothing to review but the value) and prints only names. It
needs `cluster init` first (`cluster-not-initialized`).

## Status

`piceli cluster status infra.py:my_cluster [--json]` (read-only) reports
`ready`, `degraded` (with `problems`) or `not-initialized`, and for each
part: the nodes (`name`, `arch`, `roles`, `ready`, runtime, missing labels
and `mirror {kind, state}` with the k3s `restart` need), the registry, the
controller (`health` and `last_poll` as `piceli gitops status` computes
them, `not-enabled` before `gitops enable`; its foundation), the UI
(`health`: `healthy`, `starting`, `not-installed`) and whether the Git
Secret exists (key names only). The UI reads the same document.
