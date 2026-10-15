#!/usr/bin/env python3
"""Add or remove grok-cursor entries in a Cursor hooks.json without touching
anything else. Used by install.sh / uninstall.sh. Python 3 stdlib only.

  merge_hooks.py install   <hooks.json> <hook-script-path> [--dry-run]
  merge_hooks.py uninstall <hooks.json> [--dry-run]

Our entries are recognised by their command containing "grok-cursor-hook"
(or "grok-bridge-hook", the old name; those are migrated/removed too).
A timestamped backup (hooks.json.bak-YYYYmmdd-HHMMSS) is written before any
change; if nothing would change, the file is left untouched (idempotent).
"""
import datetime
import json
import math
import os
import shlex
import shutil
import sys
import tempfile

EVENTS = ("sessionStart", "beforeSubmitPrompt", "afterAgentResponse", "stop", "sessionEnd")
# Gate entries. preToolUse is matched to the file tools; the hook ignores the rest.
GATE = (
    ("beforeShellExecution", ""),
    ("beforeMCPExecution", ""),
    ("beforeReadFile", ""),
    ("preToolUse", "Read|Write|Delete|Grep|Glob|ListDir"),
)
# Cursor 3.24 rejects the whole hooks file if any type is unknown. beforeToolCall
# is not in this set; the notifier equivalent that Cursor accepts is preToolUse.
ACCEPTED_HOOK_TYPES = frozenset({
    "beforeShellExecution", "beforeMCPExecution", "afterShellExecution", "afterMCPExecution",
    "beforeReadFile", "afterFileEdit", "beforeTabFileRead", "afterTabFileEdit",
    "stop", "beforeSubmitPrompt", "afterAgentResponse", "afterAgentThought",
    "sessionStart", "sessionEnd", "preCompact", "subagentStart", "subagentStop",
    "preToolUse", "postToolUse", "postToolUseFailure", "workspaceOpen",
})
MARK = "grok-cursor-hook"
LEGACY_MARKS = ("grok-bridge-hook",)   # previous name of this project
TIMEOUT_SECONDS = 10


def ours(item):
    if not isinstance(item, dict):
        return False
    command = str(item.get("command", ""))
    return any(m in command for m in (MARK,) + LEGACY_MARKS)


def load(path):
    if not os.path.exists(path):
        return {"version": 1, "hooks": {}}, False
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if not text.strip():
        return {"version": 1, "hooks": {}}, True
    cfg = json.loads(text)  # invalid JSON -> abort, never overwrite user config
    if not isinstance(cfg, dict):
        raise ValueError("top level of hooks.json is not an object")
    if "hooks" not in cfg:
        cfg["hooks"] = {}
    if not isinstance(cfg["hooks"], dict):
        raise ValueError('"hooks" in hooks.json is not an object')
    cfg.setdefault("version", 1)
    return cfg, True


def gate_timeout():
    """hold + 5, from config.json or PROBES.md. 65 if neither can be read."""
    try:
        here = os.path.dirname(os.path.realpath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import gc_events
        import gc_gate
        return int(math.ceil(gc_gate.hold_seconds(gc_events.data_home()) + 5))
    except Exception:
        return 65

def _entry(script, event, timeout, fail_closed=False, matcher="", loop_limit=None):
    item = {"command": "%s %s" % (shlex.quote(script), event), "timeout": timeout}
    if fail_closed:
        item["failClosed"] = True
    if matcher:
        item["matcher"] = matcher
    if loop_limit is not None:
        item["loop_limit"] = loop_limit
    return item

def _slot(hooks, event):
    items = hooks.get(event)
    if not isinstance(items, list):
        if items is not None:
            raise ValueError("hooks.%s is not a list" % event)
        items = []
    return [i for i in items if not ours(i)]

def install(cfg, script):
    wanted = EVENTS + tuple(event for event, _m in GATE)
    unknown = [event for event in wanted if event not in ACCEPTED_HOOK_TYPES]
    if unknown:
        raise ValueError("refusing hook types this Cursor version rejects: %s" % ", ".join(unknown))
    hooks = cfg["hooks"]
    for event in EVENTS:
        items = _slot(hooks, event)
        # stop may re-prompt after a late allow; 10 beats Cursor's default of 5.
        items.append(_entry(script, event, TIMEOUT_SECONDS,
                            loop_limit=10 if event == "stop" else None))
        hooks[event] = items
    timeout = gate_timeout()
    for event, matcher in GATE:
        items = _slot(hooks, event)
        items.append(_entry(script, event, timeout, fail_closed=True, matcher=matcher))
        hooks[event] = items
    return cfg


def uninstall(cfg):
    hooks = cfg["hooks"]
    for event in list(hooks.keys()):
        items = hooks[event]
        if not isinstance(items, list) or not any(ours(i) for i in items):
            continue
        kept = [i for i in items if not ours(i)]
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]  # we emptied it; drop the key we created
    return cfg


def write_atomic(path, cfg):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".hooks-", dir=directory)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    if os.path.exists(path):
        shutil.copymode(path, tmp)
    else:
        os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def main(argv):
    dry = "--dry-run" in argv
    argv = [a for a in argv if a != "--dry-run"]
    if len(argv) < 2 or argv[0] not in ("install", "uninstall"):
        sys.stderr.write(__doc__)
        return 2
    action, path = argv[0], argv[1]
    try:
        cfg, existed = load(path)
        before = json.dumps(cfg, sort_keys=True)
        if action == "install":
            if len(argv) < 3:
                sys.stderr.write("install needs the hook script path\n")
                return 2
            cfg = install(cfg, argv[2])
        else:
            cfg = uninstall(cfg)
    except (ValueError, OSError) as exc:
        sys.stderr.write("grok-cursor: refusing to edit %s: %s\n" % (path, exc))
        return 1
    if json.dumps(cfg, sort_keys=True) == before:
        print("hooks.json already up to date: %s" % path)
        return 0
    if dry:
        print("[dry-run] would write %s:" % path)
        print(json.dumps(cfg, indent=2, ensure_ascii=False))
        return 0
    if existed:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = "%s.bak-%s" % (path, stamp)
        n = 1
        while os.path.exists(backup):
            n += 1
            backup = "%s.bak-%s-%d" % (path, stamp, n)
        shutil.copy2(path, backup)
        print("backed up %s -> %s" % (path, backup))
    write_atomic(path, cfg)
    print("%s grok-cursor hooks in %s" % ("registered" if action == "install" else "removed", path))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
