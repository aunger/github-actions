#!/usr/bin/env bash
# run_action.sh -- the body of the branch-cleanup-report action: turn its inputs
# into a branch_cleanup_report.py command line, and send the report to the job
# summary.
#
# Environment (set from the action's inputs):
#   MODE         report or delete
#   CHOICES      for delete, the JSON the report step output
#   ENVIRONMENT  for delete, the environment the run's approval is for
#   KEEP         branch name patterns to keep, one per line
#   GRACE_DAYS   the report's grace period in days
#   PYTHON       the interpreter to run (default: python3)
#
# `report` also appends the branches it offered to $GITHUB_OUTPUT, as the
# `choices` step output.
# Both modes write their Markdown report to $GITHUB_STEP_SUMMARY.
#
# Exit codes: the script's own, or 2 for a usage error.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"
SUMMARY="${GITHUB_STEP_SUMMARY:-/dev/stdout}"

args=()
if [[ -n "${GRACE_DAYS:-}" ]]; then
    args+=(--grace-days "$GRACE_DAYS")
fi
while IFS= read -r pattern; do
    if [[ -n "${pattern//[[:space:]]/}" ]]; then
        args+=(--keep "$pattern")
    fi
done <<< "${KEEP:-}"

case "${MODE:-}" in
    report)
        "$PY" "$HERE/branch_cleanup_report.py" report "${args[@]}" \
            --choices-output "${GITHUB_OUTPUT:-/dev/null}" >> "$SUMMARY"
        ;;
    delete)
        if [[ -z "${CHOICES:-}" ]]; then
            echo "[branch-cleanup-report] ERROR: mode delete needs the choices input, from the report step's output" >&2
            exit 2
        fi
        if [[ -z "${ENVIRONMENT:-}" ]]; then
            echo "[branch-cleanup-report] ERROR: mode delete needs the environment input" >&2
            exit 2
        fi
        "$PY" "$HERE/branch_cleanup_report.py" delete "${args[@]}" \
            --environment "$ENVIRONMENT" --choices "$CHOICES" >> "$SUMMARY"
        ;;
    *)
        echo "[branch-cleanup-report] ERROR: mode must be report or delete, not '${MODE:-}'" >&2
        exit 2
        ;;
esac
