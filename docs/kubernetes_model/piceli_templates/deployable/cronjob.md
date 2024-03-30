# CronJob

**A wrapper for defining Kubernetes CronJob resources within Piceli.**

CronJobs in Kubernetes allow you to run jobs on a time-based schedule. These jobs can be anything from a simple maintenance task to a batch job that runs at specific intervals. The `CronJob` class in Piceli extends the `Job` class, incorporating scheduling capabilities on top of the standard job configuration.

## Properties

- `schedule`: Defines the schedule on which the job should be run. Uses the `CronTab` format to specify the timing.
- `name`, `labels`, `backoff_limit`, and pod spec are inherited from the `Job` class.

### Example

```python
from piceli.k8s.templates.auxiliary import crontab
from piceli.k8s.templates.deployable import cronjob

my_cron_job = cronjob.CronJob(
    name="example-cronjob",
    schedule=crontab.CronTab("0 */3 * * *"),  # Every 3 hours
    labels={"app": "myapp"},
    backoff_limit=2,
)
```

This example defines a CronJob named "example-cronjob" scheduled to run every 3 hours, with a specific set of labels and a backoff limit.
