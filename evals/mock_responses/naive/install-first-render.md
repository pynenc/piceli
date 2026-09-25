```bash
pip install piceli-k8s
piceli template app.py > manifests.yaml
kubectl apply -f manifests.yaml --kubeconfig ~/.kube/config
```

```python
from piceli import App, Deployment, Service

app = App(name="hello")
app.add_deployment(Deployment(name="hello", image="nginx:1.27", port=80))
app.add_service(Service(name="hello", port=80))
```
