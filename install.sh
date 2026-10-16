#!/bin/sh
# install.sh: install grok-cursor for the current user.
#   * copies the hook, CLI and MCP server into ~/.grok-cursor/bin (+ skill)
#   * merges 5 entries into ~/.cursor/hooks.json (backs up the original,
#     keeps every other hook, safe to run again)
#   * migrates an old "grok-bridge" install (hooks.json entries + ~/.grok-bridge data)
# Usage: ./install.sh [--dry-run]
# Env:   GROK_CURSOR_HOME (default ~/.grok-cursor), CURSOR_HOME (default ~/.cursor)
set -eu

DRY_RUN=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN="--dry-run" ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

SRC_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
GC_HOME=${GROK_CURSOR_HOME:-"$HOME/.grok-cursor"}
LEGACY_HOME="$HOME/.grok-bridge"
CURSOR_DIR=${CURSOR_HOME:-"$HOME/.cursor"}
HOOKS_FILE="$CURSOR_DIR/hooks.json"

if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 7))' >/dev/null 2>&1; then
  echo "grok-cursor: python3 (3.7+) is required. On macOS run: xcode-select --install" >&2
  exit 1
fi

if [ -n "$DRY_RUN" ]; then
  if [ -d "$LEGACY_HOME" ] && [ ! -e "$GC_HOME" ]; then
    echo "[dry-run] would migrate old data $LEGACY_HOME -> $GC_HOME"
  fi
  echo "[dry-run] would copy hook + CLI + MCP server into $GC_HOME/bin and the skill into $GC_HOME/skills"
  exec python3 "$SRC_DIR/lib/merge_hooks.py" install "$HOOKS_FILE" "$GC_HOME/bin/grok-cursor-hook" --dry-run
fi

# --- migrate a previous "grok-bridge" install (data only; hooks.json is handled below)
if [ -d "$LEGACY_HOME" ]; then
  if [ ! -e "$GC_HOME" ]; then
    mv "$LEGACY_HOME" "$GC_HOME"
    rm -rf "${GC_HOME:?}/bin"          # old scripts; fresh copies are installed below
    python3 - "$GC_HOME" "$LEGACY_HOME" <<'PYEOF'
import json, os, sys
home, old = sys.argv[1], sys.argv[2]
def fix(entry):
    if isinstance(entry, dict) and str(entry.get("chat_file", "")).startswith(old + os.sep):
        entry["chat_file"] = home + entry["chat_file"][len(old):]
for name in ("latest.json", "index.json"):
    path = os.path.join(home, name)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        continue
    if name == "latest.json":
        fix(data)
    else:
        for entry in data.get("conversations") or []:
            fix(entry)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
PYEOF
    echo "migrated old grok-bridge data: $LEGACY_HOME -> $GC_HOME"
  else
    echo "note: old grok-bridge data left in $LEGACY_HOME ($GC_HOME already exists); delete it when you no longer need it"
  fi
fi

umask 077
mkdir -p "$GC_HOME/bin" "$GC_HOME/chats"
chmod 700 "$GC_HOME" "$GC_HOME/chats"
cp "$SRC_DIR/hooks/grok-cursor-hook" "$GC_HOME/bin/grok-cursor-hook"
cp "$SRC_DIR/bin/grok-cursor" "$GC_HOME/bin/grok-cursor"
cp "$SRC_DIR/lib/merge_hooks.py" "$GC_HOME/bin/merge_hooks.py"
cp "$SRC_DIR/lib/grok_cursor_mcp.py" "$GC_HOME/bin/grok-cursor-mcp"
cp "$SRC_DIR/lib/gc_send.py" "$GC_HOME/bin/gc_send.py"
cp "$SRC_DIR/lib/gc_watch.py" "$GC_HOME/bin/gc_watch.py"
cp "$SRC_DIR/lib/gc_risk.py" "$GC_HOME/bin/gc_risk.py"
cp "$SRC_DIR/lib/gc_events.py" "$GC_HOME/bin/gc_events.py"
cp "$SRC_DIR/lib/gc_gate.py" "$GC_HOME/bin/gc_gate.py"
cp "$SRC_DIR/lib/gc_card_events.py" "$GC_HOME/bin/gc_card_events.py"
cp "$SRC_DIR/lib/gc_resilience.py" "$GC_HOME/bin/gc_resilience.py"
cp "$SRC_DIR/lib/gc_outbox.py" "$GC_HOME/bin/gc_outbox.py"
cp "$SRC_DIR/lib/gc_awake.py" "$GC_HOME/bin/gc_awake.py"
cp "$SRC_DIR/uninstall.sh" "$GC_HOME/bin/uninstall.sh"
mkdir -p "$GC_HOME/docs"
cp "$SRC_DIR/docs/PROBES.md" "$GC_HOME/docs/PROBES.md"   # gc_gate reads hold + resume facts from here
mkdir -p "$GC_HOME/skills/grok-cursor"
cp "$SRC_DIR/skills/grok-cursor/SKILL.md" "$GC_HOME/skills/grok-cursor/SKILL.md"
chmod 755 "$GC_HOME/bin/grok-cursor-hook" "$GC_HOME/bin/grok-cursor" "$GC_HOME/bin/grok-cursor-mcp" "$GC_HOME/bin/uninstall.sh"
echo "installed hook + CLI into $GC_HOME/bin"

# Registers our 5 hooks and drops any old grok-bridge-hook entries; other hooks are kept.
mkdir -p "$CURSOR_DIR"
python3 "$SRC_DIR/lib/merge_hooks.py" install "$HOOKS_FILE" "$GC_HOME/bin/grok-cursor-hook"

# The watcher keeps the python it started with. Restart a loaded job so this
# install's modules are the ones running. The plist is not rewritten, so
# dry-run and scratch flags stay.
# Same order as lib/gc_watch.py resolve_label: env, then $GC_HOME/watch-label, then the default.
LABEL="${GROK_CURSOR_WATCH_LABEL:-$(tr -d '[:space:]' < "$GC_HOME/watch-label" 2>/dev/null || true)}"
case "$LABEL" in ''|*[!A-Za-z0-9._-]*) LABEL=com.grok-cursor.watch ;; esac
DOMAIN="gui/$(id -u)"
PLIST="${GROK_CURSOR_LAUNCH_AGENTS:-$HOME/Library/LaunchAgents}/$LABEL.plist"
PLIST_HOME=$(plutil -extract EnvironmentVariables.GROK_CURSOR_HOME raw -o - "$PLIST" 2>/dev/null || true)
if [ "${GROK_CURSOR_SKIP_WATCH_RESTART:-0}" != 1 ] && [ "$PLIST_HOME" = "$GC_HOME" ] \
   && launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  launchctl kickstart -k "$DOMAIN/$LABEL"
  echo "restarted $LABEL"
fi

cat <<EOF

Done. Cursor reloads hooks.json automatically (restart Cursor if it doesn't).
Check it:   $GC_HOME/bin/grok-cursor doctor
Then send one message in a Cursor agent chat and run:
            $GC_HOME/bin/grok-cursor latest --turns 6

Optional:
  MCP server:  add to ~/.cursor/mcp.json (or your Claude config):
               {"mcpServers": {"grok-cursor": {"command": "python3", "args": ["$GC_HOME/bin/grok-cursor-mcp"]}}}
  Skill:       $GC_HOME/skills/grok-cursor/SKILL.md (copy into your agent's skills folder)
  send:        run $GC_HOME/bin/grok-cursor doctor --send (needs Accessibility; see README)
EOF
