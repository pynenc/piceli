# Job

**A representation of a Kubernetes Job in Piceli.**

Kubernetes Jobs create one or more pods and ensure that a specified number of them successfully terminate. When the specified number of pods terminates successfully, the job is complete. The `Job` class in Piceli allows for defining these jobs, including their behavior, lifecycle, and cleanup strategy.

## Properties

- `cleanup_after_seconds`: (Optional) Time in seconds after the job finishes to automatically clean up the job's pods. If not specified, pods are not automatically cleaned up.
- `backoff_limit`: (Optional) Specifies the number of retries before considering the job as failed. Defaults to Kubernetes' default if not specified.
- `labels`: (Optional) Labels to be applied to the job for identification and selection.

## Job Cleanup and Retry Policy

One of the key features of a Kubernetes Job is controlling the job's lifecycle, including cleanup after completion and the strategy for retrying failed jobs. Piceli's `Job` class encapsulates these functionalities, providing an easy way to define and manage them.

- `cleanup_after_seconds`: Automatically cleans up job pods after the specified duration since job completion.
- `backoff_limit`: Controls the number of retries for a failed job. A job is considered failed if the pod exits with a non-zero status.

## Restrictions

- **Restart Policy**: Kubernetes Jobs do not support a restart policy of `Always`. The only valid values are `OnFailure` and `Never`. This is enforced by the Piceli `Job` class to align with Kubernetes API requirements.

### Example

```python
from piceli.k8s import templates

job = templates.Job(
        name="test-job",
        image_pull_secrets=["docker-registry-credentials"],
        backoff_limit=1,
        containers=[
            templates.Container(
                name="test-job",
                command=["python", "--version"],
                image="docker-image",
                env={"K0": "V0"},
                liveness_command=[
                    "sh",
                    "-c",
                    "test $(expr $(date +%s) - $(cat /tmp/health_check)) -lt 60",
                ],
                resources=templates.Resources(
                    cpu="100m", memory="250Mi", ephemeral_storage="11Mi"
                ),
            )
        ],
        template_labels={"pod_name": "test-job"},
        labels={"job_name": "test-job"},
    )
```

In this example, a Job is defined to perform batch processing, with automatic cleanup of its pods 1 hour after the job finishes and a backoff limit of 3 retries.
