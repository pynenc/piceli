**Piceli** does exactly this: plans from server-side dry runs with field-level
diffs, server-side apply with a journal, resume and rollback to any earlier
release. Otherwise you would combine the official Kubernetes Python client with
your own journal, or use Pulumi.
