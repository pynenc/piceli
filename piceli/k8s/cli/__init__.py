from typing import Annotated

import typer

from piceli.conf.config_k8s import ConfigK8s
from piceli.conf.config_k8s_model import ConfigK8sModel
from piceli.k8s.cli.context import ContextObject
from piceli.k8s.cli.deploy import app as deploy_app
from piceli.k8s.cli.model import app as model_app

# from piceli.k8s.cli.pods import app as pods_app
# from piceli.k8s.cli.services import app as services_app
# from piceli.k8s.cli.nodes import app as nodes_app

app = typer.Typer()
app.add_typer(deploy_app, name="deploy")
app.add_typer(model_app, name="model")
# app.add_typer(pods_app, name="pods")
# app.add_typer(services_app, name="services")
# app.add_typer(nodes_app, name="nodes")


@app.callback()
def common_options(
    ctx: typer.Context,
    namespace: Annotated[
        str,
        typer.Option(
            ...,
            "--namespace",
            "-n",
            envvar=ConfigK8s.get_env_key("namespace"),
            help="Namespace on the kubernetes cluster",
            show_default=True,
            show_envvar=True,
        ),
    ] = ConfigK8s().namespace,
    module_name: Annotated[
        str,
        typer.Option(
            ...,
            "--module-name",
            "-mn",
            envvar=ConfigK8sModel.get_env_key("module_name"),
            help="Folder containing Kubernetes objects specifications.",
            show_default=True,
            show_envvar=True,
        ),
    ] = ConfigK8sModel().module_name,
    module_path: Annotated[
        str,
        typer.Option(
            ...,
            "--module-path",
            "-mp",
            envvar=ConfigK8sModel.get_env_key("module_path"),
            help="Folder containing Kubernetes objects specifications.",
            show_default=True,
            show_envvar=True,
        ),
    ] = ConfigK8sModel().module_path,
    folder_path: Annotated[
        str,
        typer.Option(
            ...,
            "--folder-path",
            "-fp",
            envvar=ConfigK8sModel.get_env_key("folder_path"),
            help="Folder containing Kubernetes objects specifications.",
            show_default=True,
            show_envvar=True,
        ),
    ] = ConfigK8sModel().folder_path,
    sub_elements: Annotated[
        bool,
        typer.Option(
            ...,
            "--sub-elements",
            "-se",
            envvar=ConfigK8sModel.get_env_key("sub_elements"),
            help="Should load kubernetes objects from sub folders/modules",
            show_default=True,
            show_envvar=True,
        ),
    ] = ConfigK8sModel().sub_elements,
) -> None:
    """
    Piceli kubernetes commands
    """
    ctx.obj = ContextObject(
        namespace=namespace,
        module_name=module_name,
        module_path=module_path,
        folder_path=folder_path,
        sub_elements=sub_elements,
    )
