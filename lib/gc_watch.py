"""gc_watch: `grok-cursor watch`. Pings a webhook when any Cursor agent chat finishes a
response or shows an approval card. Read-only: it never types or clicks.

Sources:
  transcripts  ~/.cursor/projects/*/agent-transcripts/<id>/<id>.jsonl. A turn is done when the
               file ends with {"type":"turn_ended",...} and has been quiet for DEBOUNCE seconds.
  CDP          every open Cursor window on 127.0.0.1:9222. A chat tab with the question-mark
               badge, or an in-view approval card, is "approval_needed". CDP down = transcripts only.

State (offsets, pinged cards) lives in ~/.grok-cursor/watch-state.json, so a restart doesn't re-ping.
Webhook config: ~/.grok-cursor/watch.env with GROK_CURSOR_WATCH_URL and GROK_CURSOR_WATCH_KEY.
The key is only ever put in the Authorization header; it is never printed or logged.
"""
import datetime
import glob
import json
import os
import plistlib
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import gc_card_events
import gc_events
import gc_outbox
import gc_send
from gc_send import SendAbort

DEFAULT_LABEL = "com.grok-cursor.watch"
_LABEL_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

def resolve_label(home=None):
    """launchd label: $GROK_CURSOR_WATCH_LABEL, else <GROK_CURSOR_HOME>/watch-label, else the default.
    Lets an existing install keep its own label so a reinstall never starts a second watcher."""
    for cand in (os.environ.get("GROK_CURSOR_WATCH_LABEL"),):
        if cand and _LABEL_RX.match(cand.strip()):
            return cand.strip()
    home = home or os.environ.get("GROK_CURSOR_HOME") or os.path.expanduser("~/.grok-cursor")
    try:
        with open(os.path.join(home, "watch-label"), encoding="utf-8") as fh:
            cand = fh.read().strip()
        if _LABEL_RX.match(cand):
            return cand
    except OSError:
        pass
    return DEFAULT_LABEL

LABEL = resolve_label()
# Re-POST an undecided decision_requested at most len(REPING_BACKOFF) times:
# 60 s after the first ping, then 120 s, then 300 s. Stops early once the
# request is decided, expired (gate TTL) or gone. State survives restarts.
REPING_BACKOFF = (60, 120, 300)
SCRATCH = ("gc-scratch-1", "gc-scratch-2")
SKIP_ERRORS = ("new_message_submitted",)      # Cursor closing a turn because a new prompt arrived
MINOR_RX = re.compile(r"^(?:Briefly inform the user about the task result|Perform any necessary follow-up"
                      r"|.*dev server (?:stopped|exited))", re.I | re.S)
UUID = gc_send.CursorAX._UUID
TAIL_BYTES = 256 * 1024


def _f(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def log(msg):
    sys.stderr.write("%s %s\n" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    sys.stderr.flush()


# ------------------------------------------------------------------ config

def load_env(home):
    """URL and key from the environment, else watch.env (KEY=VALUE lines)."""
    cfg = {}
    try:
        with open(os.path.join(home, "watch.env"), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                cfg[k.replace("export ", "").strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    url = os.environ.get("GROK_CURSOR_WATCH_URL") or cfg.get("GROK_CURSOR_WATCH_URL") or ""
    key = os.environ.get("GROK_CURSOR_WATCH_KEY") or cfg.get("GROK_CURSOR_WATCH_KEY") or ""
    return url, key


def url_ok(url):
    return bool(re.match(r"https://[^\s/]+|http://(127\.0\.0\.1|localhost)(:\d+)?(/|$)", url or ""))


def post(url, key, payload, tries=3):
    """HTTP status, or None after `tries` failures. 4xx is not retried."""
    data = json.dumps(payload, ensure_ascii=False).encode()
    for i in range(tries):
        req = urllib.request.Request(url, data, {"Content-Type": "application/json",
                                                 "Authorization": "Bearer " + key,
                                                 "User-Agent": "grok-cursor-watch"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(300).decode("utf-8", "replace")
            except Exception:
                body = ""
            exc.close()
            if exc.code < 500:
                if body:   # the webhook's own reason (never our payload or key)
                    log("webhook %d: %s" % (exc.code, " ".join(body.split())[:200]))
                return exc.code
        except (urllib.error.URLError, OSError):
            pass
        if i + 1 < tries:
            time.sleep(i + 1)
    return None


# ------------------------------------------------------------------ transcripts

def transcript_files(cursor_home):
    base = os.path.join(cursor_home, "projects")
    out = {}
    for p in glob.glob(os.path.join(base, "*", "agent-transcripts", "*", "*.jsonl")):
        stem = os.path.splitext(os.path.basename(p))[0]
        if os.path.basename(os.path.dirname(p)) == stem:      # skip subagent logs
            out[p] = (os.path.relpath(p, base).split(os.sep)[0], stem)
    return out


def _texts(obj):
    content = (obj.get("message") or {}).get("content") if isinstance(obj.get("message"), dict) else None
    if isinstance(content, str):
        return content
    return "\n".join(c.get("text") or "" for c in content or []
                     if isinstance(c, dict) and c.get("type") == "text")


def last_turn(path, end):
    """(assistant reply, user query) of the turn that ends at byte `end`."""
    try:
        with open(path, "rb") as fh:
            start = max(0, end - TAIL_BYTES)
            fh.seek(start)
            lines = fh.read(end - start).decode("utf-8", "replace").splitlines()
    except OSError:
        return "", ""
    if start:
        lines = lines[1:]                     # partial first line
    reply, query = [], ""
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "turn_ended" and (reply or query):
            break
        if obj.get("role") == "assistant" and not query:
            t = _texts(obj).strip()
            if t:
                reply.insert(0, t)
        elif obj.get("role") == "user":
            query = gc_send.norm(re.sub(r"<timestamp>.*?</timestamp>", "", _texts(obj), flags=re.S))
            m = re.search(r"<user_query>\s*(.*?)\s*</user_query>", query, re.S)
            query = m.group(1) if m else query
            break
    # The final message is what the user reads; earlier ones are progress notes.
    return (reply[-1] if reply else ""), query


def is_scratch(*names):
    return any(n and any(n == s or n.endswith("-" + s) for s in SCRATCH) for n in names)


# ------------------------------------------------------------------ CDP

def workbench_pages(port):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % port, timeout=2) as r:
            pages = json.loads(r.read().decode())
    except Exception:
        return None
    return [p for p in pages if isinstance(p, dict) and p.get("type") == "page"
            and "workbench" in (p.get("url") or "") and p.get("webSocketDebuggerUrl")]


def scan_cards(port, include_scratch, failures=None):
    """({chat_id: info}, complete). Read-only: only the `tabs` and `cards` page scripts run."""
    pages = workbench_pages(port)
    if pages is None:
        return None, False, {}
    found, complete = {}, True
    busy = {}
    for page in pages:
        label = gc_send.cdp_window_label(page.get("title"))
        if not include_scratch and is_scratch(label):
            continue
        try:
            ws = gc_send.WS(page["webSocketDebuggerUrl"], timeout=3)
        except SendAbort:
            complete = False
            continue
        try:
            for tab in gc_send.cdp_eval(ws, "tabs") or []:
                cid = tab.get("id") or "" if isinstance(tab, dict) else ""
                if not UUID.match(cid):
                    continue                  # browser / file tabs
                cards, status = [], ""
                if tab.get("selected"):
                    try:
                        snap = gc_send.cdp_eval(ws, "cards", id=cid) or {}
                    except SendAbort:
                        if failures is not None:
                            failures.append("cards")
                        continue
                    cards = snap.get("cards") or []
                    status = snap.get("status") or ""
                gc_card_events.record_tab(
                    found, busy, cid, gc_send.safe_title(tab.get("text")), label,
                    cards, status, tab.get("waiting"), tab.get("selected"))
        except SendAbort:
            complete = False
        finally:
            ws.close()
    return found, complete, busy


# ------------------------------------------------------------------ the watcher

class Watcher(object):
    def __init__(self, args, ctx, emit=None):
        self.a, self.ctx = args, ctx
        self.home = ctx["data_home"]()
        self.cursor = ctx["cursor_dir"]()
        self.state_path = os.path.join(self.home, "watch-state.json")
        self.debounce = _f("GROK_CURSOR_WATCH_DEBOUNCE", 1.2)
        self.emit = emit or self.send
        self.files = {}
        self.names, self.names_at = {}, 0
        self.last_glob = 0
        self.cdp_down = False
        st = self._load()
        self.first_run = st is None
        st = st or {}
        self.offsets = st.get("files") or {}          # path -> {"offset", "pending"}
        self.cards = st.get("cards") or {}            # chat_id -> last known card command ("" = unknown)
        self.posted = set(st.get("posted") or [])     # decision req_ids already handed to the webhook
        self.stalls = st.get("stalls") or {}
        self.repings = st.get("repings") or {}        # req_id -> {"n", "next", "payload"}; payload holds the nonce
        self.clock = time.time
        self.card_fails = st.get("card_fails") or {"streak": 0, "emitted": False}
        self.was_safe = None
        self.dirty = self.first_run           # persist "we've started" even with no chats yet
        self.outbox = gc_outbox.Outbox(self.home, clock=lambda: self.clock(), log=log)
        self.flush_lock = threading.Lock()

    def _load(self):
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                st = json.load(fh)
            return st if isinstance(st, dict) else None
        except (OSError, ValueError):
            return None

    def save(self):
        if not self.dirty:
            return
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        tmp = self.state_path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            self.prune_posted()
            json.dump({"files": self.offsets, "cards": self.cards, "posted": sorted(self.posted), "stalls": self.stalls,
                       "repings": self.repings, "card_fails": self.card_fails}, fh)
        os.replace(tmp, self.state_path)
        self.dirty = False

    def prune_posted(self):
        """Forget req_ids whose decision and outbox files are both gone (audit W2). Without the outbox
        nonce scan_decisions can never re-post them, so dropping the id is safe. Other keys stay."""
        for key in list(self.posted):
            if ":" in key or key in self.repings:
                continue
            if not os.path.exists(os.path.join(self.home, "decisions", key + ".json")) \
                    and not os.path.exists(os.path.join(self.home, "outbox", key)):
                self.posted.discard(key)

    def chat_info(self, cid, slug):
        if cid not in self.names and time.time() - self.names_at > 30:
            self.names_at = time.time()
            try:
                self.names = {t["id"]: t for t in gc_send.list_targets()}
            except Exception as exc:          # state DB locked / layout changed: names are optional
                log("chat names unavailable: %s" % type(exc).__name__)
        t = self.names.get(cid) or {}
        return t.get("title") or "", t.get("workspace_name") or slug

    # -- transcripts
    def scan_transcripts(self):
        if time.time() - self.last_glob >= 10 or not self.files:
            # first ever run: existing chats start at their end. Later: a new file is a new chat.
            known = bool(self.last_glob) or not self.first_run
            self.last_glob = time.time()
            self.files = transcript_files(self.cursor)
            for p in self.files:
                if p not in self.offsets:
                    size = 0 if known else self._size(p)
                    self.offsets[p] = {"offset": size, "pending": None}
                    self.dirty = True
            for p in [p for p in self.offsets if p not in self.files]:
                del self.offsets[p]
                self.dirty = True
        for path, (slug, cid) in self.files.items():
            self._follow(path, slug, cid)

    @staticmethod
    def _size(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

    def _follow(self, path, slug, cid):
        st = self.offsets[path]
        try:
            info = os.stat(path)
        except OSError:
            return
        if info.st_size < st["offset"]:
            st.update(offset=info.st_size, pending=None)
            self.dirty = True
        elif info.st_size > st["offset"]:
            with open(path, "rb") as fh:
                fh.seek(st["offset"])
                chunk = fh.read(info.st_size - st["offset"])
            cut = chunk.rfind(b"\n") + 1               # only whole lines
            pos = st["offset"]
            for raw in chunk[:cut].splitlines(True):
                pos += len(raw)
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("type") == "turn_ended":
                    err = str(obj.get("error") or "")
                    st["pending"] = None if err in SKIP_ERRORS else {
                        "end": pos, "status": str(obj.get("status") or ""), "error": err[:300]}
                elif obj.get("role") in ("user", "assistant"):
                    st["pending"] = None             # a new turn started: not done yet
            st["offset"] += cut
            self.dirty = True
        p = st.get("pending")
        if p and time.time() - info.st_mtime >= self.debounce:
            st["pending"] = None
            self.dirty = True
            self.finished(path, slug, cid, p)

    def finished(self, path, slug, cid, p):
        name, workspace = self.chat_info(cid, slug)
        if not self.a.include_scratch and is_scratch(slug, workspace):
            return
        reply, query = last_turn(path, p["end"])
        payload = {"event": "finished", "minor": bool(MINOR_RX.match(query or "")),
                   "chat_id": cid, "chat_name": name, "workspace": workspace,
                   "status": p["status"], "snippet": self.ctx["redact"](reply)[:500],
                   "card": None, "buttons": [], "note": p["error"], "expect": "",
                   "timestamp": now_iso(), "cdp_down": self.cdp_down}
        self.emit(payload)

    # -- approval cards
    def scan_approvals(self):
        port = self.a.cdp or int(os.environ.get("GROK_CURSOR_CDP_PORT", "9222"))
        failures = []
        found, complete, busy = (scan_cards(port, self.a.include_scratch, failures) if gc_send.cdp_listening(port)
                                 else (None, False, {}))
        down = found is None
        if down != self.cdp_down:
            log("CDP on 127.0.0.1:%d is %s" % (port, "down: transcripts only" if down else "up"))
            self.cdp_down = down
        if down:
            return {}
        import gc_resilience
        state, payload = gc_resilience.note_card_result(self.card_fails, not failures, failures)
        if state != self.card_fails:
            self.card_fails = state
            self.dirty = True
        if payload:
            gc_resilience.set_safe(self.home, True)
            self.emit(payload)
        for cid, info in found.items():
            cmd = gc_card_events.tracked_command(info["cards"])
            if cmd is None:                       # edit rows only: remember the chat, do not ping
                if cid not in self.cards:
                    self.cards[cid] = ""
                    self.dirty = True
                continue
            prev = self.cards.get(cid)
            if prev is None or (cmd and prev and cmd != prev):
                self.approval(cid, info, cmd)
            if prev != (cmd or prev or ""):
                self.cards[cid] = cmd or prev or ""
                self.dirty = True
        if complete:
            for cid in [c for c in self.cards if c not in found]:
                del self.cards[cid]                   # card gone: the next one is fresh
                self.dirty = True
        return busy

    def approval(self, cid, info, cmd):
        name, workspace = self.chat_info(cid, info["workspace"])
        payload = gc_card_events.approval_payload(
            cid, info, name, workspace, now_iso(), self.ctx["redact"])
        if payload:
            self.emit(payload)

    def scan_stalls(self, busy):
        import gc_gate
        sizes = {}
        for path, pair in self.files.items():
            sizes[pair[1]] = sizes.get(pair[1], 0) + self._size(path)
        window = gc_card_events.stall_seconds(gc_gate.load_config(self.home))
        before = json.dumps(self.stalls, sort_keys=True)
        emit = gc_card_events.consider_stalls(self.stalls, busy, sizes, time.time(), window)
        if json.dumps(self.stalls, sort_keys=True) != before:
            self.dirty = True
        for cid in emit:
            info = busy[cid]
            name, workspace = self.chat_info(cid, info.get("workspace") or "")
            self.emit(gc_card_events.stall_payload(cid, info, name, workspace, now_iso()))

    # -- output
    def send(self, payload):
        line = "%s %s %s %r%s" % (payload["event"], payload["chat_id"][:8], payload["workspace"],
                                  payload["chat_name"], " (minor)" if payload["minor"] else "")
        if self.a.dry_run:
            sys.stdout.write("PING %s\n" % json.dumps(payload, ensure_ascii=False))
            sys.stdout.flush()
            log("dry-run " + line)
            return
        # Durable: on disk first, deleted only after a 2xx. flush() retries with backoff forever.
        self.outbox.enqueue(payload)
        log("%s -> queued" % line)
        self.flush(block=self.a.once)

    def flush(self, block=False):
        """Deliver whatever is due. Runs in a thread so a slow webhook never stalls the scan."""
        url, key = load_env(self.home)
        if not url or not key:
            return
        def go():
            if not self.flush_lock.acquire(False):
                return
            try:
                delivered, left = self.outbox.flush(url, key, budget=20)
                if delivered:
                    log("delivered %d, %d still queued" % (delivered, left))
            finally:
                self.flush_lock.release()
        if block:
            go()
        else:
            threading.Thread(target=go, daemon=True).start()

    def scan_decisions(self):
        """Hand each decision_requested to the webhook with its one-time nonce, then delete the outbox file."""
        try:
            events = gc_events.read(home=self.home)
        except Exception:
            return
        for ev in events:
            if not isinstance(ev, dict) or ev.get("kind") != "decision_requested":
                continue
            req_id = str(ev.get("req_id") or "")
            if not req_id or req_id in self.posted:
                continue
            nonce_path = os.path.join(self.home, "outbox", req_id)
            try:
                with open(nonce_path, encoding="utf-8") as fh:
                    nonce = fh.read().strip()
            except OSError:
                continue
            if not nonce:
                continue
            chat = str(ev.get("conversation_id") or "")
            payload = {"event": "decision_requested", "minor": False,
                       "chat_id": chat, "chat": chat, "chat_name": "", "workspace": "",
                       "req_id": req_id, "nonce": nonce, "risk": ev.get("risk") or "",
                       "command": ev.get("command") or "", "mode": ev.get("mode") or "",
                       "decider": ev.get("decider") or "",
                       "reply": 'grok-cursor decide %s %s allow|deny --reason "..."' % (req_id, nonce),
                       "timestamp": now_iso(), "cdp_down": self.cdp_down}
            self.emit(payload)
            self.repings[req_id] = {"n": 0, "next": self.clock() + REPING_BACKOFF[0], "payload": payload}
            try:
                os.remove(nonce_path)
            except OSError:
                pass
            self.posted.add(req_id)
            self.dirty = True
        import gc_resilience
        posts, ids = gc_resilience.event_posts(events, self.posted)
        for payload, key in zip(posts, ids):
            self.emit(payload)
            self.posted.add(key)
            self.dirty = True
            if not gc_resilience.safe_on(self.home):      # stale selectors until `selftest` says otherwise
                gc_resilience.set_safe(self.home, True)
                log("cursor updated (%s): safe mode on until selftest passes" % payload.get("snippet", ""))

    def request_status(self, req_id):
        """'open' while the gate still waits on req_id, else why it is closed."""
        import gc_gate
        try:
            obj = gc_gate.load_decision(gc_gate.decision_path(self.home, req_id))
        except (OSError, ValueError):
            return "gone"
        if obj.get("decided"):
            return "decided"
        if not gc_gate._fresh(obj, gc_gate.ttl_seconds(self.home)):
            return "expired"
        return "open"

    def scan_repings(self):
        now = self.clock()
        for req_id in sorted(self.repings):
            r = self.repings[req_id]
            status = self.request_status(req_id)
            if status != "open":
                del self.repings[req_id]
                self.dirty = True
                log("reping %s stopped: %s" % (req_id, status))
                continue
            if now < float(r.get("next") or 0):
                continue
            n = int(r.get("n") or 0)
            if n >= len(REPING_BACKOFF):
                del self.repings[req_id]
                self.dirty = True
                log("reping %s stopped: cap %d reached" % (req_id, len(REPING_BACKOFF)))
                continue
            payload = dict(r.get("payload") or {}, repeat=n + 1, timestamp=now_iso())
            if os.environ.get("GROK_CURSOR_REPING_NONCE", "1") == "0":     # audit W3, opt-in
                payload.pop("nonce", None)
                payload["reply"] = "grok-cursor decide %s <nonce from the first ping> allow|deny" % req_id
            self.emit(payload)
            log("reping %s %d/%d" % (req_id, n + 1, len(REPING_BACKOFF)))
            n += 1
            if n >= len(REPING_BACKOFF):
                del self.repings[req_id]
                log("reping %s stopped: cap %d reached" % (req_id, len(REPING_BACKOFF)))
            else:
                r.update(n=n, next=now + REPING_BACKOFF[n])
            self.dirty = True

    def tick(self, cdp=True):
        import gc_resilience
        cards_on = gc_resilience.watch_cards_enabled(self.home)
        if cards_on and self.was_safe:                # safe mode turned off: fresh streak
            self.card_fails = {"streak": 0, "emitted": False}
            self.dirty = True
        self.was_safe = not cards_on
        busy = None
        if cdp and cards_on:
            busy = self.scan_approvals()  # first, so finished pings carry a fresh cdp_down
        self.scan_transcripts()
        if cdp and cards_on and not self.cdp_down:
            self.scan_stalls(busy or {})
        self.scan_decisions()
        self.scan_repings()
        if not self.a.dry_run and self.outbox.depth():
            self.flush(block=self.a.once)
        self.save()


def run(args, ctx):
    if not args.dry_run:
        url, key = load_env(ctx["data_home"]())
        if not url_ok(url) or not key:
            sys.stderr.write("grok-cursor watch: set GROK_CURSOR_WATCH_URL (https) and GROK_CURSOR_WATCH_KEY in %s, "
                             "or use --dry-run\n" % os.path.join(ctx["data_home"](), "watch.env"))
            if not getattr(args, "once", False):         # audit W4: no 10 s respawn loop on a bad config
                time.sleep(_f("GROK_CURSOR_WATCH_CONFIG_BACKOFF", 300.0))
            return 2
    w = Watcher(args, ctx)
    log("watching %s%s%s" % (os.path.join(w.cursor, "projects"), " (dry run)" if args.dry_run else "",
                             " (incl. scratch)" if args.include_scratch else ""))
    interval, cdp_every, last_cdp = _f("GROK_CURSOR_WATCH_INTERVAL", 1.0), _f("GROK_CURSOR_WATCH_CDP_EVERY", 3.0), 0
    while True:
        do_cdp = time.time() - last_cdp >= cdp_every
        if do_cdp:
            last_cdp = time.time()
        try:
            w.tick(cdp=do_cdp)
        except Exception as exc:               # one bad tick must not kill the daemon
            log("tick failed: %s: %s" % (type(exc).__name__, exc))
        if args.once:
            return 0
        time.sleep(interval)


# ------------------------------------------------------------------ launchd

def plist_path():
    d = os.environ.get("GROK_CURSOR_LAUNCH_AGENTS") or os.path.expanduser("~/Library/LaunchAgents")
    return os.path.join(d, LABEL + ".plist")


def plist_bytes(args, ctx):
    argv = [sys.executable, ctx["cli"], "watch", "run"]
    argv += ["--dry-run"] if args.dry_run else []
    argv += ["--include-scratch"] if args.include_scratch else []
    argv += ["--cdp", str(args.cdp)] if args.cdp else []
    logf = os.path.join(ctx["data_home"](), "watch.log")
    return plistlib.dumps({"Label": LABEL, "ProgramArguments": argv, "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False},
                           "ThrottleInterval": 10, "ProcessType": "Background", "LowPriorityIO": True,
                           "Nice": 10, "StandardOutPath": logf, "StandardErrorPath": logf,
                           "EnvironmentVariables": {"GROK_CURSOR_HOME": ctx["data_home"](),
                                                    "PYTHONUNBUFFERED": "1"}})


def launchctl(*a):
    try:
        return subprocess.run(["launchctl"] + list(a), capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None


def domain():
    return "gui/%d" % os.getuid()


def install(args, ctx):
    if not args.dry_run:
        url, key = load_env(ctx["data_home"]())
        if not url_ok(url) or not key:
            sys.stderr.write("grok-cursor watch install: no webhook configured. Put GROK_CURSOR_WATCH_URL (https) "
                             "and GROK_CURSOR_WATCH_KEY in %s (chmod 600), or install with --dry-run\n"
                             % os.path.join(ctx["data_home"](), "watch.env"))
            return 2
    path = plist_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.makedirs(ctx["data_home"](), mode=0o700, exist_ok=True)
    launchctl("bootout", "%s/%s" % (domain(), LABEL))
    with open(path, "wb") as fh:
        fh.write(plist_bytes(args, ctx))
    p = launchctl("bootstrap", domain(), path)
    if p is None or p.returncode != 0:
        sys.stderr.write("launchctl bootstrap failed: %s\n" % ((p.stderr.strip() if p else "launchctl missing")))
        return 1
    print("installed %s%s (starts at login, restarts if it dies)" % (path, " in dry-run mode" if args.dry_run else ""))
    return status(args, ctx)


def plist_home(path):
    try:
        with open(path, "rb") as fh:
            return (plistlib.load(fh).get("EnvironmentVariables") or {}).get("GROK_CURSOR_HOME") or ""
    except (OSError, ValueError, plistlib.InvalidFileException):
        return ""

def uninstall(args, ctx):
    path = plist_path()
    owner = plist_home(path) if os.path.exists(path) else ""
    if owner and os.path.realpath(owner) != os.path.realpath(ctx["data_home"]()):
        print("watch agent %s belongs to %s; left alone" % (path, owner))
        return 0
    launchctl("bootout", "%s/%s" % (domain(), LABEL))
    if os.path.exists(path):
        os.remove(path)
    print("removed %s" % LABEL)
    return 0


def status(args, ctx):
    home = ctx["data_home"]()
    url, key = load_env(home)
    host = re.sub(r"^https?://([^/]+).*", r"\1", url) if url else "not set"
    print("plist: %s (%s)" % (plist_path(), "present" if os.path.exists(plist_path()) else "missing"))
    p = launchctl("print", "%s/%s" % (domain(), LABEL))
    if p is None or p.returncode != 0:
        print("launchd: not loaded")
    else:
        get = lambda k: (re.search(r"^\s*%s = (.+)$" % k, p.stdout, re.M) or [None, "?"])[1]
        pid = get("pid")
        print("launchd: state = %s, pid = %s, runs = %s, last exit = %s" % (
            get("state"), pid, get("runs"), get("last exit code")))
        args_line = " ".join(re.findall(r"^\s{2,}(\S.*)$", p.stdout.split("arguments = {", 1)[-1].split("}", 1)[0], re.M))
        print("mode: %s" % ("dry run (pings are logged, not POSTed)" if "--dry-run" in args_line else "live"))
        if pid.isdigit():
            ps = subprocess.run(["ps", "-o", "%cpu=,rss=,etime=", "-p", pid], capture_output=True, text=True)
            print("process: cpu %%, rss KB, uptime = %s" % " ".join(ps.stdout.split()))
    print("webhook: %s, key %s" % (host, "set" if key else "MISSING"))
    s = gc_outbox.Outbox(home).summary()
    print("delivery: last success = %s, last error = %s%s" % (
        s["last_success"] or "never", s["last_error"] or "none",
        (" (at %s)" % s["last_error_at"]) if s["last_error"] else ""))
    print("queue: %d undelivered%s" % (s["depth"], (", oldest = %s queued %ds ago, %d attempts" % (
        s["oldest_event"], s["oldest_age_s"], s["oldest_attempts"])) if s["depth"] else ""))
    print("log: %s" % os.path.join(home, "watch.log"))
    return 0


def logs(args, ctx):
    path = os.path.join(ctx["data_home"](), "watch.log")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        print("no log yet: %s" % path)
        return 0
    sys.stdout.write("".join(lines[-args.lines:]))
    return 0


def cmd_watch(args, ctx):
    return {"run": run, "install": install, "uninstall": uninstall,
            "status": status, "logs": logs}[args.action](args, ctx)
