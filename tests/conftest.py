import difflib
import json
from typing import Any, Optional

import pytest
from kubernetes.client.exceptions import ApiException

from piceli.k8s.exceptions import api_exceptions


def pytest_assertrepr_compare(op: str, left: Any, right: Any) -> Optional[list[str]]:
    if isinstance(left, dict) and isinstance(right, dict) and op == "==":
        # Serialize the dictionaries to JSON formatted strings, sorted to ensure consistency
        left_str = json.dumps(left, sort_keys=True, indent=2).splitlines(keepends=True)
        right_str = json.dumps(right, sort_keys=True, indent=2).splitlines(
            keepends=True
        )
        # Generate the unified diff
        diff = list(
            difflib.unified_diff(left_str, right_str, fromfile="left", tofile="right")
        )
        if diff:
            return [
                "Dictionaries do not match:",
                *diff,
                # "left:",
                # *left_str,
                # "right:",
                # *right_str,
            ]
    return None


RESOURCE_JSON = '{"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "tasker-scheduler"}, "spec": {"template": {"metadata": {"name": "tasker-scheduler"}, "spec": {"containers": [{"command": ["sh", "-c", "echo \'scheduler\' && sleep 30"], "image": "busybox", "name": "tasker-scheduler"}], "restartPolicy": "Never"}}}}'


@pytest.fixture
def resource_dict() -> dict:
    """Parse JSON string to a Python dictionary."""
    return json.loads(RESOURCE_JSON)


@pytest.fixture
def not_found_api_op_exception() -> api_exceptions.ApiOperationException:
    api = ApiException()
    api.status = 404
    api.reason = "NotFound"
    api.message = "Service not found"
    api.body = '{"kind":"Status","apiVersion":"v1","metadata":{},"status":"Failure","message":"cronjobs.batch \\"cronjob-name\\" not found","reason":"NotFound","details":{"name":"cronjob-name","group":"batch","kind":"cronjobs"},"code":404}\n'

    return api_exceptions.ApiOperationException.from_api_exception(api)


@pytest.fixture
def todo() -> api_exceptions.ApiOperationException:
    api = ApiException()
    api.status = 404
    api.reason = "NotFound"
    api.message = "Service not found"
    api.body = '{"kind":"Status","apiVersion":"v1","metadata":{},"status":"Failure","message":"cronjobs.batch \\"cronjob-name\\" not found","reason":"NotFound","details":{"name":"cronjob-name","group":"batch","kind":"cronjobs"},"code":404}\n'

    return api_exceptions.ApiOperationException.from_api_exception(api)
