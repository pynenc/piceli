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

The runner generates a launch token and passes it to the server and to the
browser fixture (`session.cjs`) through `PICELI_UI_LAUNCH_TOKEN` only; every
browser context opens the launch URL once before its journey, as a user does,
and the token is never printed.

Desktop (1280×800), tablet (768×1024), and phone (390×844) run serially against
the real service. Additional Playwright arguments follow the command, for
example `--project phone`. Reports, profiles, server state, and subprocesses
are scoped to the runner and removed on exit; the terminal carries results.
Screenshots, videos, and traces are disabled by default.

This read-only suite does not establish deployment/recovery, in-cluster
authentication, Kubernetes watches, accessibility certification, or performance
budgets. Those need their later delivery-wave acceptance targets.

`make test-browser-cluster-oidc` runs a separate Chromium journey against a
disposable self-signed HTTPS UI and local OIDC issuer. The issuer checks the
authorization-code PKCE challenge and signs the ID token. The browser follows
the cross-site callback, checks the secure SameSite=Strict session and CSRF
cookies, reads one scoped resource through the fake Kubernetes API, then
revokes the grant and confirms that the API denies that resource. The runner
creates separate temporary directories for the certificate, fake kubeconfig,
browser reports and profiles, and removes them and its subprocesses on exit.
Chromium accepts only this fixture's throwaway certificate. This covers the
browser login and cookie continuation; a real IdP, TLS gateway and kind
installation still need their separate acceptance run.

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
