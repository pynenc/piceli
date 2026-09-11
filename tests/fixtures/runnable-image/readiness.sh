#!/bin/sh
set -eu
test -r /lib/aarch64-linux-gnu/libc.so.6
if test "${1:-}" = sleep; then
    sleep 30
fi
printf 'piceli-ready\n'
