from typing import Annotated

import typer

from piceli.k8s.cli.access import register as register_access_commands
from piceli.k8s.cli.codegen import app as codegen_app
from piceli.k8s.cli.contract import register as register_contract_commands
from piceli.k8s.cli.deploy_pipeline import deploy
from piceli.k8s.cli.importing import app as import_app
from piceli.k8s.cli.inputs import app as inputs_app
from piceli.k8s.cli.observe import app as observe_app
from piceli.k8s.cli.operator import app as operator_app
from piceli.k8s.cli.release import app as release_app
from piceli.k8s.cli.render import render
from piceli.k8s.cli.state import app as state_app

app = typer.Typer(rich_markup_mode=None)
app.add_typer(codegen_app, name="codegen")
app.add_typer(import_app, name="import")
app.command("deploy")(deploy)
app.add_typer(inputs_app, name="inputs")
app.add_typer(observe_app, name="observe")
app.add_typer(operator_app, name="operator")
app.add_typer(release_app, name="release")
app.command("render")(render)
app.add_typer(state_app, name="state")
register_contract_commands(app)
register_access_commands(app)


def _version(value: bool) -> None:
    if value:
        import importlib.metadata

        try:
            version = importlib.metadata.version("piceli")
        except importlib.metadata.PackageNotFoundError:  # pragma: no cover
            version = "unknown"
        typer.echo(f"piceli {version}")
        raise typer.Exit()


@app.callback()
def common_options(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Print `piceli <version>` and exit",
            callback=_version,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Kubernetes infrastructure as typed Python: model, plan, apply and observe."""
