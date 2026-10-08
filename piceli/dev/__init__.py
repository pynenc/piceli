"""Development builds: a developer's or agent's commands on a cluster builder (0.18.0).

``piceli dev run`` ships a commit or a working tree to a run pod on the
builder node declared by ``Cluster(dev=DevBuilds(...))`` and runs build and
test commands there with a shared warm cache (see ``docs/dev_builds.md``).

Importing this package is side-effect free.
"""
