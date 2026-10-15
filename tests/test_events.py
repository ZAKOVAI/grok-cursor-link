#!/usr/bin/env python3
"""events.jsonl: append (0600, redacted), rotate at 5 MB, read --since, CLI `events`."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import gc_events  # noqa: E402

CLI = os.path.join(ROOT, "bin", "grok-cursor")
FAKE_GH = "gh" + "p_" + "fakefakefake" + "0123456789abcdefghijklmnop"

class Events(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="gc-ev-")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_append_mode_redact_and_read(self):
        gc_events.append("gate", home=self.home, command="echo " + FAKE_GH, risk="medium",
                         nested={"note": "Bearer " + "a" * 20})
        p = gc_events.path(self.home)
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        with open(p) as fh:
            raw = fh.read()
        self.assertNotIn(FAKE_GH, raw)
        self.assertNotIn("a" * 20, raw)
        rows = gc_events.read(home=self.home)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "gate")
        self.assertEqual(rows[0]["risk"], "medium")

    def test_since_filter_and_garbage_lines(self):
        p = gc_events.path(self.home)
        with open(p, "w") as fh:
            fh.write(json.dumps({"t": "2020-01-01T00:00:00+00:00", "kind": "old"}) + "\n")
            fh.write("not json\n")
        gc_events.append("new", home=self.home)
        self.assertEqual([r["kind"] for r in gc_events.read(1800, home=self.home)], ["new"])
        self.assertEqual(len(gc_events.read(home=self.home)), 2)

    def test_rotation(self):
        p = gc_events.path(self.home)
        with open(p, "w") as fh:
            fh.write("x" * (gc_events.MAX_BYTES + 1))
        gc_events.append("after", home=self.home)
        self.assertTrue(os.path.exists(p + ".1"))
        self.assertEqual([r["kind"] for r in gc_events.read(home=self.home)], ["after"])

    def test_parse_since(self):
        self.assertEqual(gc_events.parse_since("30m"), 1800)
        self.assertEqual(gc_events.parse_since("90s"), 90)
        self.assertEqual(gc_events.parse_since("2h"), 7200)
        self.assertEqual(gc_events.parse_since("5"), 300)
        with self.assertRaises(ValueError):
            gc_events.parse_since("soon")

    def test_cli_events(self):
        env = dict(os.environ, GROK_CURSOR_HOME=self.home)
        gc_events.append("decided", home=self.home, req_id="r1", reason="test")
        p = subprocess.run([sys.executable, CLI, "events", "--since", "30m", "--json"],
                           capture_output=True, text=True, env=env, timeout=15)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)[0]["req_id"], "r1")
        p = subprocess.run([sys.executable, CLI, "events"], capture_output=True, text=True, env=env, timeout=15)
        self.assertIn("decided req_id=r1 reason=test", p.stdout)
        p = subprocess.run([sys.executable, CLI, "events", "--since", "later"],
                           capture_output=True, text=True, env=env, timeout=15)
        self.assertEqual(p.returncode, 2)

class Mode(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="gc-mode-")
        self.env = dict(os.environ, GROK_CURSOR_HOME=self.home)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def mode(self, *a):
        return subprocess.run([sys.executable, CLI, "mode"] + list(a), capture_output=True, text=True,
                              env=self.env, timeout=15)

    def test_default_set_and_event(self):
        self.assertEqual(self.mode("status").stdout, "mode: present\n")
        p = self.mode("away")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("mode: away (was present)", p.stdout)
        path = os.path.join(self.home, "mode")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(self.mode("status").stdout, "mode: away\n")
        self.mode("present")
        rows = gc_events.read(home=self.home)
        self.assertEqual([(r["kind"], r["mode"], r["previous"]) for r in rows],
                         [("mode", "away", "present"), ("mode", "present", "away")])

    def test_garbage_mode_file_reads_present(self):
        with open(os.path.join(self.home, "mode"), "w") as fh:
            fh.write("yolo\n")
        self.assertEqual(self.mode("status").stdout, "mode: present\n")
        self.assertEqual(self.mode("sideways").returncode, 2)

if __name__ == "__main__":
    unittest.main()
