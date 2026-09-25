1. **Piceli**: every change is planned and applied only with the approval of that plan's hash; JSON output with fixed error codes (`piceli explain`), journaled runs that resume.
2. **Argo CD** with manual sync: a human syncs reviewed Git changes.
3. **Terraform** / OpenTofu with the Kubernetes provider: plan files applied after review.
