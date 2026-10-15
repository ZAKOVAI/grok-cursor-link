"""gc_gate: the hook's permission decisions. Imported by hooks/grok-cursor-hook
and by `grok-cursor decide`. Python 3 stdlib.

Shell (beforeShellExecution), both modes:
  self-protection deny, then low / exact always.json match allow,
  away+high immediate deny (still resumable), otherwise hold for `decide`.
Present medium/high and away high ask the human (high allow needs --human).
Away medium asks Grok Bot. Reads and MCP calls are self-protection only.
Never returns {}. Never allows high without a human decision.
"""
import datetime
import hashlib
import json
import math
import os
import re
import secrets
import tempfile
import time

import gc_events
import gc_risk

GATE_EVENTS = ("beforeShellExecution", "beforeMCPExecution", "beforeReadFile", "preToolUse")
FILE_TOOLS = ("Read", "Write", "Delete", "Grep", "Glob", "ListDir")
POLL_SECONDS = 0.05
DEFAULT_TTL = 20 * 60
PROVED_CEILING = 90.0          # PROBES fact 2: a 90s hook was not killed

_GC_SUB = re.compile(
    r"(?:^|[\s;/|&(])grok-cursor\s+(decide|approve|answer|mode|hooks|always|resume)\b")
_GC_SEND = re.compile(r"(?:^|[\s;/|&(])grok-cursor\s+send\b")
_P95 = re.compile(r"p95\s*=\s*(\d+(?:\.\d+)?)|p95\s+(\d+(?:\.\d+)?)\s*s")
_CEILING = re.compile(r"[Cc]eiling[^0-9]{0,50}(\d+)\s*s\b")
_REQ_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

class DecisionError(Exception):
    """decide must exit 2. Raised before the decision file is modified."""

def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")

def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""

def probes_text():
    env = os.environ.get("GROK_CURSOR_PROBES")
    if env:
        return _read(env)
    here = os.path.dirname(os.path.realpath(__file__))
    for rel in ("../docs/PROBES.md", "../../docs/PROBES.md"):
        path = os.path.normpath(os.path.join(here, rel))
        text = _read(path)
        if text:
            return text
    return ""

def ceiling_from_text(text):
    nums = [float(n) for n in _CEILING.findall(text or "")]
    return max(nums) if nums else PROVED_CEILING

def hold_from_text(text):
    """Fact 6 p95 + 30s, rounded up, capped at fact 2's ceiling.
    60 when the probes text is missing or has no p95."""
    if not text:
        return 60.0
    ceiling = ceiling_from_text(text)
    nums = [float(a or b) for a, b in _P95.findall(text)]
    if not nums:
        return min(60.0, ceiling)
    hold = float(math.ceil(nums[-1] + 30))
    return ceiling if hold > ceiling else hold

def load_config(home):
    try:
        with open(os.path.join(home, "config.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}

def hold_seconds(home):
    cfg = load_config(home)
    text = probes_text()
    ceiling = ceiling_from_text(text)
    if "hold" in cfg:
        try:
            return min(float(cfg["hold"]), ceiling)
        except (TypeError, ValueError):
            pass
    return hold_from_text(text)

def ttl_seconds(home):
    cfg = load_config(home)
    try:
        return float(cfg.get("ttl", DEFAULT_TTL))
    except (TypeError, ValueError):
        return float(DEFAULT_TTL)

def followup_enabled():
    """PROBES fact 2b. Missing or a negative 2b means the stop hook stays quiet."""
    text = probes_text()
    if re.search(r"2b:\s*no\b", text, re.I):
        return False
    return bool(re.search(r"2b:\s*yes\b", text, re.I))

def read_mode(home):
    value = _read(os.path.join(home, "mode")).strip()
    return value if value in ("present", "away") else "present"

# ---------------------------------------------------------------- self-protection

def normalize(text):
    """Drop quotes, expand ~ and $HOME, collapse whitespace. For matching only."""
    s = re.sub(r"[\"'`]", "", str(text or ""))
    home = os.path.expanduser("~")
    s = s.replace("${HOME}", home).replace("$HOME", home)
    s = re.sub(r"(^|[\s=:])~", lambda m: m.group(1) + home, s)
    return re.sub(r"\s+", " ", s).strip()

def mentions_protected(text):
    n = normalize(text)
    if ".grok-cursor" in n or "hooks.json" in n:
        return True
    roots = [os.path.normpath(os.path.expanduser("~/.grok-cursor"))]
    if os.environ.get("GROK_CURSOR_HOME"):
        roots.append(os.path.normpath(os.environ["GROK_CURSOR_HOME"]))
    return any(r and r in n for r in roots)

def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)

def path_is_protected(path):
    """~/.grok-cursor/** or ~/.cursor/hooks.json, after ~ expansion."""
    raw = str(path or "")
    if not raw:
        return False
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        expanded = os.path.abspath(expanded)
    expanded = os.path.normpath(expanded)
    hooks = os.path.normpath(os.path.expanduser("~/.cursor/hooks.json"))
    if expanded == hooks:
        return True
    roots = [os.path.normpath(os.path.expanduser("~/.grok-cursor"))]
    extra = os.environ.get("GROK_CURSOR_HOME")
    if extra:
        roots.append(os.path.normpath(extra))
    return any(expanded == r or expanded.startswith(r + os.sep) for r in roots)

def _input_mentions(payload):
    raw = payload.get("tool_input")
    values = list(_strings(raw))
    if isinstance(raw, str):
        try:
            values.extend(_strings(json.loads(raw)))
        except ValueError:
            pass
    return any(mentions_protected(v) or path_is_protected(v) for v in values)

def _is_cursor_send(payload):
    if str(payload.get("tool_name") or "") != "cursor_send":
        return False
    hay = " ".join(str(payload.get(k) or "") for k in ("mcp_server_name", "command", "url"))
    return "grok-cursor" in hay or "grok_cursor" in hay

def self_protect(event, payload, mode):
    """Reason string, or '' to let the command through. No file reads of the target."""
    if event == "beforeShellExecution":
        cmd = payload.get("command") or ""
        norm = normalize(cmd)
        if _GC_SUB.search(norm):
            return "grok-cursor control command"
        if mode == "away" and _GC_SEND.search(norm):
            return "grok-cursor send"
        if mentions_protected(cmd):
            return "protected path"
        return ""
    if event == "beforeReadFile":
        paths = [payload.get("file_path")]
        for att in payload.get("attachments") or []:
            if isinstance(att, dict):
                paths.append(att.get("file_path"))
        if any(path_is_protected(p) for p in paths):
            return "protected read"
        return ""
    if event == "preToolUse":
        if str(payload.get("tool_name") or "") not in FILE_TOOLS:
            return ""
        if _input_mentions(payload):
            return "protected tool path"
        return ""
    if event == "beforeMCPExecution":
        if _input_mentions(payload):
            return "protected mcp input"
        if mode == "away" and _is_cursor_send(payload):
            return "cursor_send"
        return ""
    return ""

# ---------------------------------------------------------------- replies

def allow_reply():
    return {"permission": "allow"}

def deny_reply(event, user, agent=None):
    reply = {"permission": "deny", "user_message": user}
    if event != "beforeReadFile":
        reply["agent_message"] = agent or user
    return reply

def _blocked(event, reason):
    user = "grok-cursor: blocked by self-protection (%s)" % reason
    return deny_reply(event, user, user + "; do not retry")

def _log(home, kind, **fields):
    try:
        gc_events.append(kind, home=home, **fields)
    except Exception:
        pass

# ---------------------------------------------------------------- decisions on disk

def _atomic(path, obj):
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

def _write_outbox(home, req_id, nonce):
    path = os.path.join(home, "outbox", req_id)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(nonce + "\n")

def decision_path(home, req_id):
    return os.path.join(home, "decisions", req_id + ".json")

def load_decision(path):
    with open(path, encoding="utf-8") as fh:
        obj = json.load(fh)
    if not isinstance(obj, dict):
        raise ValueError("not an object")
    return obj

def _proof(nonce, verdict, human):
    raw = "%s\n%s\n%s" % (nonce, verdict, "1" if human else "0")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def _save(path, obj):
    clean = {k: v for k, v in obj.items() if k not in ("_path", "_nonce")}
    _atomic(path, clean)

def create_request(home, cmd, cwd, risk, mode, decider, cid, tool_use_id):
    req_id = secrets.token_hex(4)
    nonce = secrets.token_hex(16)
    obj = {
        "req_id": req_id, "cmd": cmd, "cwd": cwd or "", "risk": risk,
        "conversation_id": cid, "nonce_sha256": hashlib.sha256(nonce.encode("utf-8")).hexdigest(),
        "created": now_iso(), "decider": decider, "mode": mode,
        "tool_use_id": tool_use_id or "", "delivered": False, "consumed": False,
    }
    _atomic(decision_path(home, req_id), obj)
    _write_outbox(home, req_id, nonce)
    obj["_nonce"] = nonce          # memory only; _save never writes it
    _log(home, "decision_requested", req_id=req_id, risk=risk, command=cmd, cwd=cwd or "",
         mode=mode, decider=decider, conversation_id=cid)
    return obj

def _fresh(obj, ttl):
    try:
        created = datetime.datetime.fromisoformat(str(obj.get("created")))
    except ValueError:
        return False
    if created.tzinfo is None:
        created = created.astimezone()
    return (datetime.datetime.now().astimezone() - created).total_seconds() <= ttl

def _each(home):
    directory = os.path.join(home, "decisions")
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        try:
            obj = load_decision(path)
        except (OSError, ValueError):
            continue
        obj["_path"] = path
        yield obj

def _usable_allow(obj, ttl):
    if obj.get("decided") != "allow" or obj.get("consumed"):
        return False
    if obj.get("risk") == "high" and not obj.get("human"):
        return False
    return _fresh(obj, ttl)

def find_reissue(home, cid, command):
    want = (command or "").strip()
    ttl = ttl_seconds(home)
    for obj in _each(home):
        if obj.get("conversation_id") == cid and (obj.get("cmd") or "").strip() == want \
                and _usable_allow(obj, ttl):
            return obj
    return None

def always_match(home, command):
    try:
        with open(os.path.join(home, "always.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return False
    cmds = data.get("commands") if isinstance(data, dict) else data
    if not isinstance(cmds, list):
        return False
    want = (command or "").strip()
    return any(isinstance(c, str) and c.strip() == want for c in cmds)

def _mark(obj, **fields):
    obj.update(fields)
    _save(obj["_path"], obj)

def _expiry_reply(event, obj):
    if obj.get("decider") == "human":
        user = "grok-cursor: waiting for the human on %s (%s)" % (obj["req_id"], obj.get("risk"))
        agent = "grok-cursor: held %s for the human; stop and wait, it will be resumed if approved" % obj["req_id"]
        return deny_reply(event, user, agent)
    msg = "grok-cursor: no decision in time; stop and wait for the human"
    return deny_reply(event, msg, msg)

def _poll(home, event, obj):
    path = decision_path(home, obj["req_id"])
    deadline = time.monotonic() + hold_seconds(home)
    while True:
        try:
            cur = load_decision(path)
        except (OSError, ValueError):
            cur = obj
        cur["_path"] = path
        proved = cur.get("proof") == _proof(obj.get("_nonce"), cur.get("decided"), bool(cur.get("human")))
        if cur.get("decided") == "deny" and proved:
            msg = "grok-cursor: decision %s denied" % cur["req_id"]
            _log(home, "gate", req_id=cur["req_id"], permission="deny", risk=cur.get("risk"),
                 command=cur.get("cmd") or "")
            return deny_reply(event, msg, msg)
        if cur.get("decided") == "allow" and proved and _usable_allow(
                dict(cur, consumed=False), ttl_seconds(home)):
            _mark(cur, consumed=True)
            _log(home, "gate", req_id=cur["req_id"], permission="allow", risk=cur.get("risk"),
                 command=cur.get("cmd") or "")
            return allow_reply()
        if time.monotonic() >= deadline:
            reply = _expiry_reply(event, obj)
            _log(home, "gate", req_id=obj["req_id"], permission="deny", risk=obj.get("risk"),
                 command=obj.get("cmd") or "", expired=True)
            return reply
        time.sleep(min(POLL_SECONDS, max(0.0, deadline - time.monotonic())))

def note_version(home, payload):
    try:
        import gc_events
        import gc_resilience
        gc_resilience.note_cursor_version(home, payload, gc_events.append)
    except Exception:
        pass

def handle(event, payload, home):
    """Full-schema permission reply for one gate event. `home` is ~/.grok-cursor."""
    payload = payload or {}
    note_version(home, payload)
    mode = read_mode(home)
    reason = self_protect(event, payload, mode)
    if reason:
        _log(home, "blocked", event=event, reason=reason,
             command=payload.get("command") or "", tool=payload.get("tool_name") or "")
        return _blocked(event, reason)
    if event != "beforeShellExecution":
        return allow_reply()

    cmd = str(payload.get("command") or "")
    cwd = str(payload.get("cwd") or "")
    cid = str(payload.get("conversation_id") or payload.get("session_id") or "")
    risk, _why = gc_risk.tag(cmd, cwd)
    try:
        import gc_resilience
        risk = gc_resilience.effective_risk(risk, gc_resilience.safe_on(home), mode)
    except Exception:
        pass
    prior = find_reissue(home, cid, cmd)
    if prior:
        _mark(prior, consumed=True)
        _log(home, "resumed", req_id=prior["req_id"], command=cmd, via="reissue")
        return allow_reply()
    if risk != "high" and (risk == "low" or always_match(home, cmd)):
        _log(home, "gate", permission="allow", risk=risk, command=cmd, mode=mode)
        return allow_reply()

    decider = "grok" if mode == "away" and risk == "medium" else "human"
    obj = create_request(home, cmd, cwd, risk, mode, decider, cid, payload.get("tool_use_id"))
    if mode == "away" and risk == "high":
        msg = "grok-cursor: stop and wait for the human"
        _log(home, "gate", req_id=obj["req_id"], permission="deny", risk="high", command=cmd, mode=mode)
        return deny_reply(event, msg, msg)
    return _poll(home, event, obj)

def stop_reply(payload, home):
    """followup_message dict, or None to keep the recorder's {} reply."""
    if not followup_enabled():
        return None
    cid = str((payload or {}).get("conversation_id") or "")
    if not cid:
        return None
    ttl = ttl_seconds(home)
    pending = [o for o in _each(home)
               if o.get("conversation_id") == cid and not o.get("delivered") and _usable_allow(o, ttl)]
    if not pending:
        return None
    pending.sort(key=lambda o: o.get("created") or "")
    obj = pending[0]
    _mark(obj, delivered=True)
    text = resume_message(obj)
    _log(home, "resumed", req_id=obj["req_id"], command=obj.get("cmd") or "", via="stop")
    return {"followup_message": text}

def resume_message(obj):
    return "grok-cursor: decision %s allowed; re-run: %s" % (obj.get("req_id"), obj.get("cmd") or "")

def deliver(home, req_id):
    """Mark an allowed decision delivered by the CDP fallback. The stop hook then stays quiet."""
    path = decision_path(home, req_id)
    obj = load_decision(path)
    obj["_path"] = path
    _mark(obj, delivered=True)
    _log(home, "resumed", req_id=req_id, command=obj.get("cmd") or "", via="cli")

def apply_decision(home, req_id, nonce, verdict, reason="", human=False):
    """Record one decision. DecisionError before any write on a bad request."""
    if verdict not in ("allow", "deny"):
        raise DecisionError("verdict must be allow or deny")
    if not _REQ_ID.match(req_id or ""):
        raise DecisionError("unknown id")
    path = decision_path(home, req_id)
    try:
        obj = load_decision(path)
    except (OSError, ValueError):
        raise DecisionError("unknown id")
    if obj.get("decided"):
        raise DecisionError("reused nonce")
    digest = hashlib.sha256(str(nonce or "").encode("utf-8")).hexdigest()
    if not nonce or digest != obj.get("nonce_sha256"):
        raise DecisionError("wrong nonce")
    if obj.get("risk") == "high" and verdict == "allow" and not human:
        raise DecisionError("high risk allow needs --human")
    obj["decided"] = verdict
    obj["reason"] = reason or ""
    obj["human"] = bool(human)
    obj["decided_at"] = now_iso()
    # Proves this write knew the nonce. The in-flight hook checks it against the
    # nonce it still holds in memory, so a forged "decided" during the hold is ignored.
    obj["proof"] = _proof(nonce, verdict, bool(human))
    _save(path, obj)
    _log(home, "decided", req_id=req_id, verdict=verdict, reason=reason or "",
         risk=obj.get("risk") or "", human=bool(human))
    return obj
