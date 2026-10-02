# Browser acceptance

These journeys use the built Piceli application, its real Python query service,
and a disposable HTTP Kubernetes fake. They never use an ambient kubeconfig.
The service observes a Deployment, ConfigMap, and Secret; the browser checks
first use, honest state, keyboard navigation, search/deep-link persistence,
resource inspection, and retained observations after a transport failure.

To explore the current UI, run `make ui` from the Piceli repository root.
It builds the production frontend and opens disposable showcase state, normally
on port 4178, with plain-address session setup confined to that preview.
The [UI guide](../../docs/ui.md) maps the infrastructure, Sources, Cluster,
saved-plan archive, revision comparison and journal/log views.

For acceptance, install the locked frontend dependencies and build once before
starting the suites:

```sh
make ui-install ui-build
```

Do not rebuild assets while browser suites are running: their next navigation
may still refer to a chunk from the previous build. The base read-only suite
runs from the repository root:

```sh
uv run --frozen --extra ui python tests/browser/run.py
```

The runner uses `@playwright/test` from `ui/package-lock.json`. Install its
Chromium browser through that locked toolchain, or set
`PICELI_BROWSER_EXECUTABLE` to an existing compatible Chromium executable.
`PICELI_UI_TEST_PORT` overrides the reserved default loopback port, 4177.

From `ui/`, the locked Chromium installer is
`npm exec -- playwright install chromium`.

## Choose a journey

| Target | Coverage |
| --- | --- |
| `make test-browser` | First use, inventory, inspection, logs/access and transport failure |
| `make test-ui-composition` | Composition observation and existing Sync flows |
| `make test-browser-harbor` | Navigation, compact Sources, full revisions, environment/component links, application layout and appearance |
| `make test-browser-connected` | Topology and selected inspection, environment versions, workspace search, recorded plans/revisions, archive filters and direct captured-log bookmarks |
| `make test-browser-preview` | Plain-address preview access, fresh browsers, bookmarks and reload |
| `make test-browser-delivery` | Real service against the fake API: deployment, update, archived rollback, intentional failure and recovery |
| `make test-browser-cluster-oidc` | Local issuer, browser login/cookies and scope revocation |

The workspace and delivery suites cover desktop, tablet and phone. Inspect the
corresponding `*.config.cjs` when selecting one project or overriding its port.

## Sessions and cleanup

The runner generates a launch token and passes it to the server and to the
browser fixture (`session.cjs`) through `PICELI_UI_LAUNCH_TOKEN` only; every
browser context opens the launch URL once before its journey, as a user does,
and the token is never printed.

Desktop (1280×800), tablet (768×1024), and phone (390×844) run serially against
the real service. Additional Playwright arguments follow the command, for
example `--project phone`. Reports, profiles, server state, and subprocesses
are scoped to the runner and removed on exit; the terminal carries results.
Screenshots, videos, and traces are disabled by default.

The base read-only suite covers observation. Deployment/recovery and OIDC have
the separate targets above; real cluster behavior has the `test-ui-kind-*`
targets. Browser journeys do not establish accessibility certification or
performance budgets.

## Screenshots and recordings

After building, run `make ui-clips` with Chromium and `ffmpeg` installed.
It records fixed showcase journeys including compact Sources, saved-plan
history, ordered deployment phases, revision changes and captured logs.
The opt-in output is `.ui-clips/<run>/index.html`, with PNG screenshots and
animated WebP/GIF variants at most 1.5 MB each. Set `PICELI_UI_CLIPS_DIR` to
choose another output directory. Temporary videos, browser profiles, fake API
state and service processes are removed on exit; only the requested gallery
is retained. Copy selected assets into `docs/_static/ui/` for documentation,
and identify them as disposable fixture data.

## Cluster authentication and performance

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
