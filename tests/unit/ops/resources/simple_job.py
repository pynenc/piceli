from piceli.k8s import templates

simple_job = templates.Job(
    name="tasker-scheduler",
    containers=[
        templates.Container(
            name="tasker-scheduler",
            command=["sh", "-c", "echo 'scheduler' && sleep 30"],
            image="busybox",
        )
    ],
)
