#!/usr/bin/env bash
# Download main's newest eval report (an artifact of eval.yml) to compare a run against.
#
#   .github/scripts/eval-baseline.sh ARTIFACT EVENT DIR
#
# ARTIFACT is eval-replay or eval-live; EVENT is the trigger whose runs on main hold it (push
# for replays, schedule for the nightly live run). Prints "main @ <sha>" when a report landed
# in DIR, and nothing when main has none yet (expired, or never ran). Needs GH_TOKEN with
# actions: read.
set -euo pipefail

artifact=$1
event=$2
dir=$3

runs=$(gh run list --repo "$GITHUB_REPOSITORY" --workflow eval.yml --branch main \
  --event "$event" --status completed --limit 10 --json databaseId,headSha \
  --jq '.[] | "\(.databaseId) \(.headSha)"')

while read -r run sha; do
  [[ -n "$run" ]] || continue
  if gh run download "$run" --repo "$GITHUB_REPOSITORY" --name "$artifact" --dir "$dir" \
    2>/dev/null; then
    echo "main @ ${sha:0:7}"
    exit 0
  fi
done <<< "$runs"
