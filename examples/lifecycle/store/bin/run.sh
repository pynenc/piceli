#!/bin/sh
# The store: keeps its data on the retained claim and serves it read-only.
set -eu
test -s /etc/store/secret/token
test -s /etc/store/config/store.json
date -u +%Y-%m-%dT%H:%M:%SZ >> /var/lib/store/starts
echo ok > /var/lib/store/ready
# Two log lines the lifecycle acceptance reads through the UI: a known one,
# and a secret-like setting printed by mistake (a made-up value), which the
# UI must mask.
echo "store: serving /var/lib/store on port 7000"
echo "store: upstream password=example-not-a-real-password"
exec httpd -f -p 7000 -h /var/lib/store
