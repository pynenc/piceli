from copy import deepcopy

from piceli.k8s.ops.compare import object_comparer


def test_deploying_image_difference() -> None:
    actual_spec: dict = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "example-deployment"},
        "spec": {
            "replicas": 2,
            "selector": {"matchLabels": {"app": "example"}},
            "template": {
                "metadata": {"labels": {"app": "example"}},
                "spec": {
                    "containers": [
                        {
                            "name": "example-container",
                            "image": "bash:4.4",
                            "command": ["sh", "-c", "while true; do sleep 60; done;"],
                            "env": [
                                {
                                    "name": "EXAMPLE_USERNAME",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": "example-secret",
                                            "key": "username",
                                        }
                                    },
                                },
                                {
                                    "name": "EXAMPLE_PASSWORD",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": "example-secret",
                                            "key": "password",
                                        }
                                    },
                                },
                                {
                                    "name": "EXAMPLE_CONFIG",
                                    "valueFrom": {
                                        "configMapKeyRef": {
                                            "name": "example-configmap",
                                            "key": "config.json",
                                        }
                                    },
                                },
                            ],
                        }
                    ]
                },
            },
        },
    }
    desired_spec: dict = deepcopy(actual_spec)
    desired_spec["spec"]["template"]["spec"]["containers"][0]["image"] = "ubuntu:latest"
    compared_result = object_comparer._determine_update_action(
        actual_spec["kind"], desired_spec, actual_spec
    )
    expected_difference = object_comparer.PathComparison(
        path=object_comparer.Path.from_string(
            "spec,template,spec,containers,name:example-container,image",
        ),
        existing="bash:4.4",
        desired="ubuntu:latest",
    )
    assert compared_result.differences.defaults == []
    assert compared_result.differences.ignored == []
    assert len(compared_result.differences.considered) == 1
    actual_difference = compared_result.differences.considered[0]
    assert actual_difference == expected_difference
    # check that it generates the expected patch document
    # (with the only change being the image field)
    expected_patch_document = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "example-container",
                            "image": "ubuntu:latest",
                        }
                    ]
                }
            }
        }
    }
    patch_document = compared_result.patch_document()
    assert patch_document == expected_patch_document
