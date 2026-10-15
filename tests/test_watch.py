#!/usr/bin/env python3
"""Tests for `grok-cursor watch`: transcripts -> finished pings, CDP -> approval pings,
dedup across restarts, scratch filter, CDP-down fallback, webhook POST (key never logged)."""
import argparse
import http.server
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import gc_events  # noqa: E402
import gc_watch  # noqa: E402
from test_send import FakeCDP, C1, C2, PY, CLI  # noqa: E402

KEY = "wk_live_SUPERSECRETKEY1234567890"


def line(obj):
    return json.dumps(obj) + "\n"


def user(text):
    return line({"role": "user", "message": {"content": [
        {"type": "text", "text": "<timestamp>now</timestamp>\n<user_query>\n%s\n</user_query>" % text}]}})


def bot(text):
    return line({"role": "assistant", "message": {"content": [{"type": "text", "text": text}]}})


def ended(status="success", error=None):
    d = {"type": "turn_ended", "status": status}
    if error:
        d["error"] = error
    return line(d)


class WatchBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gc-watch-")
        self.cursor = os.path.join(self.tmp, "cursor")
        self.home = os.path.join(self.tmp, "gc")
        self.old_env = dict(os.environ)
        os.environ.update(CURSOR_USER_DIR=os.path.join(self.tmp, "nouser"), GROK_CURSOR_WATCH_DEBOUNCE="0",
                          GROK_CURSOR_CDP_PORT="1")
        os.environ.pop("GROK_CURSOR_WATCH_URL", None)
        os.environ.pop("GROK_CURSOR_WATCH_KEY", None)
        self.pings = []

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old_env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, cid, slug="Users-me-my-app"):
        d = os.path.join(self.cursor, "projects", slug, "agent-transcripts", cid)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, cid + ".jsonl")

    def write(self, cid, text, slug="Users-me-my-app"):
        with open(self.path(cid, slug), "a") as fh:
            fh.write(text)

    def watcher(self, cdp=None, scratch=False, dry=True):
        args = argparse.Namespace(dry_run=dry, include_scratch=scratch, cdp=cdp, once=True)
        ctx = {"data_home": lambda: self.home, "cursor_dir": lambda: self.cursor, "redact": lambda s: s}
        w = gc_watch.Watcher(args, ctx, emit=self.pings.append)
        return w

    def tick(self, w, cdp=True):
        with redirect_stderr(io.StringIO()) as err:
            w.last_glob = w.last_glob and 1     # force a re-glob every tick in tests
            w.tick(cdp=cdp)
        return err.getvalue()


class Transcripts(WatchBase):
    def test_finished_once_after_turn_ended(self):
        self.write(C1, user("old") + bot("old reply") + ended())
        w = self.watcher()
        self.tick(w)
        self.assertEqual(self.pings, [], "history before the first run is not re-pinged")
        self.write(C1, user("fix the bug") + bot("looking"))
        self.tick(w)
        self.assertEqual(self.pings, [], "no turn_ended yet: still working")
        self.write(C1, bot("Fixed in commit abc123.") + ended())
        self.tick(w)
        self.tick(w)
        self.assertEqual(len(self.pings), 1)
        p = self.pings[0]
        self.assertEqual((p["event"], p["chat_id"], p["minor"], p["status"]), ("finished", C1, False, "success"))
        self.assertEqual(p["snippet"], "Fixed in commit abc123.")
        self.assertEqual(p["workspace"], "Users-me-my-app")
        self.assertTrue(p["cdp_down"])

    def test_debounce_waits_for_quiet_file(self):
        os.environ["GROK_CURSOR_WATCH_DEBOUNCE"] = "30"
        w = self.watcher()
        self.tick(w)
        self.write(C1, user("q") + bot("a") + ended())
        self.tick(w)
        self.assertEqual(self.pings, [])
        old = time.time() - 60
        os.utime(self.path(C1), (old, old))
        self.tick(w)
        self.assertEqual(len(self.pings), 1)

    def test_turn_resumed_before_debounce_is_not_done(self):
        os.environ["GROK_CURSOR_WATCH_DEBOUNCE"] = "30"
        w = self.watcher()
        self.tick(w)
        self.write(C1, user("q") + bot("a") + ended())
        self.tick(w)
        self.write(C1, user("follow up"))
        old = time.time() - 60
        os.utime(self.path(C1), (old, old))
        self.tick(w)
        self.assertEqual(self.pings, [])

    def test_new_message_submitted_is_ignored(self):
        w = self.watcher()
        self.tick(w)
        self.write(C1, user("q") + bot("partial") + ended("error", "new_message_submitted"))
        self.tick(w)
        self.assertEqual(self.pings, [])

    def test_error_turn_is_reported(self):
        w = self.watcher()
        self.tick(w)
        self.write(C1, user("q") + ended("error", "User Provided API Key Rate Limit Exceeded"))
        self.tick(w)
        self.assertEqual(self.pings[0]["status"], "error")
        self.assertIn("Rate Limit", self.pings[0]["note"])

    def test_auto_follow_ups_are_minor(self):
        w = self.watcher()
        self.tick(w)
        self.write(C1, user("Briefly inform the user about the task result and perform any follow-up actions")
                   + bot("dev server stopped, nothing to do") + ended())
        self.tick(w)
        self.write(C2, user("Perform any necessary follow-up actions in response to the subagent") + bot("ok") + ended())
        self.tick(w)
        self.assertEqual([p["minor"] for p in self.pings], [True, True])

    def test_new_chat_after_start_is_read_from_zero(self):
        w = self.watcher()
        self.tick(w)
        self.write(C2, user("hi") + bot("hello") + ended())
        self.tick(w)
        self.assertEqual([p["chat_id"] for p in self.pings], [C2])

    def test_dedup_survives_restart(self):
        w = self.watcher()
        self.tick(w)
        self.write(C1, user("q") + bot("a") + ended())
        self.tick(w)
        self.assertEqual(len(self.pings), 1)
        w2 = self.watcher()
        self.tick(w2)
        self.assertEqual(len(self.pings), 1)
        self.write(C1, user("q2") + bot("a2") + ended())
        self.tick(w2)
        self.assertEqual([p["snippet"] for p in self.pings], ["a", "a2"])
        mode = os.stat(os.path.join(self.home, "watch-state.json")).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_turn_finished_while_stopped_is_pinged_on_restart(self):
        w = self.watcher()
        self.tick(w)
        self.write(C1, user("q") + bot("done while down") + ended())
        self.tick(self.watcher())
        self.assertEqual([p["snippet"] for p in self.pings], ["done while down"])

    def test_partial_line_waits(self):
        w = self.watcher()
        self.tick(w)
        full = user("q") + bot("a") + ended()
        self.write(C1, full[:-10])
        self.tick(w)
        self.assertEqual(self.pings, [])
        self.write(C1, full[-10:])
        self.tick(w)
        self.assertEqual(len(self.pings), 1)

    def test_scratch_skipped_unless_included(self):
        for scratch in (False, True):
            self.pings = []
            shutil.rmtree(self.tmp, ignore_errors=True)
            w = self.watcher(scratch=scratch)
            self.tick(w)
            self.write(C1, user("q") + bot("a") + ended(), slug="Users-me-gc-scratch-1")
            self.tick(w)
            self.assertEqual(len(self.pings), 1 if scratch else 0)

    def test_snippet_capped_and_last_message(self):
        w = self.watcher()
        self.tick(w)
        self.write(C1, user("q") + bot("progress note") + line({"role": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Shell"}]}}) + bot("x" * 900) + ended())
        self.tick(w)
        self.assertEqual(self.pings[0]["snippet"], "x" * 500)


class Approvals(WatchBase):
    def cdp(self):
        f = FakeCDP()
        f.tabs = [{"id": C1, "text": "Fix login bug", "selected": True},
                  {"id": C2, "text": "Refactor API layer", "selected": False},
                  {"id": "", "text": "localhost:3000", "selected": False, "waiting": True}]
        f.start()
        self.addCleanup(f.srv.close)
        return f

    def test_in_view_card_pinged_once_then_fresh_card_again(self):
        f = self.cdp()
        f.card = {"command": "npm test", "note": "", "buttons": ["Skip", "Run"]}
        w = self.watcher(cdp=f.port)
        self.tick(w)
        self.tick(w)
        self.assertEqual(len(self.pings), 1)
        p = self.pings[0]
        self.assertEqual((p["event"], p["chat_id"], p["expect"], p["buttons"]),
                         ("approval_needed", C1, "npm test", ["Skip", "Run"]))
        self.assertFalse(p["cdp_down"])
        f.card = {"command": "git push", "note": "", "buttons": ["Skip", "Run"]}
        self.tick(w)
        self.assertEqual([p["expect"] for p in self.pings], ["npm test", "git push"])
        f.card = None
        self.tick(w)
        f.card = {"command": "git push", "note": "", "buttons": ["Skip", "Run"]}
        self.tick(w)
        self.assertEqual(len(self.pings), 3, "card gone then back = fresh card")

    def test_background_tab_badge_and_no_clicks(self):
        f = self.cdp()
        f.tabs[1]["waiting"] = True
        w = self.watcher(cdp=f.port)
        self.tick(w)
        self.assertEqual([(p["chat_id"], p["card"]) for p in self.pings], [(C2, None)])
        self.assertIn("pending --to", self.pings[0]["note"])
        self.assertEqual(f.mouse, [], "watch never clicks")
        self.assertEqual(f.card_clicks, [])
        # restart: badge still there, not re-pinged
        self.tick(self.watcher(cdp=f.port))
        self.assertEqual(len(self.pings), 1)

    def test_scratch_window_skipped(self):
        f = self.cdp()
        f.page_titles = ["gc-scratch-1"]
        f.card = {"command": "ls", "note": "", "buttons": ["Skip", "Run"]}
        self.tick(self.watcher(cdp=f.port))
        self.assertEqual(self.pings, [])
        self.tick(self.watcher(cdp=f.port, scratch=True))
        self.assertEqual(len(self.pings), 1)

    def test_cdp_down_falls_back_to_transcripts(self):
        w = self.watcher(cdp=1)
        err = self.tick(w)
        self.assertIn("down", err)
        self.write(C1, user("q") + bot("a") + ended())
        self.tick(w)
        self.assertTrue(self.pings[0]["cdp_down"])

    def test_cdp_down_flag_on_first_tick_after_restart(self):
        f = self.cdp()
        w = self.watcher(cdp=f.port)
        self.tick(w)
        self.write(C1, user("q") + bot("a") + ended())
        self.tick(self.watcher(cdp=1))
        self.assertTrue(self.pings[0]["cdp_down"])


class Webhook(WatchBase):
    def server(self, status=200):
        got = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                got.append((self.headers.get("Authorization"), json.loads(body)))
                self.send_response(status)
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return "http://127.0.0.1:%d/hook" % srv.server_port, got

    def test_post_with_bearer_and_key_never_logged(self):
        url, got = self.server()
        os.makedirs(self.home)
        with open(os.path.join(self.home, "watch.env"), "w") as fh:
            fh.write("GROK_CURSOR_WATCH_URL=%s\nexport GROK_CURSOR_WATCH_KEY='%s'\n" % (url, KEY))
        w = self.watcher(dry=False)
        w.emit = w.send
        self.tick(w)
        self.write(C1, user("q") + bot("all done") + ended())
        err = self.tick(w)
        self.assertEqual(got[0][0], "Bearer " + KEY)
        self.assertEqual(got[0][1]["snippet"], "all done")
        self.assertIn("-> 200", err)
        self.assertNotIn(KEY, err)

    def test_retry_then_give_up_on_5xx(self):
        url, got = self.server(503)
        os.environ["GROK_CURSOR_WATCH_URL"], os.environ["GROK_CURSOR_WATCH_KEY"] = url, KEY
        t = time.time()
        self.assertIsNone(gc_watch.post(url, KEY, {"x": 1}, tries=2))
        self.assertEqual(len(got), 2)
        self.assertLess(time.time() - t, 5)

    def test_cli_refuses_without_config_and_rejects_plain_http(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GROK_CURSOR_WATCH")}
        env.update(GROK_CURSOR_HOME=self.home, CURSOR_HOME=self.cursor)
        p = subprocess.run([PY, CLI, "watch", "run", "--once"], capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 2)
        self.assertIn("watch.env", p.stderr)
        env.update(GROK_CURSOR_WATCH_URL="http://example.com/x", GROK_CURSOR_WATCH_KEY=KEY)
        p = subprocess.run([PY, CLI, "watch", "run", "--once"], capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 2)
        self.assertNotIn(KEY, p.stderr + p.stdout)

    def test_cli_dry_run_once_prints_pings(self):
        self.write(C1, user("q") + bot("a") + ended())
        env = dict(os.environ, GROK_CURSOR_HOME=self.home, CURSOR_HOME=self.cursor)
        p = subprocess.run([PY, CLI, "watch", "--dry-run", "--once"], capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "", "first run starts at the end of existing transcripts")
        self.write(C1, user("q2") + bot("a2") + ended())
        p = subprocess.run([PY, CLI, "watch", "--dry-run", "--once"], capture_output=True, text=True, env=env)
        ping = json.loads(p.stdout.split("PING ", 1)[1])
        self.assertEqual((ping["event"], ping["snippet"]), ("finished", "a2"))


class Launchd(WatchBase):
    def test_plist_and_install_guard(self):
        import plistlib
        args = argparse.Namespace(dry_run=True, include_scratch=False, cdp=None)
        ctx = {"data_home": lambda: self.home, "cli": "/x/grok-cursor"}
        pl = plistlib.loads(gc_watch.plist_bytes(args, ctx))
        self.assertEqual(pl["Label"], "com.grok-cursor.watch")
        self.assertEqual(pl["ProgramArguments"][1:], ["/x/grok-cursor", "watch", "run", "--dry-run"])
        self.assertTrue(pl["KeepAlive"] and pl["RunAtLoad"])
        self.assertEqual(pl["StandardErrorPath"], os.path.join(self.home, "watch.log"))
        self.assertNotIn("KEY", json.dumps(pl["EnvironmentVariables"]), "the key stays in watch.env")
        env = {k: v for k, v in os.environ.items() if not k.startswith("GROK_CURSOR_WATCH")}
        env.update(GROK_CURSOR_HOME=self.home, GROK_CURSOR_LAUNCH_AGENTS=os.path.join(self.tmp, "la"))
        p = subprocess.run([PY, CLI, "watch", "install"], capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 2)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "la")), "nothing installed without a webhook")


class Decisions(WatchBase):
    def test_nonce_is_posted_once_then_deleted(self):
        gc_events.append("decision_requested", home=self.home, req_id="r1", risk="medium",
                         command="touch /tmp/x", conversation_id=C1, mode="away", decider="grok")
        os.makedirs(os.path.join(self.home, "outbox"))
        with open(os.path.join(self.home, "outbox", "r1"), "w", encoding="utf-8") as fh:
            fh.write("abc123nonce\n")
        w = self.watcher()
        w.tick(cdp=False)
        self.assertEqual(len(self.pings), 1)
        ping = self.pings[0]
        self.assertEqual(ping["nonce"], "abc123nonce")
        self.assertEqual(ping["risk"], "medium")
        self.assertEqual(ping["command"], "touch /tmp/x")
        self.assertEqual(ping["chat"], C1)
        self.assertEqual(ping["reply"], 'grok-cursor decide r1 abc123nonce allow|deny --reason "..."')
        self.assertFalse(os.path.exists(os.path.join(self.home, "outbox", "r1")))
        w.tick(cdp=False)
        self.assertEqual(len(self.pings), 1)
        with open(os.path.join(self.home, "watch-state.json"), encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertIn("r1", saved["posted"])


class Repings(WatchBase):
    """Undecided decision_requested: first ping always, then at most 3 re-pings (60/120/300 s)."""

    def setUp(self):
        super().setUp()
        import gc_gate
        self.gate = gc_gate
        self.now = [1000.0]

    def request(self):
        obj = self.gate.create_request(self.home, "touch /tmp/x", "/tmp", "medium", "away", "grok", C1, "")
        return obj["req_id"], obj["_nonce"]

    def make(self):
        w = self.watcher()
        w.clock = lambda: self.now[0]
        return w

    def at(self, w, t):
        self.now[0] = 1000.0 + t
        with redirect_stderr(io.StringIO()) as err:
            w.tick(cdp=False)
            w.save()
        return err.getvalue()

    def count(self, rid):
        return sum(1 for p in self.pings if p.get("req_id") == rid)

    def test_first_ping_then_capped_backoff(self):
        rid, nonce = self.request()
        w = self.make()
        self.at(w, 0)
        self.assertEqual(self.count(rid), 1)
        self.assertNotIn("repeat", self.pings[0])
        self.at(w, 59)
        self.assertEqual(self.count(rid), 1)
        self.at(w, 60)
        self.assertEqual(self.count(rid), 2)
        self.assertEqual(self.pings[-1]["repeat"], 1)
        self.assertEqual(self.pings[-1]["nonce"], nonce)
        self.at(w, 179)
        self.assertEqual(self.count(rid), 2)
        self.at(w, 180)
        self.assertEqual(self.count(rid), 3)
        self.at(w, 479)
        self.assertEqual(self.count(rid), 3)
        log = self.at(w, 480)
        self.assertEqual(self.count(rid), 4)
        self.assertIn("cap 3 reached", log)
        for t in (600, 2000, 5000):
            self.at(w, t)
        self.assertEqual(self.count(rid), 4)
        self.assertNotIn(rid, w.repings)

    def test_new_req_ids_are_never_suppressed(self):
        w = self.make()
        rids = []
        for i in range(5):
            rid, _ = self.request()
            rids.append(rid)
            self.at(w, i * 5)
        for rid in rids:
            self.assertGreaterEqual(self.count(rid), 1, rid)
        self.assertEqual(len(self.pings), 5, "only first pings inside the first 60 s")

    def test_decided_stops_repings(self):
        rid, nonce = self.request()
        w = self.make()
        self.at(w, 0)
        self.gate.apply_decision(self.home, rid, nonce, "deny", reason="no")
        log = self.at(w, 60)
        self.assertEqual(self.count(rid), 1)
        self.assertIn("stopped: decided", log)
        self.assertNotIn(rid, w.repings)

    def test_expired_stops_repings(self):
        rid, _ = self.request()
        w = self.make()
        self.at(w, 0)
        path = self.gate.decision_path(self.home, rid)
        with open(path, encoding="utf-8") as fh:
            obj = json.load(fh)
        obj["created"] = "2000-01-01T00:00:00+00:00"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        log = self.at(w, 60)
        self.assertEqual(self.count(rid), 1)
        self.assertIn("stopped: expired", log)

    def test_gone_request_stops_repings(self):
        rid, _ = self.request()
        w = self.make()
        self.at(w, 0)
        os.remove(self.gate.decision_path(self.home, rid))
        self.at(w, 60)
        self.assertEqual(self.count(rid), 1)
        self.assertNotIn(rid, w.repings)

    def test_state_survives_restart(self):
        rid, _ = self.request()
        w = self.make()
        self.at(w, 0)
        self.at(w, 60)
        self.assertEqual(self.count(rid), 2)
        w2 = self.make()                    # restart: same home, fresh process
        self.assertIn(rid, w2.repings)
        self.at(w2, 61)
        self.assertEqual(self.count(rid), 2, "no duplicate first ping, no early re-ping after restart")
        self.at(w2, 180)
        self.assertEqual(self.count(rid), 3)
        self.assertEqual(self.pings[-1]["repeat"], 2)
        with open(os.path.join(self.home, "watch-state.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["repings"][rid]["n"], 2)
        self.assertEqual(oct(os.stat(os.path.join(self.home, "watch-state.json")).st_mode & 0o777), "0o600")

if __name__ == "__main__":
    unittest.main()
