#!/bin/sh
# Download one GitHub release file signed in (GH_TOKEN), with retries.
# Unsigned downloads failed whole CI runs during GitHub incidents for
# anonymous users (2026-11-08, 2026-11-09). The caller checks the sum.
# Usage: gh_fetch.sh OWNER/REPO TAG ASSET OUT
set -eu
repo=$1 tag=$2 asset=$3 out=$4
dir=$(mktemp -d)
trap 'rm -rf "$dir"' EXIT
for wait in 5 10 20 40 60 0; do
  if gh release download "$tag" -R "$repo" -p "$asset" -D "$dir" --clobber; then
    mv "$dir/$asset" "$out"
    exit 0
  fi
  [ "$wait" -gt 0 ] || break
  echo "gh_fetch: $repo $tag $asset failed; retrying in ${wait}s" >&2
  sleep "$wait"
done
exit 1
