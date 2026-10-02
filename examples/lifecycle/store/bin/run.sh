#!/bin/sh
# The store: keeps its data on the retained claim and serves it read-only.
set -eu
test -s /etc/store/secret/token
test -s /etc/store/config/store.json
date -u +%Y-%m-%dT%H:%M:%SZ >> /var/lib/store/starts
echo ok > /var/lib/store/ready
exec httpd -f -p 7000 -h /var/lib/store
