"""The safety rules of a client bundle and the annotation that waives one.

Kept apart from :mod:`piceli.bundle.safety` so the App model can name them
without importing the gate. Importing this module is side-effect free.
"""

from __future__ import annotations

from types import MappingProxyType

#: ``safety.piceli.io/allow-<rule>: <reason>`` waives ``rule`` for one object.
SAFETY_ANNOTATION_PREFIX = "safety.piceli.io/allow-"

#: Rule → what it requires (the order is the order of the checks).
RULES = MappingProxyType(
    {
        "non-root": "every container runs as a non-root user",
        "read-only-root": "every container has a read-only root filesystem",
        "no-privilege": "no privileged container, privilege escalation, host "
        "namespace or node directory (hostPath)",
        "resources": "every container requests and limits cpu and memory",
        "network": "no NetworkPolicy admits ingress from outside the namespace",
        "rbac": "least-privilege RBAC: no wildcard, no escalate/bind/impersonate, "
        "cluster-wide roles read-only and without Secrets, bindings only to "
        "roles of the bundle",
        "node-port": "no NodePort or LoadBalancer Service and no hostPort",
    }
)

#: Rule → the registered error code a violation is refused with.
CODES = MappingProxyType({rule: f"bundle-unsafe-{rule}" for rule in RULES})
