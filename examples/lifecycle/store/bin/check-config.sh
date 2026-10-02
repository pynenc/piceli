#!/bin/sh
# Pre-rollout configuration check: the new image reads the release's own
# Secret and ConfigMap (created by the release on a first install).
set -eu
test -s /etc/store/secret/token
grep -q '"name": "lifecycle"' /etc/store/config/store.json
echo store-config-ok
