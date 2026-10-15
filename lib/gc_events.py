"""gc_events: append-only ~/.grok-cursor/events.jsonl (0600, one JSON object per line).

  append(kind, **fields)        adds "t" (ISO time) and "kind"; string fields are redacted
  read(since_s=None)            events newer than since_s seconds (rotated file included)
  parse_since("30m")            -> seconds (s, m, h, d; bare number = minutes)

Written by the hook, the watcher and the CLI. Rotates to events.jsonl.1 at 5 MB.
Never holds a nonce. Python 3 stdlib.
"""
import datetime
import json
import os
import re

MAX_BYTES = 5 * 1024 * 1024

def data_home():
    return os.environ.get("GROK_CURSOR_HOME") or os.path.join(os.path.expanduser("~"), ".grok-cursor")

# --- BEGIN REDACT (keep identical in bin/grok-cursor; tests enforce it) ---
_SECRET_NAME = r"[A-Za-z0-9_.-]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Za-z0-9_.-]*"
_REDACT_RULES = [
    # PEM private keys (multi-line)
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
                re.S), "[REDACTED:private-key]"),
    # .env / shell style:  FOO_API_KEY=value   export GITHUB_TOKEN="..."
    (re.compile(r"(?im)^(\s*(?:export\s+)?" + _SECRET_NAME + r"\s*[=:]\s*)(?!\[REDACTED)(\S.*)$"),
     r"\1[REDACTED]"),
    # quoted config style:  "api_key": "value"   password = 'value'
    (re.compile(r"(?i)([\"']?" + _SECRET_NAME + r"[\"']?\s*[:=]\s*)([\"'])(?!\[REDACTED)[^\"'\n]{4,}\2"),
     r"\1\2[REDACTED]\2"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "[REDACTED:sk]"),
    (re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"), "[REDACTED:stripe]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"), "[REDACTED:github]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}"), "[REDACTED:github]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED:aws]"),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), "[REDACTED:slack]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}"), "[REDACTED:google]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[REDACTED:jwt]"),
    (re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{16,}"), r"\1 [REDACTED]"),
    # credentials inside URLs: scheme://user:password@host
    (re.compile(r"([a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s@/]+@"), r"\1[REDACTED]@"),
]


def redact(text):
    if not text or os.environ.get("GROK_CURSOR_REDACT", "1") == "0":
        return text or ""
    for pattern, repl in _REDACT_RULES:
        text = pattern.sub(repl, text)
    return text
# --- END REDACT ---

def path(home=None):
    return os.path.join(home or data_home(), "events.jsonl")

def _clean(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value

def append(kind, home=None, **fields):
    """One line, one write(), O_APPEND: concurrent writers never interleave a line."""
    p = path(home)
    os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
    try:
        if os.path.getsize(p) > MAX_BYTES:
            os.replace(p, p + ".1")
    except OSError:
        pass
    obj = {"t": datetime.datetime.now().astimezone().isoformat(timespec="seconds"), "kind": kind}
    obj.update(_clean(fields))
    data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    return obj

def read(since_s=None, home=None):
    cutoff = None
    if since_s is not None:
        cutoff = datetime.datetime.now().astimezone() - datetime.timedelta(seconds=since_s)
    out = []
    for p in (path(home) + ".1", path(home)):
        try:
            fh = open(p, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                    t = datetime.datetime.fromisoformat(obj["t"])
                except (ValueError, KeyError, TypeError):
                    continue
                if cutoff is None or t >= cutoff:
                    out.append(obj)
    return out

def parse_since(text):
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", str(text or ""))
    if not m:
        raise ValueError("bad --since %r (use e.g. 90s, 30m, 2h, 1d)" % text)
    return float(m.group(1)) * {"": 60, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
