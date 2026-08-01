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

# --- decision: offered context ------------------------------------------
# A dispatched arm starts cold. beads already holds what this project learned
# the hard way — `bd remember` notes written by whoever hit it last — and the
# arm about to redo that work cannot see any of it. This builds a bounded
# excerpt and offers it to route as `prepend`.
#
# Offered, not imposed: route drops it for non-`open` work and caps it. The
# data_class check below is not redundant with route's — it is what stops the
# memories being READ at all for sensitive work, so nothing is assembled that
# then has to be trusted to be discarded.
#
# Bounded twice over: at most MAX_TERMS searches, at most MAX_MEMORIES hits,
# and a hard character budget. A local arm may be serving a 32k window, and a
# preamble that crowds out the task it exists to inform is worse than none.
CONTEXT_MAX_CHARS="${ROUTE_BEAD_CONTEXT_MAX_CHARS:-3000}"
CONTEXT_MAX_TERMS="${ROUTE_BEAD_CONTEXT_MAX_TERMS:-5}"
CONTEXT_MAX_MEMORIES="${ROUTE_BEAD_CONTEXT_MAX_MEMORIES:-4}"

# Search terms out of the task text. Deliberately crude: distinctive words are
# long ones, and `bd memories` is a substring search, so precision costs
# nothing a cap does not already bound. Everything stays in a pipeline —
# the task text is never interpolated into a command line.
search_terms() {
  printf '%s' "$payload" \
    | jq -r '.task // ""' \
    | tr '[:upper:]' '[:lower:]' \
    | tr -c 'a-z0-9' '\n' \
    | awk 'length($0) >= 5' \
    | grep -Ev '^(about|after|again|below|could|every|first|other|should|their|there|these|thing|those|which|while|would|write|using|make|makes)$' \
    | awk '!seen[$0]++' \
    | head -n "$CONTEXT_MAX_TERMS"
}

# One `bd memories` call per term, merged into a single {key: text} object.
# `--global` is queried too: the cross-repo memories are exactly the
# infrastructure facts an arm is likeliest to be missing.
collect_memories() {
  local term
  {
    while IFS= read -r term; do
      [ -n "$term" ] || continue
      timeout "$BD_TIMEOUT_S" bd memories "$term" --json 2>/dev/null
      timeout "$BD_TIMEOUT_S" bd memories "$term" --global --json 2>/dev/null
    done < <(search_terms)
  } | jq -s --argjson limit "$CONTEXT_MAX_MEMORIES" '
        (map(select(type == "object")) | add // {})
        | del(.schema_version)
        | to_entries
        | map(select(.value | type == "string"))
        | .[:$limit]
      ' 2>/dev/null
}

# Markdown, because every arm reads it and no arm needs it explained. The
# heading says where this came from and that it is background, so an arm does
# not mistake a memory for the instruction.
#
# The budget is divided BETWEEN the hits rather than spent front-to-back: a
# single `bd remember` note routinely runs past a thousand characters, so
# filling greedily would seat the first memory and truncate the rest out of
# existence. Each is cut to its share and marked with an ellipsis, so the arm
# sees every match it was given and can tell which ones it is seeing only the
# head of.
render_context() {
  jq -r --argjson budget "$CONTEXT_MAX_CHARS" '
    if (. | length) == 0 then ""
    else
      (($budget / length | floor) - 120) as $share
      | "## Project memory (beads)\n"
      + "Background from this project'"'"'s tracker. The task follows.\n\n"
      + (
          map(
            "- **\(.key)**: "
            + (if ($share > 0) and ((.value | length) > $share)
               then (.value[:$share] + " […]")
               else .value end)
          )
          | join("\n")
        )
      | .[:$budget]
    end
  ' 2>/dev/null
}

prepend=""
data_class=$(printf '%s' "$payload" | jq -r '.plan.data_class // ""')
if [ "$data_class" = "open" ]; then
  prepend=$(collect_memories | render_context)
fi

# --- decision: bead metadata --------------------------------------------
# With ROUTE_BEAD unset there is no bead to annotate — but an offer may still
# have been built above, and it is worth making. Emit it and stop.
emit() { # emit <hook_context>
  if [ -n "${prepend//[[:space:]]/}" ]; then
    jq -cn --arg c "$1" --arg p "$prepend" '{hook_context: $c, prepend: $p}'
  elif [ -n "$1" ]; then
    printf '%s' "$1"
  fi
}

if [ -z "${ROUTE_BEAD:-}" ]; then
  emit ""
  skip "no ROUTE_BEAD set"
fi
valid_id "$ROUTE_BEAD" || { emit ""; skip "ROUTE_BEAD is not a valid id"; }

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
} | with_entries(select(.value != null))' 2>/dev/null) \
  || { emit ""; skip "malformed plan"; }

timeout "$BD_TIMEOUT_S" bd update "$ROUTE_BEAD" \
  --metadata "$metadata" >/dev/null 2>&1 \
  || { emit ""; skip "bd update failed for $ROUTE_BEAD"; }
timeout "$BD_TIMEOUT_S" bd label add "$ROUTE_BEAD" route:pending >/dev/null 2>&1 || true
# The bead id is the correlation token `complete` reads back. It is emitted
# bare when there is no offer to make, so a beads install with no memories
# behaves exactly as it did before this path existed.
emit "$ROUTE_BEAD"
