"""Version tripwire, safe mode, selftest scoring, probe dumps, fix-draft text.

No clicks. The CLI wires the commands. Safe mode makes Away treat every non-low
command as high, which the gate already denies with "stop and wait for the human".
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

EXIT_OK = 0
EXIT_DEGRADED = 40
EXIT_HOOKS = 41
CARD_FAILS = 3

# Token plus group. css_selector() turns a token into a querySelector string.
# composer, status, and input must be on screen. tab and the card groups are reported only.
COMPONENT_NAMES = ("transcript-card-footer", "transcript-card-root", "split-button")
PROBE = (
    (".composer-bar[data-composer-id]", "composer"),
    ("data-composer-status", "status"),
    (".aislash-editor-input", "input"),
    ('[contenteditable="true"].ProseMirror', "input"),
    ("[data-lexical-editor=\"true\"]", "input"),
    (".composer-input [contenteditable=\"true\"]", "input"),
    (".composer-tab", "tab"),
    ("[role=\"tab\"]", "tab"),
    (".agent-sidebar-cell", "tab"),
    (".composer-questionnaire-toolbar", "question"),
    ("composer-questionnaire-toolbar-option-label", "question"),
    ("[data-click-ready]", "question"),
    ("transcript-card-footer", "run"),
    ("split-button", "plan"),
    ("transcript-card-root", "card"),
    ("data-tool-call-id", "card"),
    ("data-tool-status", "card"),
)


def css_selector(token):
    """querySelector string. A bare word is a class, data-* is an attribute, card pieces are components."""
    token = (token or "").strip()
    if not token or token[0] in ".#[":
        return token
    if token.startswith("data-"):
        return "[%s]" % token
    if token in COMPONENT_NAMES:
        return '[data-component="%s"]' % token
    return "." + token


def probe_queries():
    return [(css_selector(token), group) for token, group in PROBE]


SELECTORS = tuple(css for css, _group in probe_queries())

# Copied by the installer besides the original list. The shell installer is patched to match.
EXTRA_MODULES = ("gc_card_events.py", "gc_resilience.py")
STATE_FILES = ("known-version", "safe-mode")

_PATH = re.compile(r"(?:~|/)[^\s\"']+")


def _read_text(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _write_private(path, text):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")


def read_known(home):
    """Last cursor version. known-version wins; the older cursor-version file is the fallback."""
    ver = _read_text(os.path.join(home, "known-version"))
    if ver:
        return ver
    return _read_text(os.path.join(home, "cursor-version"))


def note_cursor_version(home, payload, append=None):
    """Store cursor_version. On a real change, append kind cursor_updated and return {old, new}.

    The first version seen is stored and does not emit. append(kind, home=..., old=..., new=...).
    """
    payload = payload or {}
    ver = str(payload.get("cursor_version") or os.environ.get("CURSOR_VERSION") or "").strip()
    if not ver:
        return None
    old = read_known(home)
    path = os.path.join(home, "known-version")
    if old == ver and _read_text(path) == ver:
        return None
    _write_private(path, ver)
    if not old or old == ver:
        return None
    if append:
        append("cursor_updated", home=home, old=old, new=ver)
    return {"old": old, "new": ver}


def safe_on(home):
    return os.path.isfile(os.path.join(home, "safe-mode"))


def set_safe(home, on):
    path = os.path.join(home, "safe-mode")
    if on:
        _write_private(path, "1\n")
        return
    try:
        os.remove(path)
    except OSError:
        pass


def watch_cards_enabled(home):
    """False in safe mode: the watcher keeps the transcript feed and skips card scans."""
    return not safe_on(home)


def effective_risk(risk, safe, mode):
    """Away + safe mode: everything that is not low becomes high. Other modes stay put."""
    if safe and mode == "away" and risk != "low":
        return "high"
    return risk


def note_card_result(state, ok, selectors=None):
    """(new_state, payload|None). Three failures emit degraded once. A success resets the streak."""
    state = {"streak": int((state or {}).get("streak") or 0),
             "emitted": bool((state or {}).get("emitted"))}
    if ok:
        state["streak"] = 0
        state["emitted"] = False
        return state, None
    state["streak"] += 1
    if state["streak"] >= CARD_FAILS and not state["emitted"]:
        state["emitted"] = True
        return state, degraded_payload(selectors or SELECTORS)
    return state, None


def version_payload(old, new):
    return {"event": "cursor_updated", "minor": False, "chat_id": "", "chat_name": "",
            "workspace": "", "old": old, "new": new, "snippet": "%s -> %s" % (old, new),
            "timestamp": "", "cdp_down": False}


def degraded_payload(selectors):
    names = [str(s) for s in (selectors or [])]
    return {"event": "degraded", "minor": False, "chat_id": "", "chat_name": "",
            "workspace": "", "selectors": names, "snippet": ", ".join(names)[:500],
            "note": "card scan stopped; safe mode on", "timestamp": "", "cdp_down": False}


def event_posts(events, posted):
    """cursor_updated lines the watcher has not posted yet. One post per old->new pair."""
    out, ids = [], []
    seen = set(posted or [])
    for ev in events or []:
        if not isinstance(ev, dict) or ev.get("kind") != "cursor_updated":
            continue
        old, new = str(ev.get("old") or ""), str(ev.get("new") or "")
        if not old or not new:
            continue
        key = "updated:%s:%s" % (old, new)
        if key in seen:
            continue
        seen.add(key)
        out.append(version_payload(old, new))
        ids.append(key)
    return out, ids


def registry_problems(cfg):
    """(ok, detail). Entries present, names accepted, gate entries fail closed."""
    import merge_hooks
    if not isinstance(cfg, dict):
        return False, "missing registry"
    hooks = cfg.get("hooks")
    if not isinstance(hooks, dict):
        return False, "missing registry"
    for name in hooks:
        if name not in merge_hooks.ACCEPTED_HOOK_TYPES:
            return False, "unknown name %s" % name
    for event, _matcher in merge_hooks.GATE:
        items = hooks.get(event) or []
        ours = [i for i in items if isinstance(i, dict) and merge_hooks.MARK in str(i.get("command", ""))]
        if not ours:
            return False, "missing %s" % event
        for item in ours:
            if item.get("failClosed") is not True:
                return False, "%s missing failClosed" % event
    for event in merge_hooks.EVENTS:
        items = hooks.get(event) or []
        if not any(isinstance(i, dict) and merge_hooks.MARK in str(i.get("command", "")) for i in items):
            return False, "missing %s" % event
    return True, ""


def synthetic_payload():
    return {"hook_event_name": "beforeShellExecution", "command": "git push",
            "cwd": "/tmp", "conversation_id": "c1111111-0000-4000-8000-000000000001"}


def synthetic_gate(hook_script):
    """(ok, reply, elapsed). High-risk command, Away, in a throwaway home. Must deny within 1s.

    Does not read or write the user's mode file.
    """
    home = tempfile.mkdtemp(prefix="gc-selftest-")
    try:
        os.chmod(home, 0o700)
        with open(os.path.join(home, "mode"), "w", encoding="utf-8") as fh:
            fh.write("away\n")
        env = dict(os.environ)
        env["GROK_CURSOR_HOME"] = home
        env.pop("CURSOR_VERSION", None)
        started = time.time()
        try:
            proc = subprocess.run(
                [sys.executable, hook_script, "beforeShellExecution"],
                input=json.dumps(synthetic_payload()), capture_output=True, text=True,
                timeout=2, env=env)
        except (OSError, subprocess.TimeoutExpired):
            return False, None, time.time() - started
        elapsed = time.time() - started
        try:
            reply = json.loads(proc.stdout)
        except ValueError:
            reply = None
        ok = (isinstance(reply, dict) and reply.get("permission") == "deny" and elapsed < 1
              and "stop and wait for the human" in str(reply.get("agent_message") or reply.get("user_message") or ""))
        return ok, reply, elapsed
    finally:
        shutil.rmtree(home, ignore_errors=True)


def shapes_ok(sample):
    """tabs list, cards dict, input dict. input_shape uses the same keys as the input op, and does not focus."""
    if not isinstance(sample, dict):
        return False
    tabs = sample.get("tabs")
    if not isinstance(tabs, list) or any(not isinstance(t, dict) or "text" not in t for t in tabs):
        return False
    cards = sample.get("cards")
    if not isinstance(cards, dict) or "found" not in cards:
        return False
    if cards.get("found") and not isinstance(cards.get("cards"), list):
        return False
    inp = sample.get("input")
    return isinstance(inp, dict) and "n" in inp


def _has(found, css):
    if isinstance(found, dict):
        return bool(found.get(css))
    return css in found


def selector_part(found):
    """Required: composer bar, its status attribute, and a chat input. Cards are seen or not on screen.

    A quiet window still passes. .composer-tab is reported and does not fail the check:
    3.24.12 often has no element with that class.
    """
    missing = []
    for css, group in probe_queries():
        if group in ("composer", "status") and not _has(found, css):
            missing.append(css)
    inputs = [css for css, group in probe_queries() if group == "input"]
    if not any(_has(found, css) for css in inputs):
        missing.append("chat input")

    def group_line(group, label):
        hits = [css for css, kind in probe_queries() if kind == group and _has(found, css)]
        if not hits:
            return "%s not on screen" % label
        if group != "question":
            return "%s seen" % label
        option = css_selector("composer-questionnaire-toolbar-option-label")
        if not _has(found, option):
            return "question seen (option label not on screen)"
        return "question seen"

    cards = [group_line("question", "question"), group_line("plan", "plan"), group_line("run", "run")]
    tool = [css for css, kind in probe_queries() if kind == "card" and _has(found, css)]
    cards.append("tool row seen" if tool else "tool row not on screen")
    tab = css_selector(".composer-tab")
    cards.append(".composer-tab seen" if _has(found, tab) else ".composer-tab not on screen")
    detail = "; ".join(cards)
    if missing:
        detail = "missing " + ", ".join(missing) + "; " + detail
    return {"name": "selectors", "ok": not missing, "fatal": False, "detail": detail}


def missing_selectors(found):
    """Required selectors that did not match. Card selectors are not in this list."""
    part = selector_part(found)
    if part["ok"]:
        return []
    text = part["detail"].split(";", 1)[0]
    text = text[len("missing "):] if text.startswith("missing ") else text
    return [item.strip() for item in text.split(",") if item.strip()]


def dom_matches(elements, selector):
    """querySelector semantics for a fixture. A bare word matches a tag name, not a class."""
    selector = (selector or "").strip()
    if not selector:
        return False
    if selector[0] not in ".#[":
        return any((el.get("tag") or "") == selector for el in elements or [])
    target = selector.split()[-1]
    return any(_match_simple(el, target) for el in elements or [])


def _match_simple(el, selector):
    classes, attrs, body = [], [], selector
    while body:
        if body[0] == ".":
            match = re.match(r"\.([A-Za-z0-9_-]+)", body)
        elif body[0] == "[":
            match = re.match(r'\[([A-Za-z0-9_-]+)(?:="([^"]*)")?\]', body)
        else:
            return False
        if not match:
            return False
        if body[0] == ".":
            classes.append(match.group(1))
        else:
            attrs.append((match.group(1), match.group(2)))
        body = body[match.end():]
    have_classes = el.get("classes") or []
    have_attrs = el.get("attrs") or {}
    if any(name not in have_classes for name in classes):
        return False
    for name, value in attrs:
        if name not in have_attrs:
            return False
        if value is not None and str(have_attrs[name]) != value:
            return False
    return bool(classes or attrs)


def found_in_dom(elements):
    return {css for css, _group in probe_queries() if dom_matches(elements, css)}


def judge(parts):
    """(code, names). A hooks failure is 41. Any other failure is 40. Else 0."""
    fatal = [p["name"] for p in parts if p.get("fatal") and not p.get("ok")]
    if fatal:
        return EXIT_HOOKS, fatal
    bad = [p["name"] for p in parts if not p.get("ok")]
    if bad:
        return EXIT_DEGRADED, bad
    return EXIT_OK, []


def build_selftest(home, registry_ok, registry_detail, gate_ok, probe_parts, events_ok):
    parts = [
        {"name": "registry", "ok": bool(registry_ok), "fatal": True, "detail": registry_detail or ""},
        {"name": "gate", "ok": bool(gate_ok), "fatal": True, "detail": ""},
    ]
    parts.extend(probe_parts or [])
    parts.append({"name": "events", "ok": bool(events_ok), "fatal": False, "detail": ""})
    code, bad = judge(parts)
    apply_selftest(home, code)
    return code, bad, parts


def apply_selftest(home, code):
    if code == EXIT_OK:
        set_safe(home, False)
    elif code in (EXIT_DEGRADED, EXIT_HOOKS):
        set_safe(home, True)


def events_writable(home):
    import gc_events
    try:
        path = gc_events.path(home)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.close(fd)
        return True
    except OSError:
        return False


def print_selftest(code, bad, parts, as_json=False):
    if as_json:
        json.dump({"exit": code, "bad": bad, "checks": parts}, sys.stdout)
        sys.stdout.write("\n")
        return
    for part in parts:
        detail = part.get("detail") or ""
        state = "ok" if part.get("ok") else "FAIL"
        if detail:
            sys.stdout.write("- %s: %s (%s)\n" % (part["name"], state, detail))
        else:
            sys.stdout.write("- %s: %s\n" % (part["name"], state))
    if code == EXIT_OK:
        sys.stdout.write("selftest: ok\n")
    elif code == EXIT_HOOKS:
        sys.stdout.write("selftest: hooks broken (%s)\n" % ", ".join(bad))
    else:
        sys.stdout.write("selftest: degraded (%s)\n" % ", ".join(bad))


def status_bits(home):
    return read_known(home), safe_on(home)


def clip_text(text, limit=80):
    import gc_send
    text = gc_send.norm(text or "")
    if gc_send.find_secrets(text):
        return "[secret]"
    text = _PATH.sub("[path]", text)
    return text[:limit]


def sanitize_dump(doc):
    def clean(value):
        if isinstance(value, str):
            return clip_text(value)
        if isinstance(value, list):
            return [clean(v) for v in value]
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items()}
        return value
    return clean(doc or {})


def card_from_dump(doc):
    """The card dict JS cards() would return for a sanitized fixture."""
    import gc_send
    doc = doc or {}
    kind = doc.get("kind") or ""
    comps = doc.get("components") or []
    buttons = list(doc.get("buttons") or [])
    if not buttons and doc.get("actions"):
        buttons = [a.get("label") for a in doc["actions"] if isinstance(a, dict) and a.get("label")]
    if not kind:
        if doc.get("toolbar_class") or doc.get("options"):
            kind = "question"
        elif "split-button" in comps:
            kind = "plan"
        elif doc.get("footer") or ({"Run", "Allow"} & set(buttons)):
            kind = "run"
        elif "transcript-card-root" in comps or "keep_all" in doc:
            kind = "edit"
    chat_id = doc.get("composer_id") or doc.get("chat_id") or ""
    command = doc.get("command") or ""
    if kind == "question":
        card_id = ""
    elif doc.get("card_id"):
        card_id = doc["card_id"]
    elif kind == "run":
        card_id = gc_send.run_card_id(chat_id, command)
    else:
        card_id = doc.get("data-tool-call-id") or ""
    return {"kind": kind, "card_id": card_id, "buttons": buttons,
            "options": list(doc.get("options") or []),
            "status": doc.get("data-tool-status") or doc.get("composer_status") or doc.get("status") or ""}


def fixture_dir(start=None):
    cur = os.path.abspath(start or os.getcwd())
    for _i in range(6):
        cand = os.path.join(cur, "tests", "fixtures", "dom")
        if os.path.isdir(cand):
            return cand
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def dump_name(version, kind):
    safe_ver = re.sub(r"[^A-Za-z0-9._-]", "-", version or "unknown") or "unknown"
    safe_kind = re.sub(r"[^a-z]", "", (kind or "").lower()) or "card"
    return "%s-%s.json" % (safe_ver, safe_kind)


def cmd_probe(args, ctx):
    """Write one sanitized fixture per card kind. Refuses to overwrite. Clicks nothing."""
    import gc_send
    if not getattr(args, "to", None):
        sys.stderr.write("grok-cursor probe: --to is required\n")
        return 2
    if not getattr(args, "dump", False):
        sys.stderr.write("grok-cursor probe: --dump is required\n")
        return 2
    try:
        chat, _page, ws, _tab = gc_send._open_chat(args)
    except gc_send.SendAbort as exc:
        sys.stderr.write("grok-cursor probe: %s\n" % exc.msg)
        return exc.code
    try:
        snap = gc_send.cdp_eval(ws, "cards", id=chat["id"]) or {}
    finally:
        ws.close()
    cards = [c for c in (snap.get("cards") or []) if isinstance(c, dict) and c.get("kind")]
    if not cards:
        sys.stderr.write("grok-cursor probe: no card\n")
        return 32
    root = fixture_dir()
    if not root:
        sys.stderr.write("grok-cursor probe: run this from the repo\n")
        return 2
    version = read_known(ctx["data_home"]()) or "unknown"
    wrote = 0
    seen = set()
    for card in cards:
        kind = card.get("kind")
        if kind in seen:
            continue
        seen.add(kind)
        name = dump_name(version, kind)
        path = os.path.join(root, name)
        if os.path.exists(path):
            sys.stdout.write("kept %s\n" % name)
            continue
        doc = sanitize_dump({
            "version": version, "kind": kind, "card_id": card.get("card_id") or "",
            "buttons": card.get("buttons") or [], "options": card.get("options") or [],
            "status": card.get("status") or "", "command": clip_text(card.get("command") or ""),
        })
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
        sys.stdout.write("wrote %s\n" % name)
        wrote += 1
    return 0 if wrote else 2


def branch_name(version):
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", version or "unknown") or "unknown"
    return "fix/cursor-" + safe


def breakage_name(version):
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", version or "unknown") or "unknown"
    return "BREAKAGE-%s.md" % safe


def breakage_doc(version, report, failing):
    lines = ["# Breakage %s" % (version or "unknown"), "",
             "Selftest failed. Do not merge this branch. Do not update Cursor from here.", ""]
    body = clip_text(report or "", 4000)
    if body:
        lines.extend(["## Selftest", "", body, ""])
    lines.append("## Fixtures that fail")
    lines.append("")
    names = failing or []
    if not names:
        lines.append("(none listed)")
    for name in names:
        lines.append("- %s" % clip_text(name, 80))
    lines.append("")
    return "\n".join(lines)


def fix_prompt(version, filename):
    return (
        "Cursor selectors broke on %s.\n"
        "Read docs/%s. Fix the selectors. Do not merge. Do not push. Do not update Cursor.\n"
        "This branch has the notes only. Nothing was committed.\n" % (version or "unknown", filename)
    )


def cmd_fix_draft(repo, version, report, failing):
    """Branch fix/cursor-<version> off send, write the breakage note, print the prompt. Never commits."""
    branch = branch_name(version)
    name = breakage_name(version)
    proc = subprocess.run(["git", "checkout", "-b", branch, "send"], cwd=repo,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr or proc.stdout)
        return proc.returncode or 1
    path = os.path.join(repo, "docs", name)
    if os.path.exists(path):
        sys.stderr.write("grok-cursor fix-draft: %s already exists\n" % name)
        return 2
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(breakage_doc(version, report, failing))
    sys.stdout.write(fix_prompt(version, name))
    return 0


def repo_hook(start=None):
    cur = os.path.abspath(start or os.getcwd())
    for _i in range(6):
        cand = os.path.join(cur, "hooks", "grok-cursor-hook")
        if os.path.isfile(cand):
            return cand
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def collect_probe(port, targets):
    """CDP checks with no clicks. A closed port is degraded, not a hooks failure."""
    import gc_send
    import gc_watch
    if not gc_send.cdp_listening(port):
        return [
            {"name": "cdp", "ok": False, "fatal": False, "detail": "cdp down"},
            {"name": "selectors", "ok": False, "fatal": False, "detail": "cdp down"},
            {"name": "scratch", "ok": True, "fatal": False, "detail": ""},
        ]
    parts = [{"name": "cdp", "ok": True, "fatal": False, "detail": ""}]
    found = {}
    try:
        pages = gc_watch.workbench_pages(port) or []
    except Exception:
        pages = []
    for page in pages:
        ws = None
        try:
            ws = gc_send.WS(page.get("webSocketDebuggerUrl"), timeout=3)
            snap = gc_send.cdp_eval(ws, "selectors", selectors=list(SELECTORS)) or {}
            missing = set(snap.get("missing") or [])
            for sel in SELECTORS:
                found[sel] = found.get(sel) or sel not in missing
        except gc_send.SendAbort:
            pass
        finally:
            if ws:
                ws.close()
    parts.append(selector_part(found))
    scratches = [t for t in (targets or []) if t.get("open") and gc_watch.is_scratch(
        t.get("title") or "", t.get("workspace_name") or "")]
    bad = []
    for chat in scratches:
        ws = None
        try:
            page = gc_send.cdp_page(port, None, chat.get("workspace_name"))
            ws = gc_send.WS(page.get("webSocketDebuggerUrl"), timeout=3)
            sample = {
                "tabs": gc_send.cdp_eval(ws, "tabs") or [],
                "cards": gc_send.cdp_eval(ws, "cards", id=chat.get("id") or "") or {},
                "input": gc_send.cdp_eval(ws, "input_shape", id=chat.get("id") or "") or {},
            }
            if not shapes_ok(sample):
                bad.append((chat.get("id") or "")[:8])
        except gc_send.SendAbort:
            bad.append((chat.get("id") or "")[:8])
        finally:
            if ws:
                ws.close()
    parts.append({"name": "scratch", "ok": not bad, "fatal": False, "detail": ", ".join(bad)})
    return parts


def cmd_selftest(args, ctx, probe_parts=None):
    """Score the install. probe_parts injects CDP results for tests. Clicks nothing."""
    home = ctx["data_home"]()
    cfg = ctx.get("registry")
    if cfg is None:
        reg_ok, reg_detail = False, "missing registry"
    else:
        reg_ok, reg_detail = registry_problems(cfg)
    hook = ctx.get("hook_script") or repo_hook()
    gate_ok = False
    if hook:
        gate_ok, _reply, _elapsed = synthetic_gate(hook)
    if probe_parts is None:
        probe_parts = ctx.get("probe_parts")
    if probe_parts is None:
        port = getattr(args, "cdp", None) or 9222
        try:
            import gc_send
            targets = gc_send.list_targets()
        except Exception:
            targets = []
        probe_parts = collect_probe(port, targets)
    code, bad, parts = build_selftest(
        home, reg_ok, reg_detail, gate_ok, probe_parts, events_writable(home))
    print_selftest(code, bad, parts, as_json=bool(getattr(args, "json", False)))
    return code
