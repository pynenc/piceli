#!/bin/sh
# The example's "binary": a page that says which cluster it runs in and
# whether its generated Secrets are there (never their values).
set -eu
mkdir -p /tmp/www
uid="$(cat "${CLUSTER_UID_FILE:-/run/cluster-identity/uid}")"
token=absent
[ -s /run/private/api-token ] && token=present
certificate=absent
[ -s /run/private/web.crt ] && certificate=present
printf '<p>cluster %s</p>\n<p>token %s</p>\n<p>certificate %s</p>\n' \
  "$uid" "$token" "$certificate" > /tmp/www/index.html
exec httpd -f -p 8080 -h /tmp/www
