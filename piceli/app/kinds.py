"""Typed kinds of an :class:`~piceli.app.App` beyond Deployments and Services.

Pod-bearing kinds (:class:`StatefulSet`, :class:`DaemonSet`, :class:`Job`,
:class:`CronJob`) share the pod settings of
:class:`~piceli.app.model.Workload`, so ``pod_defaults``, ``service_account=``,
node pins, images, secrets and volumes work as they do for a Deployment.
:class:`Autoscaler`, :class:`DisruptionBudget`, :class:`Ingress` and
:class:`HttpRoute` point at declared workloads and Services.

Declare them with ``app.stateful_set(...)``, ``app.daemon_set(...)``,
``app.job(...)``, ``app.cron_job(...)``, ``app.autoscaler(...)``,
``app.disruption_budget(...)``, ``app.ingress(...)`` and
``app.http_route(...)``. Every class is a frozen pydantic model, and importing
this module has no side effects.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Annotated, Any, ClassVar, Literal

from pydantic import (
    AfterValidator,
    Field,
    NonNegativeInt,
    PositiveInt,
    model_validator,
)

from piceli.app.model import (
    Labels,
    Name,
    ObjectName,
    PodDefaults,
    PortName,
    PortNumber,
    Service,
    Workload,
    _compact,
    _Model,
)

# ------------------------------------------------------------------- workloads

#: Retention of the claims a StatefulSet creates: never deleted with it.
CLAIM_RETENTION = {"whenDeleted": "Retain", "whenScaled": "Retain"}


class StatefulSet(Workload):
    """A StatefulSet declared with ``app.stateful_set(...)``.

    Pods have stable names (``<name>-0``, ``<name>-1`` …), a DNS name through
    the governing headless Service, and each gets its own claims from the
    :class:`~piceli.app.model.ClaimTemplate` volumes it mounts.

    Pod fields are those of :class:`~piceli.app.model.Workload`; in addition:

    :param replicas: Desired pods. Not rendered when an autoscaler targets
        the StatefulSet (``app.autoscaler``), which then owns the count.
    :param service_name: ``serviceName``: the governing (headless) Service.
    :param pod_management: ``OrderedReady`` (one pod at a time, in order; the
        Kubernetes default) or ``Parallel``.
    :param update_strategy: ``RollingUpdate`` (default) or ``OnDelete``.
    :param min_ready_seconds: ``minReadySeconds``.

    Immutable once created: ``service_name``, ``pod_management``, the
    selector and the claim templates. Changing one is refused at plan time
    (``immutable-field-changed``) until the StatefulSet is named with
    ``--replace StatefulSet/<name>``, which recreates it with ``Orphan``
    propagation (pods are adopted by the new StatefulSet, claims are kept).
    """

    kind: ClassVar[str] = "StatefulSet"
    label: ClassVar[str] = "stateful set"

    replicas: NonNegativeInt = 1
    service_name: Name | None = None
    pod_management: Literal["OrderedReady", "Parallel"] | None = None
    update_strategy: Literal["RollingUpdate", "OnDelete"] | None = None
    min_ready_seconds: NonNegativeInt | None = None

    @classmethod
    def accepts_claim_templates(cls) -> bool:
        return True

    def manifest(
        self,
        namespace: str,
        app_labels: Mapping[str, str],
        node_name: str | None,
        defaults: PodDefaults | None = None,
        automount_token: bool | None = None,
        *,
        scaled: bool = False,
    ) -> dict[str, Any]:
        labels = self.pod_labels(app_labels)
        pod = self.pod_spec(node_name, defaults, automount_token)
        claims = [item.template() for item in self.claim_templates().values()]
        spec = _compact(
            replicas=None if scaled else self.replicas,
            serviceName=self.service_name,
            podManagementPolicy=self.pod_management,
            updateStrategy=(
                {"type": self.update_strategy} if self.update_strategy else None
            ),
            minReadySeconds=self.min_ready_seconds,
            selector={"matchLabels": self.selector_labels},
            template={"metadata": {"labels": labels}, "spec": pod},
            volumeClaimTemplates=claims or None,
            persistentVolumeClaimRetentionPolicy=(
                dict(CLAIM_RETENTION) if claims else None
            ),
        )
        return {
            "apiVersion": "apps/v1",
            "kind": "StatefulSet",
            "metadata": {"name": self.name, "namespace": namespace, "labels": labels},
            "spec": spec,
        }


class DaemonSet(Workload):
    """A DaemonSet declared with ``app.daemon_set(...)``: one pod per matching node.

    Pod fields are those of :class:`~piceli.app.model.Workload` (``node_selector``
    and ``pod_defaults.node_selector`` choose the nodes); in addition:

    :param update_strategy: ``RollingUpdate`` (default) or ``OnDelete``.
    :param min_ready_seconds: ``minReadySeconds``.

    A release waits until every scheduled pod is ready and updated.
    """

    kind: ClassVar[str] = "DaemonSet"
    label: ClassVar[str] = "daemon set"

    update_strategy: Literal["RollingUpdate", "OnDelete"] | None = None
    min_ready_seconds: NonNegativeInt | None = None

    def manifest(
        self,
        namespace: str,
        app_labels: Mapping[str, str],
        node_name: str | None,
        defaults: PodDefaults | None = None,
        automount_token: bool | None = None,
        *,
        scaled: bool = False,
    ) -> dict[str, Any]:
        if scaled:
            raise ValueError("a DaemonSet cannot be autoscaled")
        labels = self.pod_labels(app_labels)
        pod = self.pod_spec(node_name, defaults, automount_token)
        spec = _compact(
            updateStrategy=(
                {"type": self.update_strategy} if self.update_strategy else None
            ),
            minReadySeconds=self.min_ready_seconds,
            selector={"matchLabels": self.selector_labels},
            template={"metadata": {"labels": labels}, "spec": pod},
        )
        return {
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "metadata": {"name": self.name, "namespace": namespace, "labels": labels},
            "spec": spec,
        }


class _Run(Workload):
    """Job settings shared by :class:`Job` and :class:`CronJob`."""

    restart_policy: Literal["Never", "OnFailure"] = "Never"
    backoff_limit: NonNegativeInt | None = None
    completions: PositiveInt | None = None
    parallelism: NonNegativeInt | None = None
    active_deadline_seconds: PositiveInt | None = None
    ttl_seconds_after_finished: NonNegativeInt | None = None

    @model_validator(mode="after")
    def _no_selector(self) -> _Run:
        if self.selector is not None:
            raise ValueError(
                f"{self.label} {self.name!r}: Kubernetes chooses a Job's selector; "
                "pass labels= to add pod labels"
            )
        return self

    def job_spec(
        self,
        app_labels: Mapping[str, str],
        node_name: str | None,
        defaults: PodDefaults | None,
        automount_token: bool | None,
    ) -> dict[str, Any]:
        labels = self.pod_labels(app_labels)
        pod = self.pod_spec(
            node_name,
            defaults,
            automount_token,
            restart_policy=self.restart_policy,
        )
        return _compact(
            backoffLimit=self.backoff_limit,
            completions=self.completions,
            parallelism=self.parallelism,
            activeDeadlineSeconds=self.active_deadline_seconds,
            ttlSecondsAfterFinished=self.ttl_seconds_after_finished,
            template={"metadata": {"labels": labels}, "spec": pod},
        )


class Job(_Run):
    """A Job declared with ``app.job(...)``: pods that run to completion.

    Pod fields are those of :class:`~piceli.app.model.Workload` (without
    ``selector``: Kubernetes chooses a Job's selector); in addition:

    :param restart_policy: ``Never`` (default: a failed pod is replaced) or
        ``OnFailure`` (the container restarts in place).
    :param backoff_limit: Retries before the Job fails.
    :param completions: Successful pods needed.
    :param parallelism: Pods running at once.
    :param active_deadline_seconds: Time limit of the whole Job.
    :param ttl_seconds_after_finished: Delete the Job this long after it
        finishes. The next release then creates (and runs) it again.

    A release waits until the Job completes. A Job's pod template and
    ``completions`` are immutable: changing them is refused at plan time
    (``immutable-field-changed``) until the Job is named with
    ``--replace Job/<name>``, which deletes it (and its pods) and runs the new
    one. To run a Job before a Deployment starts, ``app.depends(deployment,
    on=job)``.
    """

    kind: ClassVar[str] = "Job"
    label: ClassVar[str] = "job"

    def manifest(
        self,
        namespace: str,
        app_labels: Mapping[str, str],
        node_name: str | None,
        defaults: PodDefaults | None = None,
        automount_token: bool | None = None,
        *,
        scaled: bool = False,
    ) -> dict[str, Any]:
        if scaled:
            raise ValueError("a Job cannot be autoscaled")
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": self.name,
                "namespace": namespace,
                "labels": self.pod_labels(app_labels),
            },
            "spec": self.job_spec(app_labels, node_name, defaults, automount_token),
        }


_CRON_FIELD = r"[-0-9*/,?A-Za-z]+"
_CRON = re.compile(
    rf"(@(yearly|annually|monthly|weekly|daily|midnight|hourly))|({_CRON_FIELD}( {_CRON_FIELD}){{4}})"
)


def _schedule(value: str) -> str:
    if not _CRON.fullmatch(value):
        raise ValueError(
            f"schedule {value!r}: use five cron fields ('*/5 * * * *') or a macro "
            "such as '@hourly'"
        )
    return value


class CronJob(_Run):
    """A CronJob declared with ``app.cron_job(...)``: a Job on a schedule.

    Job and pod fields are those of :class:`Job`; in addition:

    :param schedule: Cron schedule (five fields, or ``@hourly`` …).
    :param time_zone: ``timeZone``, such as ``"Etc/UTC"``.
    :param concurrency: ``Allow``, ``Forbid`` or ``Replace`` a running Job.
    :param suspend: Stop scheduling new Jobs.
    :param starting_deadline_seconds: Skip a run that could not start in time.
    :param successful_jobs_history: Finished Jobs to keep.
    :param failed_jobs_history: Failed Jobs to keep.

    A CronJob is ready as soon as it exists; its Jobs are created by the
    controller, belong to it and are never managed or pruned by a release.
    Every field may change: new Jobs use the new template.
    """

    kind: ClassVar[str] = "CronJob"
    label: ClassVar[str] = "cron job"

    schedule: Annotated[str, AfterValidator(_schedule)]
    time_zone: str | None = Field(
        default=None, min_length=1, pattern=r"^[A-Za-z0-9_+/-]+$"
    )
    concurrency: Literal["Allow", "Forbid", "Replace"] | None = None
    suspend: bool | None = None
    starting_deadline_seconds: PositiveInt | None = None
    successful_jobs_history: NonNegativeInt | None = None
    failed_jobs_history: NonNegativeInt | None = None

    def manifest(
        self,
        namespace: str,
        app_labels: Mapping[str, str],
        node_name: str | None,
        defaults: PodDefaults | None = None,
        automount_token: bool | None = None,
        *,
        scaled: bool = False,
    ) -> dict[str, Any]:
        if scaled:
            raise ValueError("a CronJob cannot be autoscaled")
        labels = self.pod_labels(app_labels)
        job = self.job_spec(app_labels, node_name, defaults, automount_token)
        return {
            "apiVersion": "batch/v1",
            "kind": "CronJob",
            "metadata": {"name": self.name, "namespace": namespace, "labels": labels},
            "spec": _compact(
                schedule=self.schedule,
                timeZone=self.time_zone,
                concurrencyPolicy=self.concurrency,
                suspend=self.suspend,
                startingDeadlineSeconds=self.starting_deadline_seconds,
                successfulJobsHistoryLimit=self.successful_jobs_history,
                failedJobsHistoryLimit=self.failed_jobs_history,
                jobTemplate={"metadata": {"labels": labels}, "spec": job},
            ),
        }


# ---------------------------------------------------------------- autoscaling


class Autoscaler(_Model):
    """A HorizontalPodAutoscaler (``autoscaling/v2``), declared with ``app.autoscaler``.

    :param name: HPA name; defaults to the workload's name.
    :param target_kind: ``Deployment`` or ``StatefulSet``.
    :param target: The workload's name.
    :param min_replicas: Lowest replica count.
    :param max_replicas: Highest replica count.
    :param cpu: Target average CPU utilization, in percent of the requests.
    :param memory: Target average memory utilization, in percent of the requests.
    :param scale_down_stabilization_seconds: How long a lower recommendation
        must hold before scaling down (``behavior.scaleDown``).
    :param component: The workload's component.

    Replicas rule: the targeted workload renders **no** ``spec.replicas``, so
    the HPA alone sets it and a release never resets it. A plan also never
    removes ``spec.replicas`` from a workload that an HPA of the same
    composition targets.
    """

    name: Name
    target_kind: Literal["Deployment", "StatefulSet"]
    target: Name
    min_replicas: PositiveInt = 1
    max_replicas: PositiveInt
    cpu: int | None = Field(default=None, ge=1, le=1000)
    memory: int | None = Field(default=None, ge=1, le=1000)
    scale_down_stabilization_seconds: int | None = Field(default=None, ge=0, le=3600)
    component: Name

    @model_validator(mode="after")
    def _shape(self) -> Autoscaler:
        if self.min_replicas > self.max_replicas:
            raise ValueError("min_replicas cannot be above max_replicas")
        if self.cpu is None and self.memory is None:
            raise ValueError("an autoscaler needs cpu= and/or memory= (percent)")
        return self

    @property
    def component_name(self) -> str:
        return self.component

    def manifest(self, namespace: str, labels: Mapping[str, str]) -> dict[str, Any]:
        metrics = [
            {
                "type": "Resource",
                "resource": {
                    "name": resource,
                    "target": {"type": "Utilization", "averageUtilization": value},
                },
            }
            for resource, value in (("cpu", self.cpu), ("memory", self.memory))
            if value is not None
        ]
        behavior = (
            {
                "scaleDown": {
                    "stabilizationWindowSeconds": self.scale_down_stabilization_seconds
                }
            }
            if self.scale_down_stabilization_seconds is not None
            else None
        )
        return {
            "apiVersion": "autoscaling/v2",
            "kind": "HorizontalPodAutoscaler",
            "metadata": _compact(
                name=self.name, namespace=namespace, labels=dict(labels) or None
            ),
            "spec": _compact(
                scaleTargetRef={
                    "apiVersion": "apps/v1",
                    "kind": self.target_kind,
                    "name": self.target,
                },
                minReplicas=self.min_replicas,
                maxReplicas=self.max_replicas,
                metrics=metrics,
                behavior=behavior,
            ),
        }


_PERCENT = re.compile(r"(100|[1-9]?[0-9])%")


def _budget(value: int | str | None) -> int | str | None:
    if isinstance(value, str) and not _PERCENT.fullmatch(value):
        raise ValueError(f"{value!r}: use a pod count or a percentage such as '50%'")
    return value


Budget = Annotated[NonNegativeInt | str | None, AfterValidator(_budget)]


class DisruptionBudget(_Model):
    """A PodDisruptionBudget (``policy/v1``), declared with ``app.disruption_budget``.

    Limits voluntary disruptions (node drains, evictions) of a workload's pods.

    :param name: PDB name; defaults to the workload's name.
    :param selector: The workload's selector labels.
    :param min_available: Pods (or percentage) that must stay available.
    :param max_unavailable: Pods (or percentage) that may be unavailable.
    :param unhealthy_pod_eviction: ``IfHealthyBudget`` or ``AlwaysAllow``
        (``unhealthyPodEvictionPolicy``).
    :param component: The workload's component.

    Invariant: exactly one of ``min_available`` and ``max_unavailable``.
    """

    name: Name
    selector: Labels = Field(min_length=1)
    min_available: Budget = None
    max_unavailable: Budget = None
    unhealthy_pod_eviction: Literal["IfHealthyBudget", "AlwaysAllow"] | None = None
    component: Name

    @model_validator(mode="after")
    def _one(self) -> DisruptionBudget:
        if (self.min_available is None) == (self.max_unavailable is None):
            raise ValueError("pass exactly one of min_available= and max_unavailable=")
        return self

    @property
    def component_name(self) -> str:
        return self.component

    def manifest(self, namespace: str, labels: Mapping[str, str]) -> dict[str, Any]:
        return {
            "apiVersion": "policy/v1",
            "kind": "PodDisruptionBudget",
            "metadata": _compact(
                name=self.name, namespace=namespace, labels=dict(labels) or None
            ),
            "spec": _compact(
                minAvailable=self.min_available,
                maxUnavailable=self.max_unavailable,
                unhealthyPodEvictionPolicy=self.unhealthy_pod_eviction,
                selector={"matchLabels": dict(self.selector)},
            ),
        }


# ----------------------------------------------------------------- networking

_HOST = re.compile(
    r"(\*\.)?[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)+"
)


def _host(value: str) -> str:
    if len(value) > 253 or not _HOST.fullmatch(value):
        raise ValueError(
            f"host {value!r}: use a lower-case DNS name such as 'shop.example.com' "
            "(a leading '*.' is allowed)"
        )
    return value


Host = Annotated[str, AfterValidator(_host)]


class Route(_Model):
    """One HTTP path to a Service port, for ``app.ingress`` and ``app.http_route``.

    Build it with a Service handle (``Route(api_service, "/api")``), which
    fills the port when the Service has one, or with the name of a Service
    the app does not declare (then ``port`` is required).

    :param service: A :class:`~piceli.app.model.Service` or a Service name.
    :param path: Path to match, starting with ``/``.
    :param port: Service port number (or name, Ingress only).
    :param match: ``Prefix`` (default) or ``Exact``.
    """

    service: Name
    path: str = Field(default="/", pattern=r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*$")
    port: PortNumber | PortName
    match: Literal["Prefix", "Exact"] = "Prefix"

    def __init__(
        self,
        service: Service | str,
        path: str = "/",
        /,
        *,
        port: int | str | None = None,
        match: Literal["Prefix", "Exact"] = "Prefix",
    ) -> None:
        if isinstance(service, Service):
            if port is None:
                if len(service.ports) != 1:
                    raise ValueError(
                        f"service {service.name!r} has {len(service.ports)} ports; "
                        "pass port="
                    )
                port = service.ports[0].port
            elif isinstance(port, str):
                named = [item.port for item in service.ports if item.name == port]
                if not named:
                    raise ValueError(
                        f"service {service.name!r} has no port named {port!r}"
                    )
                port = named[0]
            elif port not in {item.port for item in service.ports}:
                raise ValueError(f"service {service.name!r} has no port {port}")
            name = service.name
        else:
            if port is None:
                raise ValueError(
                    f"route to service {service!r}: pass port= (the Service is not "
                    "a declared handle)"
                )
            name = service
        super().__init__(
            **{"service": name, "path": path, "port": port, "match": match}
        )


class Ingress(_Model):
    """An Ingress (``networking.k8s.io/v1``), declared with ``app.ingress``.

    Every host serves every route; with no hosts, the routes match any host.

    :param name: Ingress name.
    :param routes: Paths to Service ports (:class:`Route`).
    :param hosts: Host names (a leading ``*.`` is a wildcard).
    :param class_name: ``ingressClassName``; the cluster default when unset.
    :param tls_secret: A TLS Secret (``kubernetes.io/tls``) for all ``hosts``.
    :param component: Deployment component.

    Rendering needs no controller; traffic flows once an ingress controller
    serves the class.
    """

    name: Name
    routes: tuple[Route, ...] = Field(min_length=1)
    hosts: tuple[Host, ...] = ()
    class_name: ObjectName | None = None
    tls_secret: ObjectName | None = None
    component: Name

    @model_validator(mode="after")
    def _tls(self) -> Ingress:
        if self.tls_secret is not None and not self.hosts:
            raise ValueError("tls_secret= needs hosts=")
        if len(set(self.hosts)) != len(self.hosts):
            raise ValueError("hosts must be unique")
        return self

    @property
    def component_name(self) -> str:
        return self.component

    def manifest(self, namespace: str, labels: Mapping[str, str]) -> dict[str, Any]:
        paths = [
            {
                "path": route.path,
                "pathType": route.match,
                "backend": {
                    "service": {
                        "name": route.service,
                        "port": {"name": route.port}
                        if isinstance(route.port, str)
                        else {"number": route.port},
                    }
                },
            }
            for route in self.routes
        ]
        rules = (
            [{"host": host, "http": {"paths": paths}} for host in self.hosts]
            if self.hosts
            else [{"http": {"paths": paths}}]
        )
        return {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "Ingress",
            "metadata": _compact(
                name=self.name, namespace=namespace, labels=dict(labels) or None
            ),
            "spec": _compact(
                ingressClassName=self.class_name,
                tls=(
                    [{"hosts": list(self.hosts), "secretName": self.tls_secret}]
                    if self.tls_secret
                    else None
                ),
                rules=rules,
            ),
        }


GATEWAY_API = "gateway.networking.k8s.io"


class GatewayRef(_Model):
    """A Gateway an :class:`HttpRoute` attaches to (``parentRefs``).

    :param name: Gateway name.
    :param namespace: Gateway namespace; the route's namespace when unset.
    :param section: A listener name (``sectionName``) of the Gateway.
    """

    name: ObjectName
    namespace: Name | None = None
    section: Name | None = None

    def __init__(self, name: str, /, **data: Any) -> None:
        super().__init__(**{"name": name, **data})

    def manifest(self) -> dict[str, Any]:
        return _compact(
            group=GATEWAY_API,
            kind="Gateway",
            name=self.name,
            namespace=self.namespace,
            sectionName=self.section,
        )


class HttpRoute(_Model):
    """A Gateway API HTTPRoute (``gateway.networking.k8s.io/v1``), from ``app.http_route``.

    One rule per :class:`Route`, attached to one or more Gateways.

    :param name: HTTPRoute name.
    :param gateways: Parent Gateways (:class:`GatewayRef`).
    :param routes: Paths to Service ports; ports are numbers.
    :param hosts: ``hostnames``; any host when empty.
    :param component: Deployment component.

    The Gateway API CRDs must be installed in the cluster (they are not part
    of Kubernetes itself); without them the plan is refused because the kind
    cannot be discovered. Traffic flows once a Gateway controller accepts
    the route; a release only waits until the object exists.
    """

    name: Name
    gateways: tuple[GatewayRef, ...] = Field(min_length=1)
    routes: tuple[Route, ...] = Field(min_length=1)
    hosts: tuple[Host, ...] = ()
    component: Name

    @model_validator(mode="after")
    def _ports(self) -> HttpRoute:
        for route in self.routes:
            if isinstance(route.port, str):
                raise ValueError(
                    f"route to service {route.service!r}: an HTTPRoute backend "
                    "needs a port number"
                )
        return self

    @property
    def component_name(self) -> str:
        return self.component

    def manifest(self, namespace: str, labels: Mapping[str, str]) -> dict[str, Any]:
        rules = [
            {
                "matches": [
                    {
                        "path": {
                            "type": "PathPrefix"
                            if route.match == "Prefix"
                            else "Exact",
                            "value": route.path,
                        }
                    }
                ],
                "backendRefs": [{"name": route.service, "port": route.port}],
            }
            for route in self.routes
        ]
        return {
            "apiVersion": f"{GATEWAY_API}/v1",
            "kind": "HTTPRoute",
            "metadata": _compact(
                name=self.name, namespace=namespace, labels=dict(labels) or None
            ),
            "spec": _compact(
                parentRefs=[gateway.manifest() for gateway in self.gateways],
                hostnames=list(self.hosts) or None,
                rules=rules,
            ),
        }
