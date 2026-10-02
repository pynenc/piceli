"""A failed composition build keeps the failing command's output (kind or k3s).

A build Job runs ``piceli build job-run --sources … --spec …`` (what the
GitOps controller's image build Job runs) in the controller image of this
checkout. The spec's command prints some output, a line with a secret-like
value, and fails. The build Job is kept, its log ends with the command's
output (redacted), and the error the controller records carries the same
tail (``gitops status`` ``failure.log_tail``).

Runs on the cluster of ``test_cluster_registry_kind`` (same environment
variables); it builds the controller image with ``docker buildx`` and loads
it into every node. It never uses the current context and removes the Job.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from tests.integration.test_cluster_registry_kind import pytestmark  # noqa: F401
from tests.integration.test_registry_from_pods_kind import (
    controller_image,  # noqa: F401 (fixture)
)

SPEC = """revision = "piceli.host-build.v1"
name = "two"

[build]
tools = ["sh"]
commands = [["sh", "-c", "echo compiling crate; echo 'error: expected item token=s3cr3t-in-log' >&2; exit 1"]]

[context.config]
include = ["config/**"]

[context.worker]
source = "worker"
include = ["jobs/**"]

[[output.image]]
name = "worker"
repository = "two/worker"
contexts = ["worker"]
files = { "worker/jobs" = "/opt/worker" }
"""

SCRIPT = """
set -eu
cd "$HOME"
mkdir -p src/infra/config src/worker/jobs
cat > src/infra/host-build.toml <<'TOML'
{spec}
TOML
echo '{{}}' > src/infra/config/api.json
echo 'echo hi' > src/worker/jobs/run.sh
for repo in infra worker; do
  git -C src/$repo init -q && git -C src/$repo add -A
  git -C src/$repo -c user.name=t -c user.email=t@example.invalid commit -q -m s
done
exec piceli build job-run --sources src --spec infra/host-build.toml \\
  --image '{image}' --platform linux/amd64 --cache cache --out out \\
  --registry-url oci://registry.invalid:5000
"""


def test_a_failed_build_job_keeps_the_commands_output(
    controller_image: str,  # noqa: F811
    kind_namespace: str,
    tmp_path: Path,
) -> None:
    from kind_support import CONTEXT, KUBECONFIG

    from piceli.artifacts.cluster_build import BuildCluster
    from piceli.gitops.controller import failure_detail
    from piceli.infra.builders import _job_failed
    from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

    image = json.dumps(
        {"image": "worker", "repository": "two/worker", "key": "sha256:" + "7" * 64}
    )
    name = f"piceli-image-build-{uuid.uuid4().hex[:8]}"
    labels = {
        "app.kubernetes.io/managed-by": "piceli",
        "piceli.io/build": "true",
        "piceli.io/build-cache": "kind-test",
    }
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": kind_namespace, "labels": labels},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 300,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "build",
                            "image": controller_image,
                            "imagePullPolicy": "Never",
                            "env": [{"name": "HOME", "value": "/tmp/home"}],
                            "command": [
                                "sh",
                                "-c",
                                "mkdir -p /tmp/home && "
                                + SCRIPT.format(spec=SPEC, image=image),
                            ],
                        }
                    ],
                },
            },
        },
    }
    client = api_client_from_kubeconfig(Path(KUBECONFIG), CONTEXT)
    cluster = BuildCluster(client, kind_namespace)
    try:
        outcome = cluster.run_build(job, labels)
        assert outcome.state == "failed", outcome.public()
        error = _job_failed(outcome, "image")
        detail = failure_detail(error)
        assert detail is not None, outcome.public()
        tail = detail["log_tail"]
        # The failing command's own output, not only "command 1/1 failed".
        assert "compiling crate" in tail and "error: expected item" in tail, tail
        assert "s3cr3t-in-log" not in tail
        assert detail["kept_job"] == name
        # The next build of the same key (or the branch's teardown) removes it.
        assert cluster.remove_kept(labels) == [name]
    finally:
        cluster.delete_job(name)
        client.close()
