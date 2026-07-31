#!/usr/bin/env bash
# route dispatch hook -> beads (https://github.com/gastownhall/beads).
#
# Installed into <config dir>/dispatch-hooks.d/ so a dispatch that is already
# attached to a real work item records its routing decision and its outcome
# evidence on that item.
#
# `ROUTE_BEAD=<id>` is the ONLY path. With it unset the hook records nothing
# and exits 0. It deliberately does NOT create a bead per dispatch:
#
#   * A bead this hook created is closed by nobody but the reconciler reading
#     it back, so "closed -> accepted" would encode no human judgement — a
#     circular reward signal. Closure of a real work item is genuine evidence.
#   * Auto-creation would write the full task text as --title/--description to
#     a shared network tracker on every dispatch, ignoring route's own
#     `data_class` sensitivity classification. On the ROUTE_BEAD path only
#     --metadata is written, so no task text ever leaves the machine.
#
# The outcome is deliberately NOT recorded here — route-reconcile.py owns the
# bandit reward, so there is exactly one writer. This hook only records
# evidence.
#
# Fail open, always: a missing bd, a non-beads cwd, or an unreachable beads
# server must never stop a dispatch.
#
# No `-e` here, deliberately: with `-e`, a failing command in the middle of
# an expression (e.g. the invalid-JSON `jq` path) would exit non-zero before
# `skip` gets to run, turning a fail-open path into a hard failure. Do not
# add `-e` in a later "tighten the shell flags" pass.
set -uo pipefail

BD_TIMEOUT_S="${ROUTE_BEAD_BD_TIMEOUT:-10}"

skip() { # skip <reason>
  echo "route-bead-hook: no bead ($1) — dispatching anyway" >&2
  exit 0
}

# Silent counterpart to skip(), for the case where the `decision` event
# already explained itself on stderr: with no ROUTE_BEAD set (the common
# case) `decision` prints one line and returns no hook_context, so a second
# line from `complete` would only ever restate it. One line per dispatch.
quiet_skip() {
  exit 0
}

payload=$(cat)
[ -n "${payload//[[:space:]]/}" ] || skip "empty payload"

command -v jq >/dev/null 2>&1 || skip "jq not installed"

event=$(printf '%s' "$payload" | jq -r '.event // ""')

# --- guards ------------------------------------------------------------
command -v bd >/dev/null 2>&1 || skip "bd not installed"

# A beads workspace is a git repo with a *valid* .beads/metadata.json at its
# root. "Valid" means: parses as JSON, and carries a non-empty `dolt_database`
# — the field bd itself treats as authoritative. A present-but-empty or
# placeholder metadata.json (e.g. "{}") is not a beads workspace: real `bd`
# will silently auto-init a throwaway database rather than error, which would
# pollute shared infrastructure with writes nobody will ever look at again.
valid_beads_workspace() { # valid_beads_workspace <dir>
  local meta
  meta="$1/.beads/metadata.json"
  [ -f "$meta" ] || return 1
  jq -e '(.dolt_database // "") | length > 0' "$meta" >/dev/null 2>&1
}

repo_root=$(git rev-parse --show-toplevel 2>/dev/null) \
  || repo_root=""
if [ -z "$repo_root" ]; then
  # Not a git repo: accept a .beads dir in cwd so the hook is testable and
  # works in non-git beads workspaces.
  valid_beads_workspace "$PWD" || skip "no beads workspace"
  repo_root="$PWD"
elif ! valid_beads_workspace "$repo_root"; then
  skip "no beads workspace"
fi

# Bead ids are conservative: reject anything that is not a single clean token,
# and reject a leading hyphen outright — `ROUTE_BEAD=--force` is a charset-
# clean string that `bd` would read as a flag, not as an id.
valid_id() {
  case "$1" in
    ''|-*|*[!A-Za-z0-9_.-]*) return 1 ;;
    *) return 0 ;;
  esac
}

# --- complete ------------------------------------------------------------
# Records dispatch result EVIDENCE only. The reconciler reads this evidence
# and decides the bandit reward/outcome; this hook must never close, relabel,
# or otherwise change the bead's status — one writer for reward attribution,
# always.
#
# `bd update --metadata` MERGES the JSON object it is given into the issue's
# existing metadata (shallow, top-level keys only) rather than replacing it
# wholesale. So this call is safe to run after the `decision` event's
# `bd update --metadata` without carrying the routing fields (cell/arm/
# shape/tier/why/eligible) forward by hand — they are preserved automatically.
handle_complete() {
  local bead_id exit_code wall note meta
  bead_id=$(printf '%s' "$payload" | jq -r '.hook_context // ""')
  # An empty hook_context means `decision` skipped, and it already said why.
  [ -n "$bead_id" ] || quiet_skip
  # No whitespace-strip before validating: valid_id()'s charset check already
  # rejects any embedded whitespace outright (space, tab, newline are all
  # outside [A-Za-z0-9_.-]), so a noisy hook_context fails validation whether
  # or not it is stripped first. Do not add a strip step here — squashing
  # whitespace can manufacture a single, charset-clean-looking bogus token out
  # of an id plus trailing chatter.
  valid_id "$bead_id" || skip "no usable bead id in hook_context"

  exit_code=$(printf '%s' "$payload" | jq -r '.exit_code // 0')
  wall=$(printf '%s' "$payload" | jq -r '.wall_clock_s // 0')

  note="route dispatch finished: exit_code=$exit_code wall_clock_s=$wall"
  # `with_entries(select(.value != null))` drops keys the payload did not
  # supply. Do not use `empty` inside the object instead: `{a: empty}` makes
  # jq emit NO object at all, and the hook would silently record nothing.
  meta=$(printf '%s' "$payload" | jq -c '{
    exit_code:    (.exit_code // 0),
    wall_clock_s: (.wall_clock_s // 0),
    arm:          (.plan.chosen // ""),
    exception:    .exception
  } | with_entries(select(.value != null))' 2>/dev/null) || skip "malformed plan"

  timeout "$BD_TIMEOUT_S" bd update "$bead_id" \
    --append-notes "$note" \
    --metadata "$meta" >/dev/null 2>&1 \
    || skip "bd update failed for $bead_id"
}

case "$event" in
  decision) ;;
  complete) handle_complete; exit 0 ;;
  *) skip "unknown event '$event'" ;;
esac

# --- decision ----------------------------------------------------------
# ROUTE_BEAD is checked first: with it unset there is nothing to do, and this
# is the common case, so no further work (and no `bd` call at all) happens.
[ -n "${ROUTE_BEAD:-}" ] || skip "no ROUTE_BEAD set"
valid_id "$ROUTE_BEAD" || skip "ROUTE_BEAD is not a valid id"

# Only routing metadata is written — never the task text. Every field is read
# out of the payload by `jq -r`/`jq -c` and passed as a single argv element;
# nothing is ever `eval`ed or interpolated into a shell command line, so quote
# and metacharacter content in the plan is inert. jq's JSON encoding also
# escapes control characters (\r, ESC) inside the emitted object, so no raw
# terminal escape can reach the tracker.
metadata=$(printf '%s' "$payload" | jq -c '{
  cell:        (.plan.cell     // ""),
  shape:       (.plan.shape    // ""),
  tier:        (.plan.tier     // ""),
  arm:         (.plan.chosen   // ""),
  why:         (.plan.why      // ""),
  eligible:    (.plan.eligible // []),
  decision_ts: .decision_ts
} | with_entries(select(.value != null))' 2>/dev/null) || skip "malformed plan"

timeout "$BD_TIMEOUT_S" bd update "$ROUTE_BEAD" \
  --metadata "$metadata" >/dev/null 2>&1 \
  || skip "bd update failed for $ROUTE_BEAD"
timeout "$BD_TIMEOUT_S" bd label add "$ROUTE_BEAD" route:pending >/dev/null 2>&1 || true
printf '%s' "$ROUTE_BEAD"
