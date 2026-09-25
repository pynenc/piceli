Plan first; nothing changes until you approve the combined hash the plan prints.

```bash
piceli deploy app.py:pipeline --plan --json
# show the owner the plan summary (stderr) and wait for their approval of the hash
piceli deploy app.py:pipeline --approve <hash> --json
piceli release status --spec app.py:pipeline
```
