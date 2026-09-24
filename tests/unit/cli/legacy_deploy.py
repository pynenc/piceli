"""The legacy ``deploy plan/detail/run`` group, mounted for its own tests.

``piceli deploy`` is now the pipeline command; the legacy group is no longer
registered on the root CLI but its modules remain until they are removed.
"""

import typer

from piceli.k8s.cli import common_options
from piceli.k8s.cli.deploy import app as deploy_app

app = typer.Typer()
app.callback()(common_options)
app.add_typer(deploy_app, name="deploy")
