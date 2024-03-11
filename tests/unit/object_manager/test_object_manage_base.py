from typing import Generator, Optional
from unittest.mock import MagicMock, call, patch

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from piceli.k8s.constants.dry_run import DryRun
from piceli.k8s.exceptions import api_exceptions
from piceli.k8s.k8s_client.client import ClientContext
from piceli.k8s.k8s_objects.base import K8sObject, OriginYAML
from piceli.k8s.object_manager.base import ObjectManager


@pytest.fixture
def k8s_object() -> K8sObject:
    spec = {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": {"name": "object_name"},
    }
    return K8sObject(spec=spec, origin=OriginYAML("some/path/object.yml"))


@pytest.fixture
def obj_manager(k8s_object: K8sObject) -> ObjectManager:
    return ObjectManager(k8s_object=k8s_object)


@pytest.fixture
def client_context() -> Generator[ClientContext, None, None]:
    with patch(
        "piceli.k8s.k8s_client.client.ClientManager.get_client",
        return_value=MagicMock(spec=client.ApiClient),
    ):
        context = ClientContext(kubeconfig=None)
        yield context


@pytest.fixture
def mock_api_method() -> Generator[MagicMock, None, None]:
    with patch("piceli.k8s.k8s_client.client.ClientContext.get_api") as mock:
        yield mock


@pytest.fixture
def mock_read() -> Generator[MagicMock, None, None]:
    with patch.object(ObjectManager, "read") as mock:
        yield mock


@pytest.fixture
def mock_create() -> Generator[MagicMock, None, None]:
    with patch.object(ObjectManager, "create") as mock:
        yield mock


@pytest.fixture
def mock_delete() -> Generator[MagicMock, None, None]:
    with patch.object(ObjectManager, "delete") as mock:
        yield mock


test_get_api__cases = [
    (
        {"apiVersion": "batch/v1", "kind": "CronJob", "metadata": {"name": "object"}},
        "BatchV1Api",
    ),
    (
        {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "object"}},
        "BatchV1Api",
    ),
    (
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "object"}},
        "AppsV1Api",
    ),
    (
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "object"}},
        "CoreV1Api",
    ),
    (
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": "object"},
        },
        "CoreV1Api",
    ),
    (
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "object"}},
        "CoreV1Api",
    ),
]


def generate_ids(cases: list[tuple[dict, str]]) -> list[str]:
    return [
        f"{spec['apiVersion']}_{spec['kind']} -> {api_name}" for spec, api_name in cases
    ]


@pytest.mark.parametrize(
    "k8s_object_spec, expected_api_name",
    test_get_api__cases,
    ids=generate_ids(test_get_api__cases),
)
def test_get_api(
    k8s_object_spec: dict, expected_api_name: str, client_context: ClientContext
) -> None:
    k8s_object = K8sObject(
        spec=k8s_object_spec, origin=OriginYAML("some/path/object.yml")
    )
    object_manager = ObjectManager(k8s_object=k8s_object)
    api_instance = object_manager.get_api(client_context)
    assert type(api_instance).__name__ == expected_api_name


test_api_methods__cases = [
    (
        {"apiVersion": "batch/v1", "kind": "CronJob", "metadata": {"name": "object"}},
        [
            "create_namespaced_cron_job",
            # "delete_collection_namespaced_cron_job",
            "delete_namespaced_cron_job",
            "list_namespaced_cron_job",
            "patch_namespaced_cron_job",
            "read_namespaced_cron_job",
            "replace_namespaced_cron_job",
        ],
    ),
    (
        {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "object"}},
        [
            "create_namespaced_job",
            # "delete_collection_namespaced_job",
            "delete_namespaced_job",
            "list_namespaced_job",
            "patch_namespaced_job",
            "read_namespaced_job",
            "replace_namespaced_job",
        ],
    ),
    (
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "object"}},
        [
            "create_namespaced_deployment",
            "delete_namespaced_deployment",
            "list_namespaced_deployment",
            "patch_namespaced_deployment",
            "read_namespaced_deployment",
            "replace_namespaced_deployment",
        ],
    ),
    (
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "object"}},
        [
            "create_namespaced_service",
            "delete_namespaced_service",
            "list_namespaced_service",
            "patch_namespaced_service",
            "read_namespaced_service",
            "replace_namespaced_service",
        ],
    ),
    (
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": "object"},
        },
        [
            "create_namespaced_persistent_volume_claim",
            "delete_namespaced_persistent_volume_claim",
            "list_namespaced_persistent_volume_claim",
            "patch_namespaced_persistent_volume_claim",
            "read_namespaced_persistent_volume_claim",
            "replace_namespaced_persistent_volume_claim",
        ],
    ),
    (
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "object"}},
        [
            "create_namespaced_service_account",
            "delete_namespaced_service_account",
            "list_namespaced_service_account",
            "patch_namespaced_service_account",
            "read_namespaced_service_account",
            "replace_namespaced_service_account",
        ],
    ),
]


def generate_method_ids(cases: list[tuple[dict, list]]) -> list:
    return [f"{case[0]['apiVersion']}_{case[0]['kind']}" for case in cases]


@pytest.mark.parametrize(
    "k8s_object_spec, expected_methods",
    test_api_methods__cases,
    # indirect=["k8s_object_spec"],
    ids=generate_method_ids(test_api_methods__cases),
)
def test_api_methods(
    k8s_object_spec: dict, expected_methods: list[str], client_context: ClientContext
) -> None:
    # Setup the K8sObject and ObjectManager
    k8s_object = K8sObject(spec=k8s_object_spec, origin=OriginYAML("dummy/path"))
    object_manager = ObjectManager(k8s_object=k8s_object)
    # test api_methods
    methods = object_manager.api_methods
    assert methods == expected_methods
    # test get_method_name
    for method in ["create", "delete", "list", "patch", "read", "replace"]:
        method_name = object_manager.get_method_name(method)
        assert method_name in methods
        api_method = object_manager._api_method(client_context, method)
        assert api_method.__name__ == method_name

    # test namespaced
    assert object_manager.namespaced == any(
        "namespaced" in method for method in methods
    )


@pytest.mark.parametrize(
    "inbound_ns, object_ns, expected_ns",
    [
        (None, None, "default"),
        ("other", None, "other"),
        ("other", "obj-ns", "other"),
        (None, "obj-ns", "obj-ns"),
    ],
)
def test_resolve_namespace(
    inbound_ns: Optional[str], object_ns: Optional[str], expected_ns: str
) -> None:
    spec: dict = {"apiVersion": "batch/v1", "kind": "CronJob"}
    spec["metadata"] = {"name": "object"}
    if object_ns:
        spec["metadata"]["namespace"] = object_ns
    k8s_object = K8sObject(spec=spec, origin=OriginYAML("dummy/path"))
    object_manager = ObjectManager(k8s_object=k8s_object)
    assert object_manager._resolve_namespace(inbound_ns) == expected_ns


@pytest.mark.parametrize(
    "with_name, with_spec, namespaced, expected_base_args",
    [
        (True, True, True, ("object", "default")),
        (False, True, True, ("default",)),
        (True, False, True, ("object", "default")),
        (False, False, True, ("default",)),
        (True, True, False, ("object",)),
        (False, True, False, ()),
        (True, False, False, ("object",)),
        (False, False, False, ()),
    ],
)
def test_prepare_args(
    with_name: bool, with_spec: bool, namespaced: bool, expected_base_args: tuple
) -> None:
    # Setup the K8sObject and ObjectManager
    spec = {"apiVersion": "batch/v1", "kind": "CronJob", "metadata": {"name": "object"}}
    k8s_object = K8sObject(spec=spec, origin=OriginYAML("dummy/path"))
    object_manager = ObjectManager(k8s_object=k8s_object)
    object_manager.namespaced = namespaced

    args = object_manager._prepare_args(with_name=with_name, with_spec=with_spec)

    expected = expected_base_args + (spec,) if with_spec else expected_base_args
    assert args == expected


def test_invoke_api(client_context: ClientContext, obj_manager: ObjectManager) -> None:
    with patch.object(
        obj_manager,
        "_api_method",
    ) as mock_api_method:
        # test _invoke_api - success
        obj_manager._invoke_api(client_context, "read")
        mock_api_method.assert_called_once_with(client_context, "read")
        mock_api_method.return_value.assert_called_once()

        # test _invoke_api - ApiException exception
        api_exception = ApiException(reason="An error occurred")
        api_exception.status = 404
        api_exception.body = '{"message": "An error occurred"}'
        mock_api_method.return_value.side_effect = api_exception
        with pytest.raises(api_exceptions.ApiOperationException):
            obj_manager._invoke_api(client_context, "read")

        # test _invoke_api - other exception
        mock_api_method.return_value.side_effect = ValueError
        with pytest.raises(ValueError):
            obj_manager._invoke_api(client_context, "read")


def test_read(client_context: ClientContext, obj_manager: ObjectManager) -> None:
    with patch.object(
        obj_manager, "_invoke_api", return_value=obj_manager.k8s_object.spec
    ) as mock_invoke:
        result = obj_manager.read(client_context)
        assert isinstance(result, K8sObject)
        mock_invoke.assert_called_once_with(
            client_context, "read", "object_name", "default"
        )


def test_patch(client_context: ClientContext, obj_manager: ObjectManager) -> None:
    with patch.object(obj_manager, "_invoke_api") as mock_invoke:
        obj_manager.patch(client_context)
        mock_invoke.assert_called_once_with(
            client_context,
            "patch",
            "object_name",
            "default",
            obj_manager.k8s_object.spec,
            async_req=False,
            dry_run=DryRun.OFF.value,
        )


def test_create_success(
    client_context: ClientContext, obj_manager: ObjectManager
) -> None:
    with patch.object(obj_manager, "_invoke_api") as mock_invoke:
        obj_manager.create(client_context)
        mock_invoke.assert_called_once_with(
            client_context,
            "create",
            "default",
            obj_manager.k8s_object.spec,
            async_req=False,
            dry_run=DryRun.OFF.value,
        )


def get_api_op_ex(
    reason: api_exceptions.ReasonEnum | None = None, being_deleted: bool = False
) -> api_exceptions.ApiOperationException:
    return api_exceptions.ApiOperationException(
        code=404,
        status="Not Found",
        reason=reason.value if reason else "NotFound",
        message="object is being deleted" if being_deleted else "Not Found",
        details={},
        ex=ApiException(reason="Not Found"),
        body={"message": "Not Found"},
    )


def raise_ex(ex: ApiException) -> None:
    raise ex


def test_create_on_delete_retry(
    client_context: ClientContext, obj_manager: ObjectManager
) -> None:
    api_exception_being_deleted = get_api_op_ex(
        reason=api_exceptions.ReasonEnum.AlreadyExists, being_deleted=True
    )

    with patch.object(
        obj_manager, "_invoke_api", side_effect=[api_exception_being_deleted, None]
    ) as mock_invoke:
        obj_manager.create(client_context)

        assert mock_invoke.call_count == 2, "Expected _invoke_api to be called twice"
        expected_call = call(
            client_context,
            "create",
            "default",
            obj_manager.k8s_object.spec,
            async_req=False,
            dry_run=DryRun.OFF.value,
        )
        mock_invoke.assert_has_calls([expected_call, expected_call])


def test_create_on_already_exists(
    client_context: ClientContext, obj_manager: ObjectManager
) -> None:
    api_exception_already_exists = get_api_op_ex(
        reason=api_exceptions.ReasonEnum.AlreadyExists
    )

    with patch.object(
        obj_manager, "_invoke_api", side_effect=[api_exception_already_exists]
    ) as mock_invoke:
        obj_manager.create(client_context)

        assert mock_invoke.call_count == 1, "Expected _invoke_api to be called once"
        mock_invoke.assert_called_once_with(
            client_context,
            "create",
            "default",
            obj_manager.k8s_object.spec,
            async_req=False,
            dry_run=DryRun.OFF.value,
        )


def test_create_other_exc_failure(
    client_context: ClientContext, obj_manager: ObjectManager
) -> None:
    api_exception = get_api_op_ex(reason=api_exceptions.ReasonEnum.Unknown)

    with patch.object(
        obj_manager, "_invoke_api", side_effect=[api_exception]
    ) as mock_invoke:
        with pytest.raises(api_exceptions.ApiOperationException):
            obj_manager.create(client_context)

        assert mock_invoke.call_count == 1, "Expected _invoke_api to be called once"
        mock_invoke.assert_called_once_with(
            client_context,
            "create",
            "default",
            obj_manager.k8s_object.spec,
            async_req=False,
            dry_run=DryRun.OFF.value,
        )


def test_delete_success(
    client_context: ClientContext, obj_manager: ObjectManager
) -> None:
    with patch.object(obj_manager, "_invoke_api") as mock_invoke:
        obj_manager.delete(client_context)
        mock_invoke.assert_called_once_with(
            client_context,
            "delete",
            "object_name",
            "default",
            async_req=False,
            dry_run=DryRun.OFF.value,
        )


def test_delete_not_found(
    client_context: ClientContext, obj_manager: ObjectManager
) -> None:
    api_exception_not_found = get_api_op_ex(reason=api_exceptions.ReasonEnum.NotFound)
    with patch.object(
        obj_manager, "_invoke_api", side_effect=[api_exception_not_found]
    ) as mock_invoke:
        result = obj_manager.delete(client_context)
        assert result is None, "Expected delete to return None if object does not exist"
        mock_invoke.assert_called_once_with(
            client_context,
            "delete",
            "object_name",
            "default",
            async_req=False,
            dry_run=DryRun.OFF.value,
        )


def test_delete_other_exception(
    client_context: ClientContext, obj_manager: ObjectManager
) -> None:
    api_exception = get_api_op_ex(reason=api_exceptions.ReasonEnum.Unknown)
    with patch.object(
        obj_manager, "_invoke_api", side_effect=[api_exception]
    ) as mock_invoke:
        with pytest.raises(api_exceptions.ApiOperationException):
            obj_manager.delete(client_context)
        assert mock_invoke.call_count == 1, "Expected _invoke_api to be called once"
        mock_invoke.assert_called_once_with(
            client_context,
            "delete",
            "object_name",
            "default",
            async_req=False,
            dry_run=DryRun.OFF.value,
        )


def test_apply_not_exists(
    client_context: ClientContext,
    obj_manager: ObjectManager,
    mock_read: MagicMock,
    mock_create: MagicMock,
) -> None:
    api_exception_not_found = get_api_op_ex(reason=api_exceptions.ReasonEnum.NotFound)
    mock_read.side_effect = api_exception_not_found
    obj_manager.apply(client_context)
    mock_create.assert_called_once()


def test_apply_already_exists(
    client_context: ClientContext,
    obj_manager: ObjectManager,
    mock_read: MagicMock,
    mock_delete: MagicMock,
    mock_create: MagicMock,
) -> None:
    # mock read so first found an object and then, after delete, not found
    api_exception_not_found = get_api_op_ex(reason=api_exceptions.ReasonEnum.NotFound)
    mock_read.side_effect = [{}, api_exception_not_found]

    obj_manager.apply(client_context)
    mock_delete.assert_called_once()
    mock_create.assert_called_once()


def test_apply_with_delete_timeout(
    client_context: ClientContext,
    obj_manager: ObjectManager,
    mock_read: MagicMock,
    mock_delete: MagicMock,
    mock_create: MagicMock,
) -> None:
    # mock read so first found an object and then, after delete, not found
    api_exception_not_found = get_api_op_ex(reason=api_exceptions.ReasonEnum.NotFound)
    mock_read.side_effect = [{}, api_exception_not_found]

    with patch(
        "time.time", side_effect=[1, 2, 3, 4000]
    ) as time_mock:  # Simulating time passing for timeout
        obj_manager.apply(client_context)
        mock_delete.assert_called_once()
        mock_create.assert_called_once()
        time_mock.assert_called_once()


def test_apply_dry_run(
    client_context: ClientContext,
    obj_manager: ObjectManager,
    mock_read: MagicMock,
    mock_delete: MagicMock,
    mock_create: MagicMock,
) -> None:
    mock_read.return_value = obj_manager.k8s_object.spec
    obj_manager.apply(client_context, dry_run=DryRun.ON)
    assert mock_read.call_count == 2
    mock_delete.assert_called_once()
    # Create should not be called in dry run after delete
    mock_create.assert_not_called()


def test_apply_exception_unknown(
    client_context: ClientContext, obj_manager: ObjectManager, mock_read: MagicMock
) -> None:
    api_exception = get_api_op_ex(reason=api_exceptions.ReasonEnum.Unknown)
    mock_read.side_effect = api_exception
    with pytest.raises(api_exceptions.ApiOperationException):
        obj_manager.apply(client_context)


def test_apply_exception_not_found(
    client_context: ClientContext,
    obj_manager: ObjectManager,
    mock_read: MagicMock,
    mock_create: MagicMock,
) -> None:
    api_exception = get_api_op_ex(reason=api_exceptions.ReasonEnum.NotFound)
    mock_read.side_effect = api_exception
    obj_manager.apply(client_context)
    mock_read.assert_called_once()
    mock_create.assert_called_once()


def test_wait(client_context: ClientContext, obj_manager: ObjectManager) -> None:
    with patch("piceli.k8s.utils.utils_wait.wait") as mock_utils_wait:
        obj_manager.wait(client_context)
        # Check if utils_wait.wait was called correctly
        expected_args = (
            (obj_manager._resolve_namespace(None),) if obj_manager.namespaced else ()
        )
        mock_utils_wait.assert_called_once_with(
            ctx=client_context,
            list_func=obj_manager._api_method(client_context, "list"),
            args=expected_args,
            obj_name=obj_manager.k8s_object.name,
        )
