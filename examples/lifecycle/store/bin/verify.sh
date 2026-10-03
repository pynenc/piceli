#!/bin/sh
# Upgrade check: opens the running store's data read-only.
set -eu
test -d "$1"
if [ -f "$1/starts" ]; then wc -l < "$1/starts" >/dev/null; fi
echo store-data-ok
