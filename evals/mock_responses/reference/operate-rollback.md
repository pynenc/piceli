A rollback is planned like any change: without `--approve` it prints the plan and
its hash (exit 3); after you approve, the same command applies exactly that plan.

```bash
piceli release status --spec app.py:pipeline
piceli release rollback previous --spec app.py:pipeline
# show the owner the rollback plan and wait for the approval of its hash
piceli release rollback previous --spec app.py:pipeline --approve <hash>
```
