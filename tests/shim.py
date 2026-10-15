#!/usr/bin/env python3
"""Test double for macOS tools (osascript, ioreg, pgrep, open, caffeinate, pbcopy,
pbpaste, agent). Installed under each tool's name on PATH. It simulates a
Cursor window through the same JSON file the FakeAX backend reads, so a
paste really lands in the focused input, and a Return really "submits": it
writes a hook-style index entry like Cursor's beforeSubmitPrompt would.
Never used outside tests."""
import datetime
import json
import os
import sys
from urllib.parse import unquote

name = os.path.basename(sys.argv[0])
args = sys.argv[1:]
E = os.environ.get
with open(E("SHIM_LOG"), "a", encoding="utf-8") as fh:
    fh.write(json.dumps([name] + args) + "\n")

STATE = E("GROK_CURSOR_AX_FAKE")
CLIP = os.path.join(E("HOME"), ".shim-clipboard")


def load():
    with open(STATE, encoding="utf-8") as fh:
        return json.load(fh)


def save(st):
    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump(st, fh)


def nodes(st):
    stack = list(st["windows"])
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n.get("children", []))


def focused(st):
    return next((n for n in nodes(st) if n.get("id") == st.get("focused")), None)


def type_into_focused(text):
    st = load()
    f = focused(st)
    if f is not None and f.get("role") in ("AXTextArea", "AXTextField"):
        f["value"] = (f.get("value") or "") + text
    else:
        st.setdefault("stray", []).append(text)
    save(st)


def submit():
    st = load()
    f = focused(st)
    if f is None or "chat" not in (f.get("kind") or "") or not f.get("value"):
        st.setdefault("stray_returns", 0)
        st["stray_returns"] += 1        # a Return that would have hit an editor/file
        save(st)
        return
    text = f["value"]
    f["value"] = ""
    st.setdefault("submitted", []).append({"conv": st.get("active_conv"), "text": text})
    save(st)
    if E("SHIM_NO_HOOK"):
        return
    home = os.path.join(E("HOME"), ".grok-cursor")
    os.makedirs(home, exist_ok=True)
    idx = os.path.join(home, "index.json")
    try:
        with open(idx) as fh:
            data = json.load(fh)
    except Exception:
        data = {"conversations": []}
    conv = E("SHIM_HOOK_CONV") or st.get("active_conv")
    data["conversations"].insert(0, {
        "conversation_id": conv,
        "last_prompt_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "last_prompt_preview": " ".join(text.split())[:500]})
    with open(idx, "w") as fh:
        json.dump(data, fh)
    # Cursor's own transcript for this chat id. Delivery checks this file, not the hook index.
    root = os.path.join(E("HOME"), ".cursor", "projects", "shim", "agent-transcripts", conv)
    os.makedirs(root, exist_ok=True)
    line = {"role": "user", "message": {"content": [{"type": "text", "text": text}]}}
    with open(os.path.join(root, conv + ".jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")


def osascript(script):
    if "frontmost is true" in script:
        if E("SHIM_OSA_DENIED"):
            sys.stderr.write("Not authorized to send Apple events to System Events. (-1743)\n")
            return 1
        front = E("SHIM_FRONT", "Cursor")
        bid = "com.todesktop.230313mzl4w4u92" if front == "Cursor" else "com.apple.Safari"
        if E("SHIM_FRONT_AFTER_PASTE") and load().get("pasted"):
            front, bid = E("SHIM_FRONT_AFTER_PASTE"), "com.apple.finder"
        print("%s|%s" % (front, bid) if "bundle identifier" in script else front)
        return 0
    if script.strip() == "clipboard info" and E("SHIM_CLIP_INFO_FAIL"):
        return 1
    if script.strip() == "clipboard info":
        print(E("SHIM_CLIP_INFO", "\u00abclass utf8\u00bb, 5, string, 5"))
        return 0
    if "to activate" in script:
        return 0
    if E("SHIM_KEY_FAIL") and "key code" in script:
        sys.stderr.write("System Events got an error: osascript is not allowed to send keystrokes. (1002)\n")
        return 1
    if "key code 36" in script:
        submit()
        return 0
    if 'keystroke "v"' in script:
        if not E("SHIM_PASTE_NOOP"):
            with open(CLIP, encoding="utf-8") as fh:
                type_into_focused(fh.read())
            st = load()
            st["pasted"] = True
            save(st)
        return 0
    if 'keystroke "]"' in script:
        st = load()
        st.setdefault("steps", 0)
        st["steps"] += 1
        if not E("SHIM_STEP_NOOP"):
            bar = next(n for n in nodes(st) if n.get("id") == "tabbar")
            tabs = [t for t in bar["children"] if "conv" in t]
            i = next((k for k, t in enumerate(tabs) if t.get("selected")), 0)
            nxt = tabs[(i + 1) % len(tabs)]
            for t in tabs:
                t["selected"] = t is nxt
            st["active_conv"] = nxt["conv"]
        save(st)
        return 0
    if script.strip() == "clipboard info" and E("SHIM_CLIP_INFO_FAIL"):
        return 1
    if 'keystroke "n"' in script:
        st = load()
        st["active_conv"] = "new-conv"
        save(st)
        return 0
    return 0


def main():
    if name == "caffeinate":
        return 0
    if name == "ioreg":
        if E("SHIM_IOREG_FAIL"):
            return 1
        import plistlib
        user = {"kCGSSessionUserNameKey": E("USER", "tester"), "kCGSSessionUserIDKey": os.getuid(),
                "kCGSSessionOnConsoleKey": not E("SHIM_OFF_CONSOLE")}
        if E("SHIM_LOCKED"):
            user["CGSSessionScreenIsLocked"] = True
        sys.stdout.write(plistlib.dumps({"IOConsoleUsers": [user],
                                         "IOConsoleLocked": bool(E("SHIM_LOCKED"))}).decode())
        return 0
    if name == "pgrep":
        rc = int(E("SHIM_PGREP_RC", "0"))
        if rc == 0:
            print(4242)
        return rc
    if name == "open":
        if args and args[0].startswith("cursor://"):
            rc = int(E("SHIM_OPEN_LINK_RC", "0"))
            if rc == 0 and not E("SHIM_DEEPLINK_NOOP"):
                type_into_focused(unquote(args[0].split("text=", 1)[1]))
            return rc
        return int(E("SHIM_OPEN_RC", "0"))
    if name == "osascript":
        return osascript(args[1] if len(args) > 1 else "")
    if name == "pbcopy":
        with open(CLIP, "w", encoding="utf-8") as fh:
            fh.write(sys.stdin.read())
        return 0
    if name == "pbpaste":
        if os.path.exists(CLIP):
            with open(CLIP, encoding="utf-8") as fh:
                sys.stdout.write(fh.read())
        return 0
    if name == "agent":
        if E("SHIM_AGENT_FAIL"):
            sys.stderr.write("boom\n")
            return 3
        print("agent did: %s" % args[1])
        return 0
    return 0


sys.exit(main())
