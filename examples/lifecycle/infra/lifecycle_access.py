"""Where ``piceli access`` reaches the long-lived environments from a laptop.

``piceli access`` takes ``module:attr`` of an object with ``.app`` and
``.target``; a composition's environments are deployed by the controller, so
each one is named here with its namespace and the cluster's credential
profile (``piceli login lifecycle``)::

    piceli access lifecycle_access.py:main     # web on 127.0.0.1:18080, Ctrl-C stops it
    piceli access stop --stale lifecycle_access.py:main

The forwards are the ones the app declares (``app.access.forward``); nothing
here changes what is deployed.
"""

from __future__ import annotations

from dataclasses import dataclass

from lifecycle_app import app

from piceli import App, Target


@dataclass(frozen=True)
class Reach:
    """An environment as ``piceli access`` and ``piceli status`` read it."""

    app: App
    target: Target


main = Reach(app, Target.profile("lifecycle", namespace="lc-main"))
rc = Reach(app, Target.profile("lifecycle", namespace="lc-rc"))
