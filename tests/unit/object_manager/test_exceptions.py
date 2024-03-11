import json

from kubernetes.client.exceptions import ApiException

from piceli.k8s.exceptions import api_exceptions


def test_api_exception_parser() -> None:
    api = ApiException()
    api.status = 404
    api.reason = "NotFound"
    api.message = "Service not found"
    api.body = '{"kind":"Status","apiVersion":"v1","metadata":{},"status":"Failure","message":"cronjobs.batch \\"cronjob-name\\" not found","reason":"NotFound","details":{"name":"cronjob-name","group":"batch","kind":"cronjobs"},"code":404}\n'

    parsed = api_exceptions.ApiOperationException.from_api_exception(api)
    assert parsed.code == 404
    assert parsed.status == "Failure"
    assert parsed.reason == "NotFound"
    assert parsed.message == 'cronjobs.batch "cronjob-name" not found'
    assert parsed.not_found is True
    assert parsed.already_exists is False


def test_forbidden_pvc_api_exception() -> None:
    body = {
        "kind": "Status",
        "apiVersion": "v1",
        "metadata": {},
        "status": "Failure",
        "message": (
            'PersistentVolumeClaim "example-persistentvolumeclaim" is invalid: spec: Forbidden: '
            "spec is immutable after creation except resources.requests for bound claims\n"
            "core.PersistentVolumeClaimSpec{\n"
            '\tAccessModes: {"ReadWriteOnce"},\n'
            "\tSelector:    nil,\n"
            "\tResources: core.ResourceRequirements{\n"
            "\t\tLimits: nil,\n"
            "- \t\tRequests: core.ResourceList{\n"
            '- \t\t\ts"storage": {\n'
            "- \t\t\t\ti:      resource.int64Amount{value: 107374182400, scale: -3},\n"
            '- \t\t\t\ts:      "107374182400m",\n'
            '- \t\t\t\tFormat: "DecimalSI",\n'
            "- \t\t\t},\n"
            "- \t\t},\n"
            '+ \t\tRequests: core.ResourceList{s"storage": {d: s"214748364.800", Format: "BinarySI"}},\n'
            "\t\tClaims:   nil,\n"
            "\t},\n"
            '\tVolumeName:       "",\n'
            '\tStorageClassName: &"standard-rwo",\n'
            "\t... // 3 identical fields\n"
            "}\n"
        ),
        "reason": "Invalid",
        "details": {
            "name": "example-persistentvolumeclaim",
            "kind": "PersistentVolumeClaim",
            "causes": [
                {
                    "reason": "FieldValueForbidden",
                    "message": (
                        "Forbidden: spec is immutable after creation except resources.requests for bound claims\n"
                        "core.PersistentVolumeClaimSpec{\n"
                        '\tAccessModes: {"ReadWriteOnce"},\n'
                        "\tSelector:    nil,\n"
                        "\tResources: core.ResourceRequirements{\n"
                        "\t\tLimits: nil,\n"
                        "- \t\tRequests: core.ResourceList{\n"
                        '- \t\t\ts"storage": {\n'
                        "- \t\t\t\ti:      resource.int64Amount{value: 107374182400, scale: -3},\n"
                        '- \t\t\t\ts:      "107374182400m",\n'
                        '- \t\t\t\tFormat: "DecimalSI",\n'
                        "- \t\t\t},\n"
                        "- \t\t},\n"
                        '+ \t\tRequests: core.ResourceList{s"storage": {d: s"214748364.800", Format: "BinarySI"}},\n'
                        "\t\tClaims:   nil,\n"
                        "\t},\n"
                        '\tVolumeName:       "",\n'
                        '\tStorageClassName: &"standard-rwo",\n'
                        "\t... // 3 identical fields\n"
                        "}\n"
                    ),
                    "field": "spec",
                }
            ],
        },
        "code": 422,
    }

    # Create the ApiException instance
    api = ApiException(status=422)
    api.reason = "Unprocessable Entity"
    api.body = json.dumps(body)
    api.headers = {
        "Cache-Control": "no-cache, private",
        "Content-Type": "application/json",
        "Date": "Fri, 01 Mar 2024 14:12:39 GMT",
        "Content-Length": "1770",
    }

    parsed = api_exceptions.ApiOperationException.from_api_exception(api)

    assert parsed.code == 422
    assert parsed.status == "Failure"
    assert parsed.reason == "Invalid"
    assert (
        "spec is immutable after creation except resources.requests for bound claims"
        in parsed.message
    )
    assert parsed.not_found is False
    assert parsed.already_exists is False
    assert parsed.is_immutable_field_error is True
    assert parsed.immutable_fields() == ["spec"]
