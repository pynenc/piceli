#!/bin/sh
# Reads this namespace's pods and the nodes from the API with the pod's own
# service account, every few seconds; the check reads the last result.
set -u
dir=/var/run/secrets/kubernetes.io/serviceaccount
api=https://kubernetes.default.svc
mkdir -p /tmp/watch
while true; do
  token="$(cat "$dir/token")"
  namespace="$(cat "$dir/namespace")"
  if curl -sf -m 5 --cacert "$dir/ca.crt" -H "Authorization: Bearer $token" \
      "$api/api/v1/namespaces/$namespace/pods?limit=1" >/dev/null \
    && curl -sf -m 5 --cacert "$dir/ca.crt" -H "Authorization: Bearer $token" \
      "$api/api/v1/nodes?limit=1" >/dev/null; then
    echo pods-ok > /tmp/watch/state
  else
    echo api-unreachable > /tmp/watch/state
  fi
  sleep 5
done
