"""gc_outbox: durable webhook delivery for `grok-cursor watch`. Python 3 stdlib.

Every ping is written to <home>/queue/<id>.json before any network call and deleted only
after a 2xx. Retries use exponential backoff with jitter; 400/429 and 5xx and network errors
are all retryable (the webhook has returned 400 for its own rate limit). Nothing is ever dropped.
<home>/delivery.json keeps last success, last error and counts for `watch status`.
If nothing has been delivered for NOTIFY_AFTER seconds, hold/finish events also raise a local
macOS notification so the human still sees them.
"""
import datetime
import glob
import json
import os
import random
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

BACKOFF_BASE = 5.0
BACKOFF_MAX = 300.0
NOTIFY_AFTER = 120.0
NOTIFY_EVENTS = ("decision_requested", "approval_needed", "finished", "chat_stalled")
QUEUE_DIR = "queue"
STATE_FILE = "delivery.json"
RETRYABLE_4XX = (400, 408, 425, 429)

def _now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")

def _atomic(path, obj):
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

def event_id(payload):
    """Stable id: the webhook can dedupe on it, and a re-queued event never doubles."""
    p = payload.get
    base = p("event_id") or "%s:%s:%s:%s" % (p("event"), p("chat_id") or "", p("req_id") or p("timestamp") or "",
                                             p("repeat") or 0)
    return "".join(c if c.isalnum() or c in "._:-" else "_" for c in str(base))[:200]

def post_once(url, key, payload, timeout=5):
    """(status or None, short reason). Never raises; never logs the key or the payload."""
    data = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data, {"Content-Type": "application/json",
                                             "Authorization": "Bearer " + key,
                                             "User-Agent": "grok-cursor-watch"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, ""
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(300).decode("utf-8", "replace")
        except Exception:
            body = ""
        exc.close()
        return exc.code, " ".join(body.split())[:200]
    except (urllib.error.URLError, OSError) as exc:
        return None, type(exc).__name__

def retryable(status):
    return status is None or status >= 500 or status in RETRYABLE_4XX

def backoff(attempt, rnd=random.random):
    """5, 10, 20 ... capped at 300 s, each with up to 25 % jitter."""
    base = min(BACKOFF_MAX, BACKOFF_BASE * (2 ** max(0, attempt - 1)))
    return base * (1 + 0.25 * rnd())

def notify(title, text):
    """Local macOS banner. Best effort; off outside macOS or when GROK_CURSOR_NOTIFY=0."""
    if os.environ.get("GROK_CURSOR_NOTIFY", "1") == "0" or not os.path.exists("/usr/bin/osascript"):
        return False
    safe = lambda s: str(s or "").replace("\\", "\\\\").replace('"', '\\"')[:200]
    script = 'display notification "%s" with title "%s"' % (safe(text), safe(title))
    try:
        subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, timeout=5)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False

class Outbox(object):
    def __init__(self, home, poster=post_once, clock=time.time, notifier=notify, log=None):
        self.home = home
        self.dir = os.path.join(home, QUEUE_DIR)
        self.state_path = os.path.join(home, STATE_FILE)
        self.poster, self.clock, self.notifier = poster, clock, notifier
        self.log = log or (lambda m: None)
        self.state = self._load_state()

    # -- state
    def _load_state(self):
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                st = json.load(fh)
            if isinstance(st, dict):
                return st
        except (OSError, ValueError):
            pass
        return {"last_success": None, "last_error": None, "last_error_at": None, "delivered": 0, "failed_attempts": 0}

    def _save_state(self):
        _atomic(self.state_path, self.state)

    # -- queue
    def enqueue(self, payload):
        """Write to disk first. Same event id = same file, so a re-emit is not a duplicate."""
        eid = event_id(payload)
        path = os.path.join(self.dir, eid + ".json")
        if os.path.exists(path):
            return eid
        _atomic(path, {"id": eid, "payload": payload, "queued": self.clock(), "attempts": 0, "next": 0,
                       "notified": False})
        return eid

    def _items(self):
        out = []
        for path in glob.glob(os.path.join(self.dir, "*.json")):
            try:
                with open(path, encoding="utf-8") as fh:
                    item = json.load(fh)
                if isinstance(item, dict) and isinstance(item.get("payload"), dict):
                    item["_path"] = path
                    out.append(item)
            except (OSError, ValueError):
                continue
        out.sort(key=lambda i: float(i.get("queued") or 0))
        return out

    def depth(self):
        return len(glob.glob(os.path.join(self.dir, "*.json")))

    def oldest(self):
        items = self._items()
        return items[0] if items else None

    # -- delivery
    def flush(self, url, key, budget=None):
        """One pass: send every due item once. Returns (delivered, still_queued)."""
        now = self.clock()
        delivered, left = 0, 0
        for item in self._items():
            if budget is not None and self.clock() - now > budget:
                left += 1
                continue
            if now < float(item.get("next") or 0):
                left += 1
                self._maybe_notify(item)
                continue
            status, reason = self.poster(url, key, item["payload"])
            if status is not None and 200 <= status < 300:
                try:
                    os.remove(item["_path"])
                except OSError:
                    pass
                self.state["last_success"] = _now_iso()
                self.state["delivered"] = int(self.state.get("delivered") or 0) + 1
                delivered += 1
                continue
            if not retryable(status):           # 401/403/404...: config problem; keep it, but back off hard
                item["next"] = now + BACKOFF_MAX
            else:
                item["next"] = now + backoff(int(item.get("attempts") or 0) + 1)
            item["attempts"] = int(item.get("attempts") or 0) + 1
            item["last_status"] = status
            self.state["last_error"] = "%s %s" % (status if status is not None else "network", reason)
            self.state["last_error_at"] = _now_iso()
            self.state["failed_attempts"] = int(self.state.get("failed_attempts") or 0) + 1
            self.log("webhook %s (%s): %s queued, retry in %ds" % (
                status if status is not None else "unreachable", reason[:120], item["id"][:40], item["next"] - now))
            path = item.pop("_path")
            _atomic(path, item)
            left += 1
            self._maybe_notify(item)
        if delivered or left:
            self._save_state()
        return delivered, left

    def _down_for(self):
        """Seconds since the last 2xx, or since the oldest queued item if nothing ever succeeded."""
        ls = self.state.get("last_success")
        if ls:
            try:
                return self.clock() - datetime.datetime.fromisoformat(ls).timestamp()
            except ValueError:
                pass
        o = self.oldest()
        return self.clock() - float(o.get("queued") or self.clock()) if o else 0.0

    def _maybe_notify(self, item):
        if item.get("notified"):
            return
        p = item.get("payload") or {}
        if p.get("event") not in NOTIFY_EVENTS or p.get("minor"):
            return
        if self.clock() - float(item.get("queued") or 0) < NOTIFY_AFTER and self._down_for() < NOTIFY_AFTER:
            return
        title = "grok-cursor: webhook down, %s" % p.get("event")
        text = (p.get("chat_name") or p.get("chat_id", "")[:8]) + " " + (p.get("command") or p.get("snippet") or "")[:120]
        if self.notifier(title, text.strip()):
            item["notified"] = True
            path = item.get("_path") or os.path.join(self.dir, item["id"] + ".json")
            item = {k: v for k, v in item.items() if k != "_path"}
            _atomic(path, item)
            self.log("notified locally: %s" % p.get("event"))

    def summary(self):
        o = self.oldest()
        age = (self.clock() - float(o.get("queued") or self.clock())) if o else 0
        return {"depth": self.depth(), "oldest_event": (o or {}).get("payload", {}).get("event") if o else None,
                "oldest_age_s": int(age), "oldest_attempts": int((o or {}).get("attempts") or 0),
                "last_success": self.state.get("last_success"), "last_error": self.state.get("last_error"),
                "last_error_at": self.state.get("last_error_at"), "delivered": self.state.get("delivered"),
                "failed_attempts": self.state.get("failed_attempts")}
