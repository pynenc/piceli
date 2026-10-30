"""Small shims for differences between Kubernetes client versions.

pyproject allows ``kubernetes>=29``; the nightly ``kubernetes-client-floor``
job runs the suite on that floor. Version 36.0 renamed the bearer token key
and added ``_content_type`` to the generated API methods.
"""

from __future__ import annotations

from typing import Any

MERGE_PATCH = "application/merge-patch+json"


def set_bearer_token(configuration: Any, token: str | None) -> None:
    """Set (or clear, with ``None``) the bearer token a client sends.

    Clients from 36.0 read ``api_key["BearerToken"]``; older ones (29 to 35)
    read ``api_key["authorization"]``. Both keys hold the same value.
    """
    for key in ("BearerToken", "authorization"):
        if token is None:
            configuration.api_key.pop(key, None)
        else:
            configuration.api_key[key] = "Bearer " + token


class _MergePatchClient:
    """An API client that picks merge-patch as the request content type."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def select_header_content_type(self, content_types: Any) -> str:
        return MERGE_PATCH


def merge_patch(method: Any, *args: Any, **kwargs: Any) -> Any:
    """Call a generated ``patch_*`` method as a JSON merge patch.

    Clients from 36.0 take ``_content_type``. Older ones pick the first type
    the method lists (a strategic merge patch for a dict body), so the call
    goes through a copy of the API whose client always picks merge-patch.
    The shared client is not changed.
    """
    from kubernetes.client.exceptions import ApiTypeError

    try:
        return method(*args, _content_type=MERGE_PATCH, **kwargs)
    except ApiTypeError as error:
        if "_content_type" not in str(error):
            raise
    api = method.__self__
    patched = type(api)(_MergePatchClient(api.api_client))
    return getattr(patched, method.__name__)(*args, **kwargs)
