import typer

from piceli.k8s.cli.access import register as register_access_commands
from piceli.k8s.cli.contract import register as register_contract_commands
from piceli.k8s.cli.deploy_pipeline import deploy
from piceli.k8s.cli.importing import app as import_app
from piceli.k8s.cli.inputs import app as inputs_app
from piceli.k8s.cli.observe import app as observe_app
from piceli.k8s.cli.operator import app as operator_app
from piceli.k8s.cli.release import app as release_app
from piceli.k8s.cli.render import render

app = typer.Typer(rich_markup_mode=None)
app.add_typer(import_app, name="import")
app.command("deploy")(deploy)
app.add_typer(inputs_app, name="inputs")
app.add_typer(observe_app, name="observe")
app.add_typer(operator_app, name="operator")
app.add_typer(release_app, name="release")
app.command("render")(render)
register_contract_commands(app)
register_access_commands(app)


@app.callback()
def common_options() -> None:
    """
    Piceli kubernetes commands
    """
