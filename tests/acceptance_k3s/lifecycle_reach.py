"""Stages 16-19 of the k3s lifecycle acceptance: workload logs and port forwards.

Mixed into :class:`lifecycle.Lifecycle` (it uses its cluster, commands and
``ui_session``). Everything goes through the in-cluster UI as a user opens it
(``piceli access ui --cluster infra.py:cluster``, the launch token kept in
memory) or through the commands of ``lifecycle_commands.toml``:

16. Logs: the store's container log in main, rc and the branch environment
    through the UI's log API, with a line the example prints; the previous
    container's log after its container is restarted (rc).
17. Redaction: the secret-like setting the store prints
    (``password=<made-up value>``) is masked in what the UI returns.
18. A forward from the in-cluster UI: a ticket for main's ``web`` Service,
    ``piceli ui connect`` with the pairing secret on stdin, a GET through the
    local port, then stopped from the UI; the session list follows it.
19. A forward from the CLI: ``piceli access lifecycle_access.py:main`` (and
    rc), a GET through 127.0.0.1:18080; a forward left behind (its piceli
    killed) is stopped by ``piceli access stop --stale``, which then leaves
    nothing.

The UI's API paths and the browser's pages are in :data:`ENDPOINTS` and
:data:`PAGES`: switch them there when the UI moves its Logs and Forwards
views. The pairing secret, like the launch token, is never printed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from lifecycle_support import (
    Result,
    Served,
    StageFailed,
    UiClient,
    UiError,
    alive,
    check,
    kill_group,
    log,
    port_open,
    processes,
    redact,
    run_bounded,
    wait_for,
)

#: The UI's API as the stages call it ({app}: an environment's scope id from
#: the composition, {resource}: a resource id, both URL-quoted).
ENDPOINTS = {
    "composition": "/api/v1/composition",
    "resources": "/api/v1/applications/{app}/resources",
    "log_sources": "/api/v1/applications/{app}/resources/{resource}/log-sources",
    "logs": "/api/v1/applications/{app}/resources/{resource}/logs",
    "tickets": "/api/v1/applications/{app}/remote-access",
    "ticket": "/api/v1/applications/{app}/remote-access/{ticket}",
}
#: The browser's pages (optional Playwright check): path and the selector of
#: the element whose text must hold the expected value.
PAGES = {
    "logs": (
        "/applications/{app}/resources?resource={resource}&panel=logs",
        ".log-output .log-line",
    ),
    "forwards": (
        "/applications/{app}/resources?resource={resource}&panel=access",
        ".access-panel",
    ),
}

#: What the example's store prints (examples/lifecycle/store/bin/run.sh).
STORE_LINE = "store: serving /var/lib/store on port 7000"
SECRET_LINE = "store: upstream password="
SECRET_VALUE = "example-not-a-real-password"
#: Environments whose logs stage 16 reads (when deployed), with namespaces.
LOG_ENVS = {"main": "lc-main", "rc": "lc-rc", "wp-feature": "lc-wp-feature"}
#: ``app.access.forward(local=18080)`` of the web Service; ``ui connect``'s port.
ACCESS_PORT = 18080
CONNECT_PORT = 18081
WEB_PAGE = "/index.html"


def _path(name: str, **parts: str) -> str:
    return ENDPOINTS[name].format(
        **{k: urllib.parse.quote(v, safe="") for k, v in parts.items()}
    )


def _fetch(port: int, path: str = WEB_PAGE) -> str | None:
    """The body of ``GET http://127.0.0.1:<port><path>``, or ``None``."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}{path}", timeout=5
        ) as response:
            return str(response.read(65536).decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError):
        return None


def _error(what: str, error: UiError) -> str:
    return f"{what}: {error.status} {error.code or 'no code'}"


class ReachStages:
    """Stages 16-19 (see the module docstring); mixed into ``Lifecycle``."""

    # ------------------------------------------------------------ the UI's API
    def _scope(self, client: UiClient, env: str) -> tuple[str, str]:
        """``env``'s scope id in the UI (its namespace's resources), or a problem."""
        body = client.get(ENDPOINTS["composition"])
        if isinstance(body, UiError):
            return "", _error(f"GET {ENDPOINTS['composition']}", body)
        for item in (body or {}).get("environments") or []:
            if item.get("name") == env:
                if item.get("application_id"):
                    return str(item["application_id"]), ""
                return "", f"{env}: no application_id in the composition"
        return "", f"{env}: not in the composition"

    def _workload(
        self, client: UiClient, app: str, kind: str, name: str
    ) -> tuple[dict[str, Any], str]:
        """The UI's resource ``kind/name`` of scope ``app``, or a problem."""
        last = ""

        def found() -> dict[str, Any] | None:
            nonlocal last
            items = client.items(_path("resources", app=app))
            if isinstance(items, UiError):
                last = _error("GET resources", items)
                return None
            for item in items:
                identity = item.get("identity") or {}
                if identity.get("kind") == kind and identity.get("name") == name:
                    return dict(item)
            last = f"no {kind}/{name} among {len(items)} resources"
            return None

        try:
            return wait_for(
                f"{kind}/{name} in the UI", found, timeout=90, interval=3
            ), ""
        except StageFailed:
            return {}, last

    def _log_sources(
        self, client: UiClient, app: str, resource: dict[str, Any]
    ) -> list[dict[str, Any]] | UiError:
        query = urllib.parse.urlencode(
            {"resource_uid": (resource.get("identity") or {}).get("uid") or ""}
        )
        body = client.get(
            f"{_path('log_sources', app=app, resource=resource['id'])}?{query}"
        )
        if isinstance(body, UiError):
            return body
        return list((body or {}).get("items") or [])

    def _read_log(
        self,
        client: UiClient,
        app: str,
        resource: dict[str, Any],
        source: dict[str, Any],
        *,
        previous: bool = False,
    ) -> list[str] | UiError:
        pod = source.get("pod") or {}
        containers = list(source.get("containers") or [])
        container = (
            resource["identity"]["name"]
            if resource["identity"]["name"] in containers
            else (containers or [""])[0]
        )
        query = urllib.parse.urlencode(
            {
                "resource_uid": resource["identity"].get("uid") or "",
                "pod_name": pod.get("name") or "",
                "pod_uid": pod.get("uid") or "",
                "container": container,
                "previous": "true" if previous else "false",
                "tail_lines": "500",
            }
        )
        body = client.get(f"{_path('logs', app=app, resource=resource['id'])}?{query}")
        if isinstance(body, UiError):
            return body
        return [str(line) for line in (body or {}).get("lines") or []]

    def _store_log(
        self, client: UiClient, env: str
    ) -> tuple[str, dict[str, Any], dict[str, Any], list[str], str]:
        """``env``'s store log through the UI: (app, resource, pod source, lines, problem).

        Waits until the newest running pod's log holds :data:`STORE_LINE`.
        """
        app, problem = self._scope(client, env)
        if problem:
            return app, {}, {}, [], problem
        resource, problem = self._workload(client, app, "StatefulSet", "store")
        if problem:
            return app, resource, {}, [], f"{env}: {problem}"
        capability = (resource.get("capabilities") or {}).get("logs") or {}
        if not capability.get("allowed", True):
            return (
                app,
                resource,
                {},
                [],
                (
                    f"{env}: logs not allowed for statefulset/store ({capability.get('reason')!r})"
                ),
            )
        state: dict[str, Any] = {"problem": "", "source": {}, "lines": []}

        def read() -> bool:
            sources = self._log_sources(client, app, resource)
            if isinstance(sources, UiError):
                state["problem"] = _error("GET log-sources", sources)
                return False
            running = [s for s in sources if s.get("phase") == "Running"]
            if not running:
                state["problem"] = (
                    f"no running store pod among {len(sources)} log sources"
                )
                return False
            source = running[-1]
            lines = self._read_log(client, app, resource, source)
            if isinstance(lines, UiError):
                state["problem"] = _error("GET logs", lines)
                return False
            state.update(source=source, lines=lines, problem="")
            if not any(STORE_LINE in line for line in lines):
                state["problem"] = f"no {STORE_LINE!r} in {len(lines)} log line(s)"
                return False
            return True

        try:
            wait_for(f"{env}: the store's log in the UI", read, timeout=120, interval=5)
        except StageFailed:
            return (
                app,
                resource,
                state["source"],
                state["lines"],
                f"{env}: {state['problem']}",
            )
        pod = (state["source"].get("pod") or {}).get("name")
        log(f"{env}: {len(state['lines'])} log line(s) of {pod} through the UI; "
            f"{STORE_LINE!r} found")  # fmt: skip
        return app, resource, state["source"], state["lines"], ""

    # ------------------------------------------------------------ stage 16
    def stage_16_ui_logs(self) -> None:
        envs = [e for e, ns in LOG_ENVS.items() if self.cluster.get("namespace", ns)]
        check("main" in envs, "lc-main is not deployed")
        skipped = sorted(set(LOG_ENVS) - set(envs))
        if skipped:
            log(f"not deployed (their stages did not run): {skipped}")
        problems: list[str] = []
        pages: list[dict[str, str]] = []
        with self.ui_session() as (client, url):
            for env in envs:
                app, resource, _, lines, problem = self._store_log(client, env)
                if problem:
                    problems.append(problem)
                    continue
                shape = _log_shape(lines)
                if shape:
                    problems.append(f"{env}: {shape}")
                if env == "main":
                    path, selector = PAGES["logs"]
                    pages.append({
                        "name": "logs of main's store",
                        "path": path.format(app=urllib.parse.quote(app, safe=""),
                                            resource=urllib.parse.quote(resource["id"], safe="")),
                        "selector": selector, "text": STORE_LINE,
                    })  # fmt: skip
            if "rc" in envs:
                problems.extend(f"rc previous: {p}" for p in self._previous_log(client))
            self._browser_pages(url, pages, problems)
        check(not problems, "; ".join(problems))

    def _previous_log(self, client: UiClient) -> list[str]:
        """Restart rc's store container (its node's crictl); read the previous log."""
        app, resource, source, _, problem = self._store_log(client, "rc")
        if problem:
            return [problem]
        pod_name = (source.get("pod") or {}).get("name") or ""
        pod = self.cluster.get("pod", pod_name, "-n", "lc-rc") or {}
        statuses = (pod.get("status") or {}).get("containerStatuses") or []
        status = next((s for s in statuses if s.get("name") == "store"), None)
        if not status or not status.get("containerID"):
            return [f"no store container status in {pod_name}"]
        restarts = int(status.get("restartCount") or 0)
        container_id = str(status["containerID"]).split("://", 1)[-1]
        node = pod["spec"].get("nodeName", "")
        stopped = self.proc.run(
            ["docker", "exec", node, "crictl", "stop", "--timeout", "5", container_id],
            check_exit=None, timeout=120,
        )  # fmt: skip
        if stopped.code != 0:
            return [f"crictl stop on {node}: exit {stopped.code}"]

        def restarted() -> bool:
            body = self.cluster.get("pod", pod_name, "-n", "lc-rc") or {}
            for item in (body.get("status") or {}).get("containerStatuses") or []:
                if item.get("name") == "store":
                    return int(item.get("restartCount") or 0) > restarts and bool(
                        item.get("ready")
                    )
            return False

        try:
            wait_for(
                "rc's store container restarted", restarted, timeout=180, interval=3
            )
        except StageFailed as error:
            return [str(error)]
        sources = self._log_sources(client, app, resource)
        if isinstance(sources, UiError):
            return [_error("GET log-sources", sources)]
        same = next(
            (s for s in sources if (s.get("pod") or {}).get("uid") == (source.get("pod") or {}).get("uid")),
            None,
        )  # fmt: skip
        if same is None:
            return [
                f"{pod_name} is no longer a log source after its container restarted"
            ]
        lines = self._read_log(client, app, resource, same, previous=True)
        if isinstance(lines, UiError):
            return [_error("GET logs (previous)", lines)]
        log(
            f"rc: previous container of {pod_name}: {len(lines)} line(s) through the UI"
        )
        if not any(STORE_LINE in line for line in lines):
            return [
                f"no {STORE_LINE!r} in the previous container's {len(lines)} line(s)"
            ]
        return []

    # ------------------------------------------------------------ stage 17
    def stage_17_ui_log_redaction(self) -> None:
        envs = [e for e, ns in LOG_ENVS.items() if self.cluster.get("namespace", ns)]
        check("main" in envs, "lc-main is not deployed")
        problems: list[str] = []
        with self.ui_session() as (client, _):
            for env in envs:
                _, _, _, lines, problem = self._store_log(client, env)
                if problem:
                    problems.append(problem)
                    continue
                setting = [line for line in lines if SECRET_LINE in line]
                shown = [line for line in lines if SECRET_VALUE in line]
                # Never print the lines themselves: only what was masked.
                log(f"{env}: {len(setting)} line(s) with {SECRET_LINE!r}; "
                    f"value shown unmasked in {len(shown)}")  # fmt: skip
                if not setting:
                    problems.append(f"{env}: no {SECRET_LINE!r} line (masked or not)")
                if shown:
                    problems.append(
                        f"{env}: the UI returns the secret-like setting unmasked "
                        f"({len(shown)} line(s) of the store's log)"
                    )
        check(not problems, "; ".join(problems))

    # ------------------------------------------------------------ stage 18
    def stage_18_ui_forward(self) -> None:
        check(
            not port_open(CONNECT_PORT),
            f"127.0.0.1:{CONNECT_PORT} is in use before the stage",
        )
        problems: list[str] = []
        with self.ui_session() as (client, url):
            app, problem = self._scope(client, "main")
            check(not problem, problem)
            web, problem = self._workload(client, app, "Service", "web")
            check(not problem, f"main: {problem}")
            capability = (web.get("capabilities") or {}).get("access") or {}
            log(f"main: service/web access capability {json.dumps(capability)}, "
                f"ports {web.get('ports')}")  # fmt: skip
            tickets = _path("tickets", app=app)
            issued = client.request(
                "POST", tickets,
                {"resource_id": web["id"], "resource_uid": web["identity"].get("uid") or "",
                 "remote_port": 8080, "duration_seconds": 300},
            )  # fmt: skip
            if isinstance(issued, UiError):
                problems.append(
                    "the in-cluster UI issues no forward ticket for service/web "
                    f"(POST {ENDPOINTS['tickets']}: {issued.status} {issued.code or 'no code'}; "
                    f"access capability {capability.get('allowed')!r}/{capability.get('reason')!r})"
                )
                problems.extend(self._connect_probe(client.origin + "/"))
                check(not problems, "; ".join(problems))
            session = issued.get("session") or {}
            ticket = str(session.get("id") or "")
            secret = str(issued.get("pairing_secret") or "")  # never printed
            log(f"ticket {ticket[:8]}…: {session.get('state')}")
            problems.extend(
                self._forward_through_ticket(client, app, ticket, secret, url, web)
            )
        check(not problems, "; ".join(problems))

    def _session_state(self, client: UiClient, app: str, ticket: str) -> str:
        body = client.get(_path("tickets", app=app))
        if isinstance(body, UiError):
            return f"error {body.status} {body.code}"
        for item in (body or {}).get("items") or []:
            if item.get("id") == ticket:
                return str(item.get("state"))
        return "absent"

    def _forward_through_ticket(
        self, client: UiClient, app: str, ticket: str, secret: str, url: str,
        web: dict[str, Any],
    ) -> list[str]:  # fmt: skip
        problems: list[str] = []
        seen = [self._session_state(client, app, ticket)]
        if seen[0] != "pending":
            problems.append(f"a new ticket is listed {seen[0]!r}, not 'pending'")
        values = {"ticket": ticket, "ui_server": client.origin + "/",
                  "connect_port": str(CONNECT_PORT)}  # fmt: skip
        connect = self.serve(
            "ui-connect", values=values, secrets={"pairing_secret": secret}
        )
        kubectl: list[int] = []
        try:

            def ready() -> bool:
                state = self._session_state(client, app, ticket)
                if state != seen[-1]:
                    seen.append(state)
                if connect.process.poll() is not None:
                    raise_exit(connect)
                return state == "ready" and port_open(CONNECT_PORT)

            try:
                wait_for("the UI's session ready and the local port open", ready,
                         timeout=180, interval=2)  # fmt: skip
            except StageFailed as error:
                problems.append(f"{error} (states {seen}): {connect.text()[-600:]}")
                return problems
            kubectl = [p for p, c in _descendants(connect.pid) if "port-forward" in c]
            body = wait_for(f"GET 127.0.0.1:{CONNECT_PORT}{WEB_PAGE}",
                            lambda: _fetch(CONNECT_PORT), timeout=60, interval=2)  # fmt: skip
            if "lifecycle" not in str(body):
                problems.append(
                    f"127.0.0.1:{CONNECT_PORT}{WEB_PAGE} is not main's web page"
                )
            else:
                log(f"main's web page through 127.0.0.1:{CONNECT_PORT} (ui connect)")
            path, selector = PAGES["forwards"]
            self._browser_pages(url, [{
                "name": "the forward session of main's web",
                "path": path.format(app=urllib.parse.quote(app, safe=""),
                                    resource=urllib.parse.quote(web["id"], safe="")),
                "selector": selector, "text": f"{CONNECT_PORT}",
            }], problems)  # fmt: skip
            stopped = client.request("DELETE", _path("ticket", app=app, ticket=ticket))
            if isinstance(stopped, UiError):
                problems.append(_error(f"DELETE {ENDPOINTS['ticket']}", stopped))
            try:
                wait_for(
                    "ui connect to stop with its session",
                    lambda: connect.process.poll() is not None and not port_open(CONNECT_PORT),
                    timeout=60, interval=2,
                )  # fmt: skip
            except StageFailed as error:
                problems.append(f"{error} (stopped in the UI)")
            final = self._session_state(client, app, ticket)
            if final != seen[-1]:
                seen.append(final)
            log(f"ticket {ticket[:8]}… states in the UI: {seen}")
            if final != "stopped":
                problems.append(
                    f"the session is {final!r} after it was stopped (states {seen})"
                )
            return problems
        finally:
            connect.stop()
            for pid in kubectl:
                if alive(pid):
                    kill_group(pid)

    def _connect_probe(self, server: str) -> list[str]:
        """Without a ticket: does ``ui connect`` accept this UI's address at all?

        A well-formed ticket id that does not exist and a placeholder secret:
        an accepted address ends in ``ui-not-found`` (the claim); a refused
        one in ``ui-invalid-request`` before any request.
        """
        values = {
            "ticket": "0" * 32,
            "ui_server": server,
            "connect_port": str(CONNECT_PORT),
        }
        argv = self.commands.argv(
            "ui-connect", {**self.values, "piceli": self.piceli, **values}
        )
        done = run_bounded(argv, env=self.env, cwd=self.infra, timeout=60,
                           stdin="placeholder-" + "x" * 40)  # fmt: skip
        reason = (done.json() or {}).get("reason")
        log(
            f"ui connect against the in-cluster UI ({server}): exit {done.code}, {reason!r}"
        )
        if reason == "ui-invalid-request":
            return [f"`piceli ui connect` refuses the in-cluster UI's address {server} "
                    "(ui-invalid-request: it takes an https server only)"]  # fmt: skip
        if reason not in {"ui-not-found"}:
            return [f"ui connect probe: exit {done.code}, {reason!r}: "
                    f"{redact(done.stderr.strip())[-300:]}"]  # fmt: skip
        return []

    # ------------------------------------------------------------ stage 19
    def stage_19_cli_forward(self) -> None:
        check(
            not port_open(ACCESS_PORT),
            f"127.0.0.1:{ACCESS_PORT} is in use before the stage",
        )
        problems: list[str] = []
        problems.extend(
            f"main: {p}" for p in self._cli_forward("main", left_behind=True)
        )
        if self.cluster.get("namespace", "lc-rc"):
            problems.extend(
                f"rc: {p}" for p in self._cli_forward("rc", left_behind=False)
            )
        check(not problems, "; ".join(problems))

    def _cli_forward(self, env: str, *, left_behind: bool) -> list[str]:
        """``piceli access`` of ``env``; a GET through it; then stop it.

        ``left_behind``: piceli's process group (piceli and its forward
        watchdog) is killed (SIGKILL), its kubectl keeps the port, ``access
        stop --stale`` must stop it. Otherwise Ctrl-C, and
        ``access stop --stale`` finds nothing. Either way nothing remains.
        """
        values = {"access_env": env}
        problems: list[str] = []
        served = self.serve("access-env", values=values)
        kubectl: list[int] = []
        try:

            def reached() -> str | None:
                if served.process.poll() is not None:
                    raise_exit(served)
                return _fetch(ACCESS_PORT) if port_open(ACCESS_PORT) else None

            try:
                body = wait_for(f"{env}: web through 127.0.0.1:{ACCESS_PORT}", reached,
                                timeout=180, interval=2)  # fmt: skip
            except StageFailed as error:
                return [f"{error}: {served.text()[-600:]}"]
            states = _json_lines(served.out)
            log(f"{env}: piceli access: {[s.get('event') or s.get('state') for s in states][:6]}; "
                f"web page {'ok' if 'lifecycle' in body else 'unexpected'}")  # fmt: skip
            if "lifecycle" not in body:
                problems.append(
                    f"127.0.0.1:{ACCESS_PORT}{WEB_PAGE} is not {env}'s web page"
                )
            kubectl = [p for p, c in _descendants(served.pid) if "port-forward" in c]
            if left_behind:
                # piceli and its forward watchdog (its process group): a
                # killed piceli alone no longer leaves its kubectl (0.14.7).
                kill_group(served.pid)
                served.process.wait(timeout=10)
                time.sleep(2)
                held = port_open(ACCESS_PORT) and any(alive(p) for p in kubectl)
                log(f"{env}: piceli access killed; its kubectl {'holds' if held else 'left'} "
                    f"127.0.0.1:{ACCESS_PORT}")  # fmt: skip
            else:
                served.stop()
                held = False
                if not any(s.get("event") == "stopped" or s.get("state") == "stopped"
                           for s in _json_lines(served.out)):  # fmt: skip
                    problems.append("no 'stopped' line after Ctrl-C")
            stopped = self.bounded("access-env-stop-stale", timeout=60, values=values)
            gone = [
                (i.get("port"), i.get("holder"))
                for i in (stopped.json() or {}).get("stopped") or []
            ]
            log(f"{env}: access stop --stale: exit {stopped.code}, stopped {gone}")
            if stopped.code != 0:
                problems.append(f"access stop --stale: exit {stopped.code}: "
                                f"{redact(stopped.stderr.strip())[-300:]}")  # fmt: skip
            elif held and (ACCESS_PORT, "piceli-forward") not in gone:
                problems.append(
                    f"access stop --stale stopped {gone}, not the left-behind forward"
                )
            elif not held and gone:
                problems.append(
                    f"access stop --stale stopped {gone} after a clean stop"
                )
            try:
                wait_for(
                    f"127.0.0.1:{ACCESS_PORT} free and no kubectl left",
                    lambda: not port_open(ACCESS_PORT) and not any(alive(p) for p in kubectl),
                    timeout=20, interval=1,
                )  # fmt: skip
            except StageFailed:
                problems.append(
                    f"a forward still holds {ACCESS_PORT} (kubectl {kubectl})"
                )
            return problems
        finally:
            served.stop()
            # This run's forward of the access port, whatever happened above.
            mine = str(self.cluster.kubeconfig)
            for pid, _, command in processes():
                if "port-forward" in command and (
                    pid in kubectl or (mine in command and f"{ACCESS_PORT}:" in command)
                ):
                    kill_group(pid)

    # ------------------------------------------------------------ browser
    def _browser_pages(
        self, url: str, pages: list[dict[str, str]], problems: list[str]
    ) -> None:
        """Optional: each page shows its text (Playwright from ``ui/node_modules``)."""
        if not pages:
            return
        ui = self.args.playwright_ui
        node = shutil.which("node")
        if not (node and (ui / "node_modules" / "@playwright" / "test").is_dir()):
            log(f"browser check skipped: no node or no Playwright in {ui}/node_modules")
            return
        script = self.scratch / "pages.mjs"
        script.write_text(PAGES_SCRIPT)
        try:
            done = subprocess.run(
                [node, str(script)], capture_output=True, text=True, timeout=240,
                env={**self.env, "PW_UI_DIR": str(ui), "PW_LAUNCH_URL": url,
                     "PW_PAGES": json.dumps(pages)},
                check=False,
            )  # fmt: skip
        except subprocess.TimeoutExpired:
            problems.append("browser: the pages did not load in 240s")
            return
        body = Result([], done.returncode, done.stdout, done.stderr).json() or {}
        log(f"browser: {json.dumps(body)[:400]}")
        if done.returncode != 0 or not body:
            problems.append(
                f"browser: exit {done.returncode}: {redact(done.stderr[-600:])}"
            )
            return
        for name, found in (body.get("found") or {}).items():
            if not found:
                problems.append(f"browser: {name} not shown")


def raise_exit(served: Served) -> None:
    time.sleep(0.5)  # let the pumps drain
    raise StageFailed(f"exited {served.process.returncode}: {served.text()[-600:]}")


def _log_shape(lines: list[str]) -> str:
    """What is wrong with the shape of a log read, or ``""``.

    The store prints two lines before its server starts: they must come back
    as separate lines (each with its timestamp), not as one line holding a
    Python bytes literal with escaped newlines.
    """
    literal = [line for line in lines if line.startswith(("b'", 'b"')) or "\\n" in line]
    if literal:
        return (
            f"the UI returns the log as {len(lines)} line(s), {len(literal)} of them "
            "a bytes literal with escaped newlines (not one line per log line)"
        )
    together = [line for line in lines if STORE_LINE in line and SECRET_LINE in line]
    if together:
        return "two log lines of the store come back as one"
    return ""


def _descendants(root: int) -> list[tuple[int, str]]:
    """``(pid, command)`` of every process below ``root``."""
    table = processes()
    found: list[tuple[int, str]] = []
    parents = {root}
    while True:
        more = [(p, c) for p, pp, c in table if pp in parents and p not in parents]
        if not more:
            return found
        found.extend(more)
        parents |= {p for p, _ in more}


def _json_lines(lines: list[str]) -> list[dict[str, Any]]:
    found = []
    for line in list(lines):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            found.append(value)
    return found


PAGES_SCRIPT = """\
import { createRequire } from 'node:module';
const require = createRequire(process.env.PW_UI_DIR + '/package.json');
const { chromium } = require('@playwright/test');
const launch = process.env.PW_LAUNCH_URL;
const origin = new URL(launch).origin;
const browser = await chromium.launch();
try {
  const page = await browser.newPage();
  await page.goto(launch);
  const found = {};
  for (const item of JSON.parse(process.env.PW_PAGES || '[]')) {
    await page.goto(origin + item.path);
    await page.waitForLoadState('networkidle').catch(() => {});
    const match = page.locator(item.selector).filter({ hasText: item.text });
    await match.first().waitFor({ timeout: 30000 }).catch(() => {});
    found[item.name] = (await match.count()) > 0;
  }
  console.log(JSON.stringify({ found }));
} finally {
  await browser.close();
}
"""
