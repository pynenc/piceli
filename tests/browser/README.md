# Browser acceptance

These journeys use the built Piceli application, its real Python query service,
and a disposable HTTP Kubernetes fake. They never use an ambient kubeconfig.
The service observes a Deployment, ConfigMap, and Secret; the browser checks
first use, honest state, keyboard navigation, search/deep-link persistence,
resource inspection, and retained observations after a transport failure.

After installing the locked frontend dependencies and building its assets, run
from the Piceli repository root:

```sh
uv run --frozen python tests/browser/run.py
```

The runner uses `@playwright/test` from `ui/package-lock.json`. Install its
Chromium browser through that locked toolchain, or set
`PICELI_BROWSER_EXECUTABLE` to an existing compatible Chromium executable.
`PICELI_UI_TEST_PORT` overrides the reserved default loopback port, 4177.

Desktop (1280×800), tablet (768×1024), and phone (390×844) run serially against
the real service. Additional Playwright arguments follow the command, for
example `--project phone`. Reports, profiles, server state, and subprocesses
are scoped to the runner and removed on exit; the terminal carries results.
Screenshots, videos, and traces are disabled by default.

This read-only suite does not establish deployment/recovery, in-cluster
authentication, Kubernetes watches, accessibility certification, or performance
budgets. Those need their later delivery-wave acceptance targets.

`make test-ui-browser-performance` runs a separate synthetic scale fixture with
ten Chromium processes, 100 ms added API latency, fourfold CPU throttling and
50 registrations/5,000 objects across three scopes. It measures desktop and
phone first list navigation in turn with all ten browser processes alive, then
concurrent resource load, selection response and scroll long tasks. It then
reads a scoped synthetic Pod through the real log service at 1,000 lines/s for
ten seconds, checking visible continuity/gaps and log-scroll tasks. The runner
prints one JSON record per viewport and removes browser profiles and server
resources even when a published budget fails. Ten simultaneous cold starts are
a separate stress case. It does not model ten independent physical machines or
a real-cluster log load.
