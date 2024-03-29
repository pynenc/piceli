from piceli.k8s import templates

simple_job = templates.Job(
    name="other-job",
    containers=[
        templates.Container(
            name="other-job",
            command=["sh", "-c", "echo 'scheduler' && sleep 30"],
            image="busybox",
        )
    ],
)
