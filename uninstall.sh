#!/bin/sh
# uninstall.sh: remove grok-cursor-link.
#   * removes ONLY grok-cursor entries from ~/.cursor/hooks.json (backup first)
#   * removes ~/.grok-cursor/bin, skills and docs (sends.log is data)
#   * keeps recorded chats unless --purge is given
# Usage: ./uninstall.sh [--purge] [--dry-run]
set -eu

PURGE=0
DRY_RUN=""
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    --dry-run) DRY_RUN="--dry-run" ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

SRC_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
GC_HOME=${GROK_CURSOR_HOME:-"$HOME/.grok-cursor"}
CURSOR_DIR=${CURSOR_HOME:-"$HOME/.cursor"}
HOOKS_FILE="$CURSOR_DIR/hooks.json"

MERGE="$SRC_DIR/lib/merge_hooks.py"
[ -f "$MERGE" ] || MERGE="$SRC_DIR/merge_hooks.py"   # running the installed copy

if [ -f "$HOOKS_FILE" ]; then
  if [ -f "$MERGE" ]; then
    if [ -n "$DRY_RUN" ]; then
      python3 "$MERGE" uninstall "$HOOKS_FILE" --dry-run
    else
      python3 "$MERGE" uninstall "$HOOKS_FILE"
    fi
  else
    echo "grok-cursor: merge_hooks.py not found; remove the grok-cursor-hook entries from $HOOKS_FILE by hand" >&2
    exit 1
  fi
else
  echo "no $HOOKS_FILE; nothing to unregister"
fi

LABEL="${GROK_CURSOR_WATCH_LABEL:-com.grok-cursor.watch}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
if [ -f "$PLIST" ]; then
  if [ -n "$DRY_RUN" ]; then
    echo "[dry-run] would stop the watch agent and remove $PLIST"
  else
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "removed watch agent $PLIST"
  fi
fi

if [ -n "$DRY_RUN" ]; then
  echo "[dry-run] would remove $GC_HOME/bin, $GC_HOME/skills and $GC_HOME/docs$( [ "$PURGE" = 1 ] && echo " and all data in $GC_HOME")"
  echo "[dry-run] would remove $GC_HOME/known-version and $GC_HOME/safe-mode"
  exit 0
fi

case "$GC_HOME" in
  ""|/|"$HOME"|"$HOME/") echo "grok-cursor: refusing to delete suspicious GC_HOME=$GC_HOME" >&2; exit 1 ;;
esac
rm -rf "${GC_HOME:?}/bin" "${GC_HOME:?}/skills" "${GC_HOME:?}/docs"
rm -f "${GC_HOME:?}/known-version" "${GC_HOME:?}/safe-mode"
if [ "$PURGE" = 1 ]; then
  rm -rf "${GC_HOME:?}"
  echo "removed $GC_HOME (including recorded chats)"
else
  echo "removed $GC_HOME/bin; recorded chats kept in $GC_HOME (use --purge to delete)"
fi
