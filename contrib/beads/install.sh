#!/usr/bin/env bash
# Install the beads integration: dispatch hook + reconciler.
#
# Gated on `bd` being on PATH. beads is a standalone Go binary, not a Python
# package, so there is no dependency either direction — route does not depend
# on beads, beads does not depend on route, and this installer is the only
# thing that knows both exist. With `bd` absent it installs nothing and exits
# 0, leaving route byte-identical to a stock install.
#
# Idempotent: re-running replaces the symlinks it owns and touches nothing
# else. Pass --uninstall to remove them.
set -euo pipefail

HOOK_NAME="50-beads"
BIN_NAME="route-reconcile"

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# Mirrors route.storage.config_dir(): ROUTE_CONFIG_DIR, else
# XDG_CONFIG_HOME/route, else ~/.config/route. Kept in sync by hand — this
# script must not import route, because it has to work before (and without)
# route being installed into the current interpreter.
config_dir() {
  if [ -n "${ROUTE_CONFIG_DIR:-}" ]; then
    printf '%s' "$ROUTE_CONFIG_DIR"
  else
    printf '%s/route' "${XDG_CONFIG_HOME:-$HOME/.config}"
  fi
}

bin_dir() {
  printf '%s' "${ROUTE_BEADS_BIN_DIR:-$HOME/.local/bin}"
}

hook_path="$(config_dir)/dispatch-hooks.d/$HOOK_NAME"
bin_path="$(bin_dir)/$BIN_NAME"

if [ "${1:-}" = "--uninstall" ]; then
  rm -f "$hook_path" "$bin_path"
  echo "beads integration removed"
  exit 0
fi

if ! command -v bd >/dev/null 2>&1; then
  echo "install.sh: bd not on PATH — beads integration not installed" >&2
  echo "install.sh: install beads first, then re-run this script" >&2
  exit 0
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "install.sh: jq not on PATH — the dispatch hook needs it" >&2
  exit 1
fi

chmod +x "$here/route-bead-hook.sh" "$here/route-reconcile" "$here/route_reconcile.py"

mkdir -p "$(dirname "$hook_path")" "$(bin_dir)"
ln -sfn "$here/route-bead-hook.sh" "$hook_path"
ln -sfn "$here/route-reconcile" "$bin_path"

echo "dispatch hook: $hook_path"
echo "reconciler:    $bin_path"

case ":$PATH:" in
  *":$(bin_dir):"*) ;;
  *) echo "note: $(bin_dir) is not on PATH" >&2 ;;
esac

cat <<'EOF'

Next: run the reconciler from a beads workspace on a schedule (a session-start
hook, a cron entry, or by hand) so closed work turns into bandit rewards:

    route-reconcile --dry-run     # print the verdicts, change nothing
    route-reconcile               # record them

Attach a dispatch to a work item by setting ROUTE_BEAD:

    ROUTE_BEAD=proj-42 auto "refactor the storage layer"
EOF
