# Check a release and roll back automatically

Declare `[[checks]]` in `release.toml` and set
`rollback_on_failed_checks = true`: after `piceli release apply` becomes ready it
runs the checks, and when one fails it re-applies the previous ready release
without operator action and records the failed check and the rollback.

```{admonition} Maturity: preview
:class: note

Post-deploy checks (`[[checks]]`, `piceli.checks`, `piceli release check`,
`--skip-checks`) are new. Keys, JSON fields and the Python API may change in a
minor release, with a changelog entry. See the {doc}`roadmap` for every
feature's status.
```

Readiness only says that pods started and their probes pass. A check says that
the release *works*: the login page answers, a self-test command exits `0`,
the error rate stays low. A release is `ready` only when its checks pass.

## Prerequisites

- A working `piceli release` spec (see {doc}`release_cli`).
- `kubectl` on `PATH` for `http` and `metric` checks. They open a temporary
  loopback port forward with the spec's kubeconfig and context, never the
  current context.
- For `exec` checks, the spec's credentials need `create` on `pods/exec` in
  the namespace.

## Steps

1. Add checks and the rollback policy to `release.toml`:

   ```toml
   [release]
   name = "shop"
   # … owner, field_manager, composition, state_dir …
   rollback_on_failed_checks = true

   [[checks]]
   type = "http"
   target = "service/web"
   path = "/login"
   expect = 200
   body_contains = "Sign in"

   [[checks]]
   type = "exec"
   target = "deployment/api"
   command = ["api", "self-test"]
   ```

2. Plan. The plan validates the checks (a `python` check must import) and
   shows what `apply` will run; nothing is executed yet:

   ```sh
   piceli release plan --spec release.toml
   ```

   ```json
   "checks": {"names": ["http-service-web-login", "exec-deployment-api-api"],
              "rollback_on_failed_checks": true}
   ```

   The checks and the policy are stored with the plan: `apply` runs exactly
   what was reviewed, even if the spec changes afterwards.

3. Apply the approved plan:

   ```sh
   piceli release apply --spec release.toml --approve <plan hash>
   ```

   When the checks pass, the output has `"release_state": "ready"` and the
   exit code is `0`:

   ```text
     check http-service-web-login: passed GET /login returned 200
     check exec-deployment-api-api: passed api exited 0
   apply shop-3f9a1c2e4b5d: ready
   ```

4. When a check fails, the command exits `1` and the result records the
   failed check and the automatic rollback:

   ```text
     check http-service-web-login: FAILED [check-failed] GET /login returned 500, expected 200
   apply shop-8b20e71d9c44: checks-failed
     rolled back automatically to shop-3f9a1c2e4b5d: ready
       check http-service-web-login: passed GET /login returned 200
   ```

   ```json
   {
     "release": "shop-8b20e71d9c44",
     "release_state": "checks-failed",
     "execution": {"state": "ready", "execution_id": "…"},
     "checks": {"passed": false, "failed": ["http-service-web-login"],
                "results": [{"name": "http-service-web-login", "type": "http",
                             "passed": false, "code": "check-failed",
                             "detail": "GET /login returned 500, expected 200",
                             "attempts": 4, "duration": 6.2}]},
     "rollback": {"state": "rolled-back", "target": "shop-3f9a1c2e4b5d",
                  "release_state": "ready", "execution": {"…": "…"},
                  "checks": {"passed": true, "…": "…"}}
   }
   ```

5. See the outcome later without contacting the cluster:

   ```sh
   piceli release status --spec release.toml
   ```

   Each release has its latest `checks` outcome, and the history shows the
   `checks-failed` execution followed by the rollback (`"trigger":
   "checks-failed"`).

To run the checks again at any time, without applying or rolling back
anything:

```sh
piceli release check --spec release.toml [--release NAME]
```

## Check types

Every check has `name` (unique; derived from the check when omitted),
`timeout` (seconds per attempt, default `10`), `retries` (extra attempts,
default `3`) and `interval` (seconds between attempts, default `2`). Checks run
in order; every check runs even after one failed.

| Type | Passes when | Keys |
| --- | --- | --- |
| `http` | `GET path` through a temporary supervised port forward returns a status in `expect` and the body contains `body_contains` | `target` (`service/`, `deployment/` or `pod/NAME`), `path` (`/`), `port` (the target's first port), `expect` (`[200, 299]`; one code or `[low, high]`), `body_contains` |
| `exec` | `command` in the newest ready pod of `target` exits `expect_exit` and stdout contains `output_contains` | `target` (`deployment/`, `statefulset/`, `daemonset/` or `pod/NAME`), `command` (argv list), `container` (the first), `expect_exit` (`0`), `output_contains` |
| `metric` | a Prometheus-compatible query returns at least one sample and every sample satisfies `value <op> threshold` | `target`, `query`, `op` (`<`, `<=`, `>`, `>=`, `==`, `!=`; default `<=`), `threshold`, `port`, `path` (`/api/v1/query`), `empty` (`fail` or `pass` when there are no samples) |
| `python` | the function returns `None` or `True` | `call`: `module:function` or `path/file.py:function` (relative to `release.toml`) |

The same checks in Python, with App handles or plain `kind/name` targets:

```python
from piceli import App
from piceli.checks import Checks

app = App("shop", owner="shop-prod")
web = app.deployment("web", image="registry.test/web@sha256:…", ports=[3000])
web_service = app.service(web, port=80, target_port=3000)

checks = [
    Checks.http(web_service, "/login", expect=200, body_contains="Sign in"),
    Checks.exec("deployment/api", ["api", "self-test"], retries=0),
    Checks.metric(
        "service/prometheus",
        "sum(rate(http_errors_total[5m]))",
        op="<",
        threshold=0.05,
        port=9090,
    ),
    Checks.python("checks.py:smoke"),
]
```

A handle's first port becomes the check's `port`. In TOML, and for a plain
target without `port`, the port is read from the live Service or pod template.

### Python checks

A `python` check receives a `piceli.checks.CheckContext`:

```python
# checks.py, next to release.toml
from piceli.checks import CheckContext, CheckFailed


def smoke(ctx: CheckContext) -> str | None:
    response = ctx.http_get("service/web", "/api/health")  # through a forward
    if response.status != 200:
        raise CheckFailed(f"health returned {response.status}")
    result = ctx.exec("deployment/api", ["api", "check-db"])  # pods/exec
    if result.exit_code != 0:
        raise CheckFailed("api cannot reach its database")
    if "web" not in ctx.images:
        return "the release has no web image"  # a string fails
    return None  # passes
```

It fails by returning `False` or a string (the detail), or by raising
`CheckFailed(detail)`. Any other exception fails with `check-raised` and only
the exception type is recorded, so an exception message can never leak a
secret. Never put a secret value in a detail: details are printed and stored
in the release history.

## Run checks from Python

`run_checks(checks, context) -> CheckReport` is the whole interface; `piceli
release` uses it after readiness.

```python
from pathlib import Path

from piceli.checks import CheckContext, Checks, run_checks

with CheckContext(
    Path("cluster.kubeconfig"),  # explicit file; KUBECONFIG is never read
    "kind-shop",  # explicit context; never current-context
    "shop",  # namespace
    "shop-3f9a1c2e4b5d",  # release name
    {"web": "registry.test/web@sha256:…"},  # images: references, ImageRef or dicts
) as ctx:
    report = run_checks(Checks.http("service/web", "/login"), ctx)

report.passed  # True only when every check passed
for result in report.results:  # CheckResult(name, passed, detail, duration, …)
    print(result.name, result.passed, result.code, result.detail)
report.to_dict()  # {"passed", "failed": [names], "results": […]}
```

`CheckContext(kubeconfig, context, namespace, release, images=None, *,
values=None, base=None, transport="https", request_seconds=10.0,
kubectl="kubectl", forward_seconds=20.0, forwarder=None, executor=None,
api_client=None)`: building one contacts nothing; the API client is created on
first use and closed by `close()` or the `with` block. `base` is where relative
`python` check files resolve from. `forwarder`, `executor` and `api_client`
replace the port forward, the exec transport and the client (tests).
`CheckResult` also has `type`, `attempts` and `code` (`None` when passed).

## Release states and rollback

After an execution, `release_state` in the result and `state` in the history
is one of:

| State | Meaning | Selected | Exit |
| --- | --- | --- | --- |
| `ready` | The execution is ready and every check passed (or there are none, or `--skip-checks`) | yes | `0` |
| `checks-failed` | The execution is ready but a check failed | no (the previous selection is kept) | `1` |
| `failed`, `blocked`, `cancelled`, … | The execution did not become ready; no check ran | no | `1` |

With `rollback_on_failed_checks = true`, a `checks-failed` release triggers
exactly one automatic rollback:

- The target is the last **other** release whose execution was `ready`. If
  there is none, `rollback.state` is `unavailable`
  (`checks-rollback-unavailable`) and the failed release is left running.
- The rollback is an ordinary re-apply: it is planned against fresh discovery,
  executed through the journal as a new execution, and runs the checks too.
  Its history entry has `"trigger": "checks-failed"`, `rolled_back_from` and
  `failed_execution_id`; the failed entry gets `rollback` with the state,
  target and execution id.
- It needs no second approval: `rollback_on_failed_checks = true` was part of
  the approved plan. It never triggers another rollback. If the rollback is
  refused, does not become ready or fails its own checks, `rollback.state` is
  `failed` with `checks-rollback-failed`.

The full report of each checked execution is kept privately in
`state_dir/checks/<execution id>.json`.

`resume` runs the checks when the resumed execution becomes ready, with the
same outcome and automatic rollback. A manual `rollback` runs the current
spec's checks against the restored release.

### Skipping checks in an emergency

`--skip-checks` on `apply`, `rollback` or `resume` marks the release `ready`
without running its checks. It is recorded: the result has
`"checks": {"skipped": true, "flag": "--skip-checks", "declared": […]}` and the
history entry has `"skip_checks": true`. Use it only to restore service when a
check itself is wrong, then fix the check.

## If it fails

Each failing check has a `code`; `piceli explain <code>` prints the cause and
the fix.

| Code | Means | Next step |
| --- | --- | --- |
| `check-failed` | The condition was not met (status, body, exit code, threshold, a Python check returned False) | Read `detail`, fix the application, apply a new release |
| `check-forward-unavailable` | The temporary port forward did not become healthy | Check `kubectl` is on `PATH` and a ready pod listens on the port |
| `check-target-not-found` | No such Service/workload/pod, or no ready pod | Check the target name against the composition |
| `check-port-unknown` | No `port` and the target declares none | Set `port` |
| `check-timed-out` | An attempt exceeded `timeout` | Raise `timeout` or `retries` |
| `check-exec-unavailable` | The API refused the exec | Grant `pods/exec`, or fix `command`/`container` |
| `check-api-unavailable` | The kubeconfig context cannot read the namespace | Fix `[target]` or the credentials' permissions |
| `check-metric-invalid` | Not a Prometheus query response | Fix `path`, `port` or the query |
| `check-raised`, `check-callable-invalid` | A Python check raised, returned something odd, or cannot be imported | Run the function locally |
| `checks-rollback-unavailable`, `checks-rollback-failed` | The automatic rollback had no target or did not succeed | `piceli release status`, then `piceli release rollback previous` |

A spec with an invalid `[[checks]]` table is refused before planning (exit `2`,
`check-invalid` in the message).

## For agents

- `piceli release check` changes no Piceli state and never rolls back, but its
  checks may exec the declared commands in pods and run the declared Python
  functions: ask the owner before running it on a spec you did not write.
- An automatic rollback happens only when the approved plan had
  `rollback_on_failed_checks = true`; after `apply` exits `1`, read
  `release_state` and `rollback` before doing anything else.
- Never add `--skip-checks` unless the owner asked for it for this run.
