#!/usr/bin/env bash
# Keep one open issue per failing nightly workflow (label nightly-failure).
#
#   scripts/nightly_issue.sh <workflow> <failed|passed>
#
# failed: open the issue, or comment on it when it is already open.
# passed: close the open issue. Needs GH_TOKEN with issues: write.
set -euo pipefail

workflow=$1
outcome=$2
title="Nightly $workflow failed"
run="$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID"
number=$(gh issue list --label nightly-failure --state open --json number,title \
  --jq "map(select(.title == \"$title\"))[0].number // empty")

if [ "$outcome" = failed ]; then
  if [ -n "$number" ]; then
    gh issue comment "$number" --body "Failed again: $run"
  else
    gh label create nightly-failure --color B60205 \
      --description "A scheduled CI run failed" 2>/dev/null || true
    gh issue create --title "$title" --label nightly-failure \
      --body "The scheduled $workflow run failed: $run

This issue closes itself when the nightly passes again."
  fi
elif [ -n "$number" ]; then
  gh issue close "$number" --comment "Passed again: $run"
fi
