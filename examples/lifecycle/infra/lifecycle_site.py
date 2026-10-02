"""Where this composition runs: the cluster's API, node names and Git remotes.

Everything here is specific to one cluster. The lifecycle acceptance
(``tests/acceptance_k3s/lifecycle.py``) writes this file into the
composition repository it creates, with the values of its disposable k3s
cluster; the defaults below only make the module importable.
"""

from __future__ import annotations

#: The API server of the credential profile ``lifecycle`` (``piceli login``).
API = "https://127.0.0.1:6443"
#: One server node (builder, controller, registry) and two workload nodes.
SERVER = "lifecycle-server-0"
AGENT_A = "lifecycle-agent-0"
AGENT_B = "lifecycle-agent-1"
ARCH = "amd64"
#: Where the three repositories are served (``<GIT_BASE>/<name>.git``).
GIT_BASE = "https://git.example.com/lifecycle"
#: The Piceli image the controller and the UI run, pinned by digest.
CONTROLLER_IMAGE = "ghcr.io/pynenc/piceli-controller@sha256:" + "0" * 64
#: How often the controller polls its sources.
POLL = "1m"
