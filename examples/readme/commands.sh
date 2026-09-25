piceli render app.py:app --namespace hello   # the manifests; never contacts a cluster
piceli deploy app.py:pipeline --plan         # what would change, and a combined hash
piceli deploy app.py:pipeline --approve <combined-hash>   # runs exactly that plan
piceli status app.py:pipeline                # is it up, and at which URLs
piceli access app.py:pipeline                # forwards http://127.0.0.1:18080/
