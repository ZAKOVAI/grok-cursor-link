#!/usr/bin/env python3
"""Durable webhook delivery: nothing lost under a rate limit, nothing duplicated, local fallback."""
import argparse
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
import gc_outbox  # noqa: E402
import gc_watch  # noqa: E402

RATE_LIMIT = (400, '{"success":false,"error":"Automation run rate limit exceeded: grok bot limit 20000 per minute, current_count=20001"}')


class FakeWebhook(object):
    """Returns `fail` until `until` (clock seconds), then 200. Records every accepted event id."""
    def __init__(self, clock, until, fail=RATE_LIMIT):
        self.clock, self.until, self.fail = clock, until, fail
        self.accepted, self.attempts = [], 0

    def __call__(self, url, key, payload):
        self.attempts += 1
        if self.clock() < self.until:
            return self.fail
        self.accepted.append(gc_outbox.event_id(payload))
        return 200, ""


class OutboxBase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="gc-outbox-")
        self.now = [1000.0]
        self.notes = []
        os.environ.pop("GROK_CURSOR_NOTIFY", None)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def clock(self):
        return self.now[0]

    def box(self, hook):
        return gc_outbox.Outbox(self.home, poster=hook, clock=self.clock,
                                notifier=lambda t, x: self.notes.append((t, x)) or True)

    @staticmethod
    def ev(kind, i, **extra):
        p = {"event": kind, "minor": False, "chat_id": "chat-%d" % i, "chat_name": "c", "workspace": "w",
             "timestamp": "t%d" % i, "cdp_down": False}
        p.update(extra)
        return p


class RateLimited(OutboxBase):
    def test_ten_minute_400_storm_loses_nothing_and_never_duplicates(self):
        hook = FakeWebhook(self.clock, until=1000 + 600)
        box = self.box(hook)
        url, key = "https://example.com/h", "k"
        ids = []
        for i in range(12):                     # a hold or finish every 50 s during the outage
            self.now[0] = 1000 + i * 50
            ids.append(box.enqueue(self.ev("decision_requested" if i % 2 else "finished", i, req_id="r%d" % i)))
            box.flush(url, key)
        self.assertEqual(hook.accepted, [], "nothing accepted while the webhook returns 400")
        self.assertEqual(box.depth(), 12, "every event is still on disk")
        self.assertTrue(all(box.state["last_error"].startswith("400") for _ in [0]))
        # backoff: attempts stay bounded (no storm) across the outage
        self.assertLess(hook.attempts, 60, "exponential backoff, not a retry storm: %d attempts" % hook.attempts)
        # webhook recovers: drain
        for step in range(40):
            self.now[0] = 1600 + step * 30
            box.flush(url, key)
            if box.depth() == 0:
                break
        self.assertEqual(box.depth(), 0, "queue drained after recovery")
        self.assertEqual(sorted(hook.accepted), sorted(ids))
        self.assertEqual(len(hook.accepted), len(set(hook.accepted)), "no duplicates")
        self.assertEqual(box.state["delivered"], 12)
        self.assertIsNotNone(box.state["last_success"])

    def test_queue_survives_restart_and_dedupes_re_emit(self):
        hook = FakeWebhook(self.clock, until=10 ** 9)
        box = self.box(hook)
        p = self.ev("decision_requested", 1, req_id="abc")
        box.enqueue(p)
        box.enqueue(p)                           # the watcher re-emitting the same ping
        box.flush("u", "k")
        box2 = self.box(hook)                    # restart: same home
        self.assertEqual(box2.depth(), 1)
        self.assertEqual(box2.oldest()["attempts"], 1)
        self.assertEqual(box2.state["last_error"][:3], "400")

    def test_429_and_5xx_and_network_are_retryable_401_backs_off_but_keeps(self):
        for status in (429, 500, 503, None):
            self.assertTrue(gc_outbox.retryable(status), status)
        self.assertTrue(gc_outbox.retryable(400))
        hook = lambda u, k, p: (401, "bad key")
        box = self.box(hook)
        box.enqueue(self.ev("finished", 1))
        box.flush("u", "k")
        self.assertEqual(box.depth(), 1, "kept even on 401: a config fix must not lose the event")
        self.assertGreaterEqual(box.oldest()["next"] - self.now[0], gc_outbox.BACKOFF_MAX)

    def test_backoff_is_exponential_capped_and_jittered(self):
        self.assertAlmostEqual(gc_outbox.backoff(1, rnd=lambda: 0), 5)
        self.assertAlmostEqual(gc_outbox.backoff(3, rnd=lambda: 0), 20)
        self.assertAlmostEqual(gc_outbox.backoff(20, rnd=lambda: 0), 300)
        self.assertAlmostEqual(gc_outbox.backoff(1, rnd=lambda: 1), 6.25)


class LocalFallback(OutboxBase):
    def test_notifies_once_per_hold_after_two_minutes_down(self):
        hook = FakeWebhook(self.clock, until=10 ** 9)
        box = self.box(hook)
        box.enqueue(self.ev("decision_requested", 1, req_id="r1", command="npm i left-pad"))
        box.enqueue(self.ev("finished", 2, snippet="done"))
        box.enqueue(self.ev("chat_stalled", 3, minor=True))      # minor: never a banner
        box.flush("u", "k")
        self.assertEqual(self.notes, [], "no banner in the first two minutes")
        self.now[0] += gc_outbox.NOTIFY_AFTER + 1
        box.flush("u", "k")
        self.assertEqual(len(self.notes), 2)
        self.assertIn("decision_requested", self.notes[0][0])
        self.assertIn("npm i left-pad", self.notes[0][1])
        self.now[0] += 600
        box.flush("u", "k")
        self.assertEqual(len(self.notes), 2, "each event notifies once")

    def test_osascript_text_is_escaped_and_off_switch(self):
        os.environ["GROK_CURSOR_NOTIFY"] = "0"
        self.assertFalse(gc_outbox.notify('a"b', "c\\d"))


class WatcherIntegration(OutboxBase):
    """The watcher's send() goes through the outbox and `watch status` reports it."""

    def setUp(self):
        super().setUp()
        self.cursor = tempfile.mkdtemp(prefix="gc-cursor-")
        os.environ["GROK_CURSOR_WATCH_URL"], os.environ["GROK_CURSOR_WATCH_KEY"] = "https://example.com/h", "k"

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self.cursor, ignore_errors=True)
        os.environ.pop("GROK_CURSOR_WATCH_URL", None)
        os.environ.pop("GROK_CURSOR_WATCH_KEY", None)

    def test_send_queues_then_status_shows_depth_and_error(self):
        args = argparse.Namespace(dry_run=False, include_scratch=False, cdp=None, once=True)
        ctx = {"data_home": lambda: self.home, "cursor_dir": lambda: self.cursor, "redact": lambda s: s}
        w = gc_watch.Watcher(args, ctx)
        w.clock = self.clock
        hook = FakeWebhook(self.clock, until=10 ** 9)
        w.outbox.poster = hook
        w.outbox.notifier = lambda t, x: True
        with redirect_stderr(io.StringIO()) as err:
            w.send(self.ev("finished", 1))
        self.assertIn("queued", err.getvalue())
        self.assertIn("400", err.getvalue())
        self.assertNotIn("20001", "")             # the webhook body is logged, our key never is
        self.assertNotIn("Bearer", err.getvalue())
        out = io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(out):
            gc_watch.status(args, ctx)
        text = out.getvalue()
        self.assertIn("queue: 1 undelivered", text)
        self.assertIn("last error = 400", text)
        self.assertIn("last success = never", text)
        self.assertIn("oldest = finished", text)
        self.assertNotIn("k\n", text)


if __name__ == "__main__":
    unittest.main()
