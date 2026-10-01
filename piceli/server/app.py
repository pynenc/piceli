"""FastAPI transport for the shared typed query service and packaged UI."""

from __future__ import annotations

import asyncio
import html
import json
import re
import threading
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.exceptions import HTTPException
from starlette.middleware.sessions import SessionMiddleware

from piceli.server.security import LocalSecurity
from piceli.services.authority import request_principal
from piceli.services.contracts import (
    AccessPage,
    AccessSession,
    AccessStartRequest,
    Application,
    ApplicationPage,
    CancelRequest,
    Capabilities,
    Capability,
    ClusterBuildOperationRequest,
    ClusterBuildPlanRequest,
    CompositionSyncRequest,
    EnvironmentActionRequest,
    Evaluation,
    EvaluationPreview,
    EvaluationRequest,
    Event,
    GitOpsApprovalRequest,
    GitOpsPromotionRequest,
    LogBatch,
    LogSourcePage,
    Operation,
    OperationPage,
    OperationRequest,
    PlanRecord,
    PlanRequest,
    RecoveryRequest,
    ReleasePage,
    RemoteAccessClaimRequest,
    RemoteAccessHeartbeatRequest,
    RemoteAccessLease,
    RemoteAccessReleaseRequest,
    RemoteAccessStartRequest,
    RemoteAccessTicket,
    Resource,
    ResourcePage,
    ServiceError,
)
from piceli.services.query import QueryError, QueryService

if TYPE_CHECKING:
    from piceli.server.cluster_security import ClusterSecurity
    from piceli.services.access import AccessService
    from piceli.services.cluster_build_control import ClusterBuildControl
    from piceli.services.composition_control import CompositionControl
    from piceli.services.environment_control import EnvironmentControl
    from piceli.services.logs import LogService
    from piceli.services.operations import OperationService
    from piceli.services.pipeline_control import PipelineControl
    from piceli.services.remote_access import RemoteAccessService


def _error(code: str, status: int) -> JSONResponse:
    messages = {
        "ui-not-found": "The requested item was not found.",
        "ui-observation-unavailable": "The target could not be fully observed.",
        "ui-request-rejected": "This request is not authorized for this local session.",
        "ui-invalid-request": "The request is invalid or its snapshot has expired.",
        "ui-assets-unavailable": "The packaged UI assets are unavailable.",
        "ui-operation-unavailable": "This operation is unavailable for this application.",
        "ui-plan-stale": "The reviewed plan has expired or its inputs changed. Review a new plan.",
        "ui-approval-mismatch": "The approval does not match the reviewed plan.",
        "ui-idempotency-conflict": "This request key was already used for different inputs.",
        "ui-operation-conflict": "Another operation owns this release.",
        "ui-operation-interrupted": "Execution was interrupted. Inspect its recorded outcome before recovery.",
        "ui-execution-failed": "The deployment did not complete successfully.",
        "ui-evaluation-failed": "Source evaluation or planning did not complete successfully.",
        "ui-state-invalid": "Stored control records could not be verified.",
        "ui-access-port-conflict": "The requested local port is already in use.",
        "ui-access-failed": "A local connection could not be established.",
        "ui-logs-unavailable": "The selected container logs could not be read.",
        "cluster-build-invalid": "The installed build configuration or reviewed plan is invalid. Review a new build plan.",
        "cluster-build-failed": "The cluster build Job did not complete. Inspect its recorded outcome.",
        "ui-controller-absent": "No GitOps controller status is published yet. Enable GitOps for the composition, then refresh.",
        "ui-sync-target-unknown": "The controller's status does not list that environment or component. Refresh and try again.",
        "ui-sync-unavailable": "The controller's request inbox is unavailable. Run cluster init again, then retry.",
    }
    safe_code = code if code in messages else "ui-observation-unavailable"
    return JSONResponse(
        ServiceError(
            code=safe_code,
            message=messages[safe_code],
            retryable=status in {409, 503},
            correlation_id=uuid.uuid4().hex,
        ).model_dump(),
        status_code=status,
    )


def _launch_required() -> HTMLResponse:
    """A page without the local session: say how to open the UI, grant nothing."""
    return HTMLResponse(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        "<title>Piceli</title></head><body><main>"
        "<h1>Open Piceli from its launch address</h1>"
        "<p>This local UI accepts only a browser that opened the address "
        "printed by <code>piceli ui serve</code> when it started (it ends in "
        "<code>?token=</code>). Open that address in this browser once.</p>"
        "</main></body></html>",
        status_code=401,
    )


def _same_origin_reload(target: str) -> HTMLResponse:
    """Navigate again from this origin, so the browser sends its session cookie."""
    escaped = html.escape(target, quote=True)
    return HTMLResponse(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta http-equiv="refresh" content="0;url={escaped}">'
        "<title>Piceli</title></head><body>"
        f'<p><a href="{escaped}">Continue to Piceli</a></p></body></html>'
    )


def create_app(
    service: QueryService,
    *,
    origin: str = "http://127.0.0.1:8000",
    url_prefix: str = "",
    static_dir: Path | None = None,
    operations: OperationService | None = None,
    access: AccessService | None = None,
    remote_access: RemoteAccessService | None = None,
    environment_control: EnvironmentControl | None = None,
    pipeline_control: PipelineControl | None = None,
    cluster_build_control: ClusterBuildControl | None = None,
    composition_control: CompositionControl | None = None,
    logs: LogService | None = None,
    cluster_security: ClusterSecurity | None = None,
    launch_token: str | None = None,
) -> FastAPI:
    """Create a local app. The caller owns the server's loopback listener.

    Opening the launch URL (``app.state.security.launch_url()``, containing a
    secret ``launch_token``) establishes an HttpOnly session cookie; without
    it a page gets no session and no CSRF token. API calls require that
    cookie; all routes reject foreign Host, Origin and Fetch Metadata.
    State-changing routes additionally require Origin and X-Piceli-CSRF.
    """
    if cluster_security is not None:
        from piceli.services.cluster_evaluation import KubernetesJobEvaluator

        if composition_control is not None:
            # The OIDC service keeps its fixed scopes; the forward-mode UI
            # registers environment scopes as the status lists them.
            raise ValueError("composition views require the local launch session")
        if (
            service.scope_policy is None
            or cluster_security.config.origin != origin
            or cluster_security.config.prefix != url_prefix
            or access is not None
            or (remote_access is not None and remote_access.query is not service)
            or (
                environment_control is not None
                and environment_control.query is not service
            )
            or pipeline_control is not None
            or (
                cluster_build_control is not None
                and (
                    operations is None
                    or cluster_build_control.query is not service
                    or cluster_build_control.store is not operations.store
                )
            )
            or (
                operations is not None
                and (
                    operations.query is not service
                    or operations.principal is not None
                    or not isinstance(operations.evaluator, KubernetesJobEvaluator)
                )
            )
        ):
            raise ValueError("cluster service boundary is incomplete")
        cluster_security.bind_scope_policy(
            service.scope_policy, service.registrations.keys()
        )
        security = cluster_security
    elif launch_token is not None:
        if remote_access is not None:
            raise ValueError("remote access requires cluster security")
        security = LocalSecurity(origin, url_prefix, launch_token=launch_token)
    else:
        if remote_access is not None:
            raise ValueError("remote access requires cluster security")
        security = LocalSecurity(origin, url_prefix)
    assets = (static_dir or Path(__file__).parent / "static").resolve()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if operations is not None:
            operations.start()
        if pipeline_control is not None:
            pipeline_control.start()
        if cluster_build_control is not None:
            cluster_build_control.start()
        try:
            yield
        finally:
            if access is not None:
                access.close()
            if remote_access is not None:
                remote_access.close()
            if cluster_build_control is not None:
                cluster_build_control.close()
            if operations is not None:
                operations.close()
            if pipeline_control is not None:
                pipeline_control.close()
            service.close()

    app = FastAPI(
        title="Piceli",
        version="1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.service = service
    app.state.security = security
    if cluster_security is not None:
        app.add_middleware(
            SessionMiddleware,
            secret_key=cluster_security.login_cookie_key,
            session_cookie="piceli_oidc_flow",
            max_age=300,
            same_site="lax",
            https_only=origin.startswith("https:"),
            path=url_prefix or "/",
        )
    api = f"{url_prefix}/api/v1"
    client_path = re.compile(
        re.escape(api) + r"/remote-access/[0-9a-f]{32}/(?:claim|heartbeat|release)"
    )
    event_guard = threading.Lock()
    event_clients = 0

    def secured(response: Response) -> Response:
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'"
        )
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    def with_local_session(response: Response, local: LocalSecurity) -> Response:
        response.set_cookie(
            local.cookie_name,
            local.session,
            httponly=True,
            samesite="strict",
            secure=origin.startswith("https:"),
            path=url_prefix or "/",
        )
        response.set_cookie(
            local.csrf_cookie_name,
            local.csrf,
            httponly=False,
            samesite="strict",
            secure=origin.startswith("https:"),
            path=url_prefix or "/",
        )
        return response

    @app.middleware("http")
    async def local_boundary(request: Request, call_next: Any) -> Response:
        path = request.url.path
        client_request = (
            cluster_security is not None
            and request.method == "POST"
            and client_path.fullmatch(path) is not None
        )
        is_api = (
            path == f"{url_prefix}/api"
            or path.startswith(f"{url_prefix}/api/")
            or (cluster_security is not None and path == f"{url_prefix}/auth/logout")
        )
        local = security if isinstance(security, LocalSecurity) else None
        if local is not None:
            canonical = local.canonical_navigation(request, api=is_api)
            if canonical is not None:
                return RedirectResponse(canonical, status_code=307)
        callback_path = f"{url_prefix}/auth/callback"
        if client_request:
            # The CLI uses a one-time pairing secret or a distinct lease secret.
            # It has no browser cookie; only these exact paths bypass that cookie.
            client_accepted = (
                request.headers.get("host") == urlsplit(origin).netloc
                and request.headers.get("origin", origin) == origin
                and request.headers.get("sec-fetch-site", "none")
                in {"none", "same-origin"}
                and request.headers.get("content-type", "").split(";", 1)[0]
                == "application/json"
            )
        else:
            client_accepted = False
        if client_request and not client_accepted:
            return _error("ui-request-rejected", 403)
        if not client_request and not (
            (cluster_security is not None and path == callback_path)
            or security.accepted(request, api=is_api)
        ):
            return _error("ui-request-rejected", 403)
        page = not is_api and request.method in {"GET", "HEAD"}
        if local is not None and page and "token" in request.query_params:
            # The launch URL: exchange its secret once for this browser's
            # session, then drop it from the address bar and history.
            tokens = request.query_params.getlist("token")
            if len(tokens) != 1 or not local.launch_token_matches(tokens[0]):
                return secured(_error("ui-request-rejected", 403))
            return secured(
                with_local_session(
                    RedirectResponse(local.without_token(request), 303), local
                )
            )
        principal = cluster_security.principal(request) if cluster_security else None
        if (
            cluster_security is not None
            and principal is None
            and not is_api
            and path not in {f"{url_prefix}/auth/login", callback_path}
            and not path.startswith(f"{url_prefix}/assets/")
        ):
            if page and request.headers.get("sec-fetch-site") in {
                "cross-site",
                "same-site",
            }:
                # The browser withheld the SameSite=Strict session cookie on
                # a navigation from another site; reload from this origin
                # before deciding that a login is needed.
                query = f"?{request.url.query}" if request.url.query else ""
                return secured(_same_origin_reload(origin + path + query))
            return RedirectResponse(f"{url_prefix}/auth/login", 303)
        try:
            if principal is None:
                response = await call_next(request)
            else:
                with request_principal(principal):
                    response = await call_next(request)
        except Exception:
            response = (
                _error("ui-request-rejected", 403)
                if cluster_security is not None
                and path in {f"{url_prefix}/auth/login", callback_path}
                else _error("ui-observation-unavailable", 503)
            )
        if (
            local is not None
            and page
            and response.headers.get("content-type", "").startswith("text/html")
        ):
            if local.has_session(request):
                response = with_local_session(response, local)
            elif request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}:
                # A browser withholds the SameSite=Strict session cookie on a
                # navigation from another site; reload from this origin.
                response = _same_origin_reload(local.without_token(request))
            else:
                response = _launch_required()
        return secured(response)

    if cluster_security is not None:

        @app.get(f"{url_prefix}/auth/login", include_in_schema=False)
        async def cluster_login(request: Request) -> RedirectResponse:
            return await cluster_security.login(request)

        @app.get(f"{url_prefix}/auth/callback", include_in_schema=False)
        async def cluster_callback(request: Request) -> Response:
            return await cluster_security.callback(request)

        @app.post(f"{url_prefix}/auth/logout", include_in_schema=False)
        async def cluster_logout(request: Request) -> Response:
            return cluster_security.logout(request)

    @app.exception_handler(QueryError)
    async def query_error(_request: Request, error: QueryError) -> JSONResponse:
        return _error(error.code, error.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        _request: Request, _error_value: RequestValidationError
    ) -> JSONResponse:
        return _error("ui-invalid-request", 422)

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, error: HTTPException) -> JSONResponse:
        return _error("ui-not-found", error.status_code)

    @app.get(f"{api}/capabilities", response_model=Capabilities)
    def capabilities() -> Capabilities:
        result = service.capabilities()
        if (
            environment_control is None
            and pipeline_control is None
            and cluster_build_control is None
            and composition_control is None
        ):
            return result
        actions = dict(result.actions)
        if composition_control is not None:
            visible = service._allowed(composition_control.application_id, "inspect")
            can_sync = service._allowed(composition_control.application_id, "deploy")
            actions["composition"] = Capability(
                allowed=visible, reason=None if visible else "not-authorized"
            )
            actions["composition_sync"] = Capability(
                allowed=visible and can_sync,
                reason=None if visible and can_sync else "not-authorized",
            )
        if cluster_build_control is not None:
            allowed = service._allowed(cluster_build_control.application_id, "deploy")
            actions["cluster_build"] = Capability(
                allowed=allowed, reason=None if allowed else "not-authorized"
            )
        if pipeline_control is not None:
            pipeline_allowed = service._allowed(
                pipeline_control.application_id, "plan"
            ) and service._allowed(pipeline_control.application_id, "deploy")
            actions["pipeline"] = Capability(
                allowed=pipeline_allowed,
                reason=None if pipeline_allowed else "not-authorized",
            )
        if environment_control is None:
            return result.model_copy(update={"actions": actions})
        configured = environment_control.application_id in service.registrations
        visible = configured and service._allowed(
            environment_control.application_id, "inspect"
        )
        editable = configured and service._allowed(
            environment_control.application_id, "deploy"
        )
        actions["environments"] = Capability(
            allowed=visible
            and (
                environment_control.pipeline is not None
                or environment_control.controller_target is not None
                or environment_control.channel_factory is not None
            ),
            reason=None if visible else "environments-not-configured",
        )
        actions["gitops"] = Capability(
            allowed=visible
            and (
                environment_control.controller_target is not None
                or environment_control.channel_factory is not None
            ),
            reason=None if visible else "not-authorized",
        )
        actions["environment_change"] = Capability(
            allowed=editable and environment_control.pipeline is not None,
            reason=None if editable else "not-authorized",
        )
        actions["gitops_change"] = Capability(
            allowed=editable
            and (
                environment_control.controller_target is not None
                or environment_control.channel_factory is not None
            ),
            reason=None if editable else "not-authorized",
        )
        return result.model_copy(update={"actions": actions})

    def pipelines() -> PipelineControl:
        if pipeline_control is None:
            raise QueryError("ui-operation-unavailable", 409)
        return pipeline_control

    def builds() -> ClusterBuildControl:
        if cluster_build_control is None:
            raise QueryError("ui-operation-unavailable", 409)
        return cluster_build_control

    @app.post(f"{api}/cluster-build/plans")
    def cluster_build_plan(body: ClusterBuildPlanRequest) -> dict[str, Any]:
        return builds().plan(body.commit, body.cache_key)

    @app.get(f"{api}/cluster-build/plans/{{plan_id}}")
    def cluster_build_get_plan(plan_id: str) -> dict[str, Any]:
        return builds().get_plan(plan_id)

    @app.post(f"{api}/cluster-build/operations", status_code=202)
    def cluster_build_admit(body: ClusterBuildOperationRequest) -> dict[str, Any]:
        return builds().admit(body.plan_id, body.approved_digest, body.idempotency_key)

    @app.get(f"{api}/cluster-build/operations")
    def cluster_build_operations() -> dict[str, Any]:
        return builds().operations()

    @app.get(f"{api}/cluster-build/operations/{{operation_id}}")
    def cluster_build_operation(operation_id: str) -> dict[str, Any]:
        return builds().operation(operation_id)

    @app.post(f"{api}/pipeline/plans")
    def pipeline_plan() -> dict[str, Any]:
        return pipelines().plan()

    @app.get(f"{api}/pipeline/plans/{{plan_id}}")
    def pipeline_get_plan(plan_id: str) -> dict[str, Any]:
        return pipelines().get_plan(plan_id)

    @app.post(f"{api}/pipeline/operations", status_code=202)
    def pipeline_admit(body: OperationRequest) -> dict[str, Any]:
        return pipelines().admit(
            body.plan_id, body.approved_digest, body.idempotency_key
        )

    @app.get(f"{api}/pipeline/operations")
    def pipeline_operations() -> dict[str, Any]:
        return pipelines().operations()

    @app.get(f"{api}/pipeline/operations/{{operation_id}}")
    def pipeline_operation(operation_id: str) -> dict[str, Any]:
        return pipelines().operation(operation_id)

    @app.post(f"{api}/pipeline/operations/{{operation_id}}/approve", status_code=202)
    def pipeline_approve_second(
        operation_id: str, body: OperationRequest
    ) -> dict[str, Any]:
        return pipelines().approve_second(
            operation_id, body.plan_id, body.approved_digest
        )

    def controls() -> EnvironmentControl:
        if environment_control is None:
            raise QueryError("ui-operation-unavailable", 409)
        return environment_control

    @app.get(f"{api}/environments")
    def environments() -> dict[str, Any]:
        return controls().environments()

    @app.post(f"{api}/environments/actions")
    def environment_action(body: EnvironmentActionRequest) -> dict[str, Any]:
        return controls().environment_action(
            body.verb,
            body.branch,
            approved_hash=body.approved_hash,
            source=body.source,
        )

    @app.get(f"{api}/gitops")
    def gitops() -> dict[str, Any]:
        return controls().gitops_status()

    @app.post(f"{api}/gitops/approvals")
    def gitops_approval(body: GitOpsApprovalRequest) -> dict[str, str]:
        return controls().approve_gitops(body.branch, body.plan_hash)

    @app.post(f"{api}/gitops/promotions")
    def gitops_promotion(body: GitOpsPromotionRequest) -> dict[str, str]:
        return controls().promote(body.branch, body.commit)

    def composition() -> CompositionControl:
        if composition_control is None:
            raise QueryError("ui-operation-unavailable", 409)
        return composition_control

    @app.get(f"{api}/composition")
    def composition_overview() -> dict[str, Any]:
        return composition().overview()

    @app.get(f"{api}/composition/environments/{{env}}")
    def composition_environment(env: str) -> dict[str, Any]:
        return composition().environment(env)

    @app.post(f"{api}/composition/sync", status_code=202)
    def composition_sync(body: CompositionSyncRequest) -> dict[str, Any]:
        return composition().sync(body.env, body.component)

    @app.get(f"{api}/applications", response_model=ApplicationPage)
    def applications(cursor: str | None = None) -> ApplicationPage:
        return service.applications(cursor)

    @app.get(f"{api}/applications/{{application_id}}", response_model=Application)
    def application(application_id: str) -> Application:
        return service.application(application_id)

    @app.get(
        f"{api}/applications/{{application_id}}/resources", response_model=ResourcePage
    )
    def resources(application_id: str, cursor: str | None = None) -> ResourcePage:
        return service.resources(application_id, cursor)

    @app.get(
        f"{api}/applications/{{application_id}}/resources/{{resource_id}}",
        response_model=Resource,
    )
    def resource(application_id: str, resource_id: str) -> Resource:
        return service.resource(application_id, resource_id)

    @app.get(f"{api}/observation-events")
    async def observation_events(
        request: Request, application_id: str, after: str | None = None
    ) -> StreamingResponse:
        """Transient scoped watch notifications; reset after replay loss."""
        nonlocal event_clients
        stream_principal = (
            cluster_security.principal(request) if cluster_security else None
        )
        service.registration(application_id)
        cursor = request.headers.get("last-event-id") or after
        try:
            service.observation.events(application_id, cursor)
        except ValueError:
            raise QueryError("ui-invalid-request", 422) from None
        with event_guard:
            if event_clients >= 32:
                raise QueryError("ui-observation-unavailable", 503)
            event_clients += 1

        async def stream() -> AsyncIterator[str]:
            nonlocal event_clients
            current = cursor
            heartbeat = time.monotonic()
            try:
                while not await request.is_disconnected():
                    if cluster_security is not None:
                        if stream_principal is None or not cluster_security.live(
                            request, stream_principal
                        ):
                            return
                        with request_principal(stream_principal):
                            service.registration(application_id)
                            batch, current, reset = service.observation.events(
                                application_id, current
                            )
                    else:
                        service.registration(application_id)
                        batch, current, reset = service.observation.events(
                            application_id, current
                        )
                    if reset:
                        event = Event(
                            cursor=current, scope=application_id, kind="reset"
                        )
                        yield (
                            f"id: {event.cursor}\nevent: reset\n"
                            f"data: {json.dumps(event.model_dump(), separators=(',', ':'))}\n\n"
                        )
                        return
                    for event in batch:
                        yield (
                            f"id: {event.cursor}\nevent: {event.kind}\n"
                            f"data: {json.dumps(event.model_dump(), separators=(',', ':'))}\n\n"
                        )
                    if time.monotonic() - heartbeat >= 15:
                        yield ": heartbeat\n\n"
                        heartbeat = time.monotonic()
                    await asyncio.sleep(0.25)
            except (QueryError, ValueError):
                return
            finally:
                with event_guard:
                    event_clients -= 1

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get(
        f"{api}/applications/{{application_id}}/resources/{{resource_id}}/log-sources",
        response_model=LogSourcePage,
    )
    def log_sources(
        application_id: str, resource_id: str, resource_uid: str
    ) -> LogSourcePage:
        if logs is None:
            raise QueryError("ui-operation-unavailable", 409)
        return logs.sources(application_id, resource_id, resource_uid)

    @app.get(
        f"{api}/applications/{{application_id}}/resources/{{resource_id}}/logs",
        response_model=LogBatch,
    )
    def read_logs(
        application_id: str,
        resource_id: str,
        resource_uid: str,
        pod_name: str,
        pod_uid: str,
        container: str,
        previous: bool = False,
        tail_lines: int = 2000,
    ) -> LogBatch:
        if logs is None:
            raise QueryError("ui-operation-unavailable", 409)
        return logs.read(
            application_id,
            resource_id,
            resource_uid=resource_uid,
            pod_name=pod_name,
            pod_uid=pod_uid,
            container=container,
            previous=previous,
            tail_lines=tail_lines,
        )

    def delivery() -> OperationService:
        if operations is None:
            raise QueryError("ui-operation-unavailable", 409)
        return operations

    @app.post(
        f"{api}/applications/{{application_id}}/evaluation-preview",
        response_model=EvaluationPreview,
    )
    def evaluation_preview(application_id: str, body: PlanRequest) -> EvaluationPreview:
        return delivery().preview(application_id, body)

    @app.post(
        f"{api}/applications/{{application_id}}/evaluations",
        response_model=Evaluation,
        status_code=202,
    )
    def evaluate(
        application_id: str, body: EvaluationRequest, response: Response
    ) -> Evaluation:
        result = delivery().evaluate(application_id, body)
        response.headers["Location"] = f"{api}/evaluations/{result.id}"
        return result

    @app.get(f"{api}/evaluations/{{evaluation_id}}", response_model=Evaluation)
    def evaluation(evaluation_id: str) -> Evaluation:
        return delivery().evaluation(evaluation_id)

    @app.get(f"{api}/plans/{{plan_id}}", response_model=PlanRecord)
    def plan(plan_id: str) -> PlanRecord:
        return delivery().plan(plan_id)

    @app.post(
        f"{api}/applications/{{application_id}}/operations",
        response_model=Operation,
        status_code=202,
    )
    def admit(
        application_id: str, body: OperationRequest, response: Response
    ) -> Operation:
        result = delivery().admit(application_id, body)
        response.headers["Location"] = f"{api}/operations/{result.id}"
        return result

    @app.get(
        f"{api}/applications/{{application_id}}/operations",
        response_model=OperationPage,
    )
    def operation_list(application_id: str) -> OperationPage:
        return delivery().operations(application_id)

    @app.get(f"{api}/operations/{{operation_id}}", response_model=Operation)
    def operation(operation_id: str) -> Operation:
        return delivery().operation(operation_id)

    @app.post(
        f"{api}/operations/{{operation_id}}/resume",
        response_model=Operation,
        status_code=202,
    )
    def resume(
        operation_id: str, body: RecoveryRequest, response: Response
    ) -> Operation:
        result = delivery().resume(operation_id, body)
        response.headers["Location"] = f"{api}/operations/{result.id}"
        return result

    @app.post(
        f"{api}/operations/{{operation_id}}/cancel",
        response_model=Operation,
        status_code=202,
    )
    def cancel(operation_id: str, body: CancelRequest) -> Operation:
        return delivery().cancel(operation_id, body)

    @app.get(
        f"{api}/applications/{{application_id}}/releases", response_model=ReleasePage
    )
    def releases(application_id: str) -> ReleasePage:
        return delivery().releases(application_id)

    def local_access() -> AccessService:
        if access is None:
            raise QueryError("ui-operation-unavailable", 409)
        return access

    @app.post(
        f"{api}/applications/{{application_id}}/access-sessions",
        response_model=AccessSession,
        status_code=201,
    )
    def start_access(application_id: str, body: AccessStartRequest) -> AccessSession:
        return local_access().start(application_id, body)

    @app.get(
        f"{api}/applications/{{application_id}}/access-sessions",
        response_model=AccessPage,
    )
    def access_sessions(application_id: str) -> AccessPage:
        return local_access().list(application_id)

    @app.get(
        f"{api}/applications/{{application_id}}/access-sessions/{{session_id}}",
        response_model=AccessSession,
    )
    def access_session(application_id: str, session_id: str) -> AccessSession:
        return local_access().get(application_id, session_id)

    @app.delete(
        f"{api}/applications/{{application_id}}/access-sessions/{{session_id}}",
        response_model=AccessSession,
    )
    def stop_access(application_id: str, session_id: str) -> AccessSession:
        return local_access().stop(application_id, session_id)

    def remote_connections() -> RemoteAccessService:
        if remote_access is None:
            raise QueryError("ui-operation-unavailable", 409)
        return remote_access

    @app.post(
        f"{api}/applications/{{application_id}}/remote-access",
        response_model=RemoteAccessTicket,
        status_code=201,
    )
    def issue_remote_access(
        application_id: str, body: RemoteAccessStartRequest
    ) -> RemoteAccessTicket:
        return remote_connections().issue(application_id, body)

    @app.get(
        f"{api}/applications/{{application_id}}/remote-access",
        response_model=AccessPage,
    )
    def list_remote_access(application_id: str) -> AccessPage:
        return remote_connections().list(application_id)

    @app.get(
        f"{api}/applications/{{application_id}}/remote-access/{{ticket_id}}",
        response_model=AccessSession,
    )
    def remote_access_ticket(application_id: str, ticket_id: str) -> AccessSession:
        return remote_connections().get(application_id, ticket_id)

    @app.delete(
        f"{api}/applications/{{application_id}}/remote-access/{{ticket_id}}",
        response_model=AccessSession,
    )
    def stop_remote_access(application_id: str, ticket_id: str) -> AccessSession:
        return remote_connections().stop(application_id, ticket_id)

    @app.post(
        f"{api}/remote-access/{{ticket_id}}/claim", response_model=RemoteAccessLease
    )
    def claim_remote_access(
        ticket_id: str, body: RemoteAccessClaimRequest
    ) -> RemoteAccessLease:
        return remote_connections().claim(ticket_id, body)

    @app.post(
        f"{api}/remote-access/{{ticket_id}}/heartbeat", response_model=AccessSession
    )
    def remote_access_heartbeat(
        ticket_id: str, body: RemoteAccessHeartbeatRequest
    ) -> AccessSession:
        return remote_connections().heartbeat(ticket_id, body)

    @app.post(
        f"{api}/remote-access/{{ticket_id}}/release", response_model=AccessSession
    )
    def release_remote_access(
        ticket_id: str, body: RemoteAccessReleaseRequest
    ) -> AccessSession:
        return remote_connections().release(ticket_id, body)

    @app.get(f"{api}/events")
    async def events(
        request: Request, application_id: str, after: str | None = None
    ) -> StreamingResponse:
        """Scoped replay with a bounded per-client window and explicit reset."""
        nonlocal event_clients
        stream_principal = (
            cluster_security.principal(request) if cluster_security else None
        )
        dispatcher = delivery()
        service.registration(application_id, action="activity")
        cursor = request.headers.get("last-event-id") or after
        if cursor is not None and (
            len(cursor) > 20 or not cursor.isascii() or not cursor.isdecimal()
        ):
            raise QueryError("ui-invalid-request", 422)
        if cursor is not None and int(cursor) > int(
            await asyncio.to_thread(dispatcher.store.cursor)
        ):
            raise QueryError("ui-invalid-request", 422)
        with event_guard:
            if event_clients >= 32:
                raise QueryError("ui-observation-unavailable", 503)
            event_clients += 1

        async def stream() -> AsyncIterator[str]:
            nonlocal event_clients
            current = cursor
            heartbeat = time.monotonic()
            try:
                while not await request.is_disconnected():
                    # Reauthorize on every replay read, not only at connect.
                    if cluster_security is not None:
                        if stream_principal is None or not cluster_security.live(
                            request, stream_principal
                        ):
                            return
                        with request_principal(stream_principal):
                            service.registration(application_id, action="activity")
                    else:
                        service.registration(application_id, action="activity")
                    batch, current, reset = await asyncio.to_thread(
                        dispatcher.store.events, application_id, current
                    )
                    assert current is not None
                    if reset:
                        event = Event(
                            cursor=current,
                            scope=application_id,
                            kind="reset",
                        )
                        yield (
                            f"id: {event.cursor}\nevent: reset\n"
                            f"data: {json.dumps(event.model_dump(), separators=(',', ':'))}\n\n"
                        )
                        return
                    for event in batch:
                        yield (
                            f"id: {event.cursor}\nevent: {event.kind}\n"
                            f"data: {json.dumps(event.model_dump(), separators=(',', ':'))}\n\n"
                        )
                    if time.monotonic() - heartbeat >= 15:
                        yield ": heartbeat\n\n"
                        heartbeat = time.monotonic()
                    await asyncio.sleep(0.5)
            except QueryError:
                # Scope revocation or invalid replay ends this connection.
                return
            finally:
                with event_guard:
                    event_clients -= 1

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"},
        )

    @app.get(f"{api}/openapi.json")
    def schema() -> dict[str, Any]:
        return app.openapi()

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> RedirectResponse:
        return RedirectResponse(f"{url_prefix}/favicon.svg", status_code=307)

    @app.get(
        "/.well-known/appspecific/com.chrome.devtools.json",
        include_in_schema=False,
    )
    def chrome_devtools_probe() -> Response:
        # Chrome checks for an optional DevTools project configuration.
        return Response(status_code=204)

    @app.get(f"{url_prefix}/{{path:path}}", include_in_schema=False)
    def frontend(path: str) -> Response:
        if path == "api" or path.startswith("api/"):
            return _error("ui-not-found", 404)
        candidate = (assets / path).resolve()
        if not candidate.is_relative_to(assets):
            return _error("ui-not-found", 404)
        if candidate.is_file() and candidate.name != "index.html":
            headers = (
                {"Cache-Control": "public, max-age=31536000, immutable"}
                if path.startswith("assets/")
                else {"Cache-Control": "no-store"}
            )
            return FileResponse(candidate, headers=headers)
        # Missing asset files must not become misleading successful HTML.
        if path.startswith("assets/") or (
            Path(path).suffix and candidate.name != "index.html"
        ):
            return _error("ui-not-found", 404)
        index = (assets / "index.html").resolve()
        if not index.is_relative_to(assets) or not index.is_file():
            return _error("ui-assets-unavailable", 503)
        document = index.read_text()
        base = f'<base href="{html.escape(url_prefix + "/", quote=True)}">'
        # Name this server's CSRF cookie: the browser also holds other local
        # UIs' cookies for the same host.
        base += (
            '<meta name="piceli-csrf-cookie" content="'
            f'{html.escape(security.csrf_cookie_name, quote=True)}">'
        )
        if "<head>" in document:
            document = document.replace("<head>", "<head>" + base, 1)
        else:
            document = base + document
        return HTMLResponse(document)

    return app
