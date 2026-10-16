"""Keep-awake Phase 1: durations, --until, chip toggle, status, daemon ticks. No real pmset/sudo."""
import os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
import gc_awake as A

class FakePower(object):
    def __init__(self):
        self.on_battery, self.pct, self.low = False, 80, False
        self.therm = "nominal"
        self.sleep_off = False
        self.forms = ["-a"]
        self.calls = []
        self.alive_pids = set()
        self.killed = []
    def battery(self): return self.on_battery, self.pct, self.low
    def thermal(self): return self.therm
    def sleep_disabled(self): return self.sleep_off
    def sudo_forms(self): return list(self.forms)
    def set_disablesleep(self, on, form="-a"):
        self.calls.append((on, form)); self.sleep_off = bool(on); return True, ""
    def spawn_holder(self, pid, display_sleep):
        self.calls.append(("holder", pid, display_sleep)); return 4242
    def kill(self, pid): self.killed.append(pid)
    def alive(self, pid): return pid in self.alive_pids

class AwakeBase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="gc-awake-")
        self.p = FakePower()
    def start(self, **kw): return A.start(self.home, self.p, **kw)

class Durations(unittest.TestCase):
    def test_chips(self):
        self.assertEqual([A.parse_duration(c) for c in ("15m", "30m", "1h", "2h", "4h", "8h")],
                         [900, 1800, 3600, 7200, 14400, 28800])
        self.assertIsNone(A.parse_duration("inf"))
        for bad in ("", "0m", "abc", "1x", "-5m"):
            with self.subTest(bad=bad):
                self.assertRaises(ValueError, A.parse_duration, bad)
    def test_compound(self):
        self.assertEqual(A.parse_duration("1h30m"), 5400)
        self.assertEqual(A.parse_duration("90m"), 5400)

class Until(unittest.TestCase):
    def test_same_day(self):
        now = 1_700_000_000.0
        got = A.parse_until("18:30", now=now)
        self.assertGreater(got, now)
        self.assertLess(got - now, 86400)
    def test_crosses_midnight(self):
        base = __import__("datetime").datetime(2026, 10, 16, 23, 30)
        now = base.timestamp()
        got = A.parse_until("01:15", now=now)
        self.assertGreater(got, now)
        self.assertLessEqual(got - now, 2 * 3600)
        self.assertEqual(A.parse_until("11:30pm", now=now), A.parse_until("23:30", now=now))
    def test_bad(self):
        self.assertRaises(ValueError, A.parse_until, "25:99")
        self.assertRaises(ValueError, A.parse_until, "noon")

class ChipToggle(AwakeBase):
    def test_same_chip_stops(self):
        self.p.alive_pids.add(7)
        st, note = self.start(duration="1h", launch=lambda h: 7)
        self.assertEqual(note, "started")
        st2, note2 = self.start(duration="1h")
        self.assertIsNone(st2)
        self.assertIn("stopped", note2)
        self.assertIsNone(A.load_state(self.home))
    def test_different_duration_replaces(self):
        self.p.alive_pids.add(7)
        self.start(duration="1h", launch=lambda h: 7)
        st, note = self.start(duration="2h", launch=lambda h: 8)
        self.assertEqual(note, "started")
        self.assertEqual(st["duration"], "2h")
    def test_auto_does_not_override_manual(self):
        self.p.alive_pids.add(7)
        self.start(duration="1h", launch=lambda h: 7)
        st, note = self.start(mode="auto", duration="30m")
        self.assertIn("does not override", note)
        self.assertEqual(st["mode"], "manual")

class Session(AwakeBase):
    def test_start_stop_status(self):
        self.p.alive_pids.add(7)
        st, _ = self.start(duration="30m", launch=lambda h: 7, now=1000.0)
        self.assertAlmostEqual(st["end"], 1000.0 + 1800)
        s = A.status(self.home, self.p, now=1000.0)
        self.assertTrue(s["active"])
        self.assertEqual(s["remaining"], "30m")
        self.assertTrue(A.stop(self.home, self.p, "manual", now=1100.0))
        s2 = A.status(self.home, self.p, now=1100.0)
        self.assertFalse(s2["active"])
        self.assertEqual(s2["last_end"]["reason"], "manual")
    def test_extend(self):
        self.start(duration="30m", now=1000.0)
        st = A.extend(self.home, 15, now=1000.0)
        self.assertAlmostEqual(st["end"], 1000.0 + 1800 + 900)
    def test_inf_has_no_end(self):
        self.p.alive_pids.add(7)
        st, _ = self.start(duration="inf", launch=lambda h: 7, now=1000.0)
        self.assertIsNone(st["end"])
        self.assertEqual(A.status(self.home, self.p, now=1000.0)["remaining"], "indefinite")
    def test_lid_clamped_to_cap(self):
        st, _ = self.start(duration="inf", lid=True, now=1000.0)
        self.assertAlmostEqual(st["end"], 1000.0 + A.LID_CAP_DEFAULT)
        self.assertTrue(self.p.sleep_off)
        self.assertTrue((A.lid_flag(self.home) or {}).get("we_set"))

class Tick(AwakeBase):
    def session(self, **kw):
        kw.setdefault("now", 1000.0)
        return self.start(duration="1h", **kw)[0]
    def test_timer(self):
        st = self.session()
        self.assertEqual(A.tick(st, self.p, self.home, now=st["end"] - 1), "")
        self.assertEqual(A.tick(st, self.p, self.home, now=st["end"]), "timer")
    def test_battery(self):
        st = self.session()
        self.p.on_battery, self.p.pct = True, 20
        self.assertEqual(A.tick(st, self.p, self.home, now=1001.0), "battery")
        self.p.pct = 21
        self.assertEqual(A.tick(st, self.p, self.home, now=1001.0), "")
    def test_thermal_only_with_lid(self):
        st = self.session()
        self.p.therm = "serious"
        self.assertEqual(A.tick(st, self.p, self.home, now=1001.0), "")
        st["lid"] = True
        self.assertEqual(A.tick(st, self.p, self.home, now=1001.0), "thermal")

class DaemonLoop(AwakeBase):
    def test_ends_on_timer(self):
        self.start(duration="1h", now=1000.0)
        clock = {"t": 1000.0}
        def advance(_): clock["t"] += 3600
        d = A.Daemon(self.home, self.p, sleep=advance, clock=lambda: clock["t"])
        self.assertEqual(d.run(), 0)
        self.assertIsNone(A.load_state(self.home))
        self.assertEqual(A._read(self.home, A.LAST)["reason"], "timer")
        self.assertIn(("holder", os.getpid(), False), self.p.calls)
    def test_max_ticks_keeps_session(self):
        self.start(duration="inf", now=1000.0)
        d = A.Daemon(self.home, self.p, sleep=lambda _s: None, clock=lambda: 1000.0)
        self.assertEqual(d.run(max_ticks=2), 0)
        self.assertIsNotNone(A.load_state(self.home))


import argparse
import io
import json
from contextlib import redirect_stdout

class Assert(AwakeBase):
    def test_caffeinate_flags(self):
        self.assertEqual(A.caffeinate_argv(5), ["caffeinate", "-dimsu", "-w", "5"])
        self.assertEqual(A.caffeinate_argv(5, display_sleep=True), ["caffeinate", "-imsu", "-w", "5"])

    def test_session_line(self):
        st = {"mode": "manual", "end": 1000 + 2 * 3600 - 60, "holder_pid": 1234}
        self.assertEqual(A.session_line(st, now=1000), "Awake for 1h 59m (manual), caffeinate pid 1234")
        self.assertEqual(A.session_line({"mode": "auto", "end": None, "holder_pid": 9}, now=1),
                         "Awake indefinitely (auto), caffeinate pid 9")
        self.assertEqual(A.fmt_span(119), "1m 59s")
        self.assertEqual(A.fmt_span(120), "2m")

class Auto(AwakeBase):
    def enable(self):
        A._write(self.home, A.AUTO, {"enabled": True, "idle_seconds": 600})

    def launch(self, home):
        self.p.alive_pids.add(9)
        return 9

    def test_holds_while_generating_releases_after_idle(self):
        self.enable()
        self.assertEqual(A.auto_sync(self.home, self.p, False, now=1000, launch=self.launch), "")
        self.assertIsNone(A.load_state(self.home))
        self.assertEqual(A.auto_sync(self.home, self.p, True, now=1000, launch=self.launch), "held")
        self.assertEqual(A.load_state(self.home)["mode"], "auto")
        self.assertIsNone(A.load_state(self.home)["end"])
        self.assertEqual(A.auto_sync(self.home, self.p, True, now=1300, launch=self.launch), "held")
        self.assertEqual(A.auto_sync(self.home, self.p, False, now=1300 + 599, launch=self.launch), "idle")
        self.assertIsNotNone(A.load_state(self.home))
        self.assertEqual(A.auto_sync(self.home, self.p, False, now=1300 + 600, launch=self.launch), "released")
        self.assertIsNone(A.load_state(self.home))
        self.assertEqual(A._read(self.home, A.LAST)["reason"], "auto_idle")

    def test_never_overrides_manual(self):
        self.enable()
        self.p.alive_pids.add(7)
        A.start(self.home, self.p, duration="1h", launch=lambda h: 7, now=1000)
        calls = []
        self.assertEqual(A.auto_sync(self.home, self.p, True, now=1100, launch=lambda h: calls.append(h) or 9), "manual")
        self.assertEqual(calls, [])
        self.assertEqual(A.load_state(self.home)["mode"], "manual")

    def test_off_suppresses_until_the_chat_finishes(self):
        self.enable()
        A.auto_sync(self.home, self.p, True, now=1000, launch=self.launch)
        A._write(self.home, A.AUTO_SUPPRESS, {"ts": 1000})
        A.stop(self.home, self.p, "manual")
        self.p.alive_pids.discard(9)
        self.assertEqual(A.auto_sync(self.home, self.p, True, now=1010, launch=self.launch), "suppressed")
        self.assertIsNone(A.load_state(self.home))
        A.auto_sync(self.home, self.p, False, now=1020, launch=self.launch)
        self.assertIsNone(A._read(self.home, A.AUTO_SUPPRESS))
        self.assertEqual(A.auto_sync(self.home, self.p, True, now=1030, launch=self.launch), "held")

class OneLine(AwakeBase):
    def ctx(self):
        return {"data_home": lambda: self.home, "cli": "unused"}

    def launch(self, home):
        st = A.load_state(home) or {}
        st.update(pid=7, holder_pid=1234, holder_ready=True)
        A._write(home, A.STATE, st)
        self.p.alive_pids.add(7)
        return 7

    def cli(self, *parts, as_json=False):
        ns = argparse.Namespace(action=parts[0] if parts else None,
                                duration=parts[1] if len(parts) > 1 else None,
                                until=None, lid=False, display_sleep=False, json=as_json, idle=None)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = A.cmd_awake(ns, self.ctx(), power=self.p, launch=self.launch)
        return code, buf.getvalue().strip()

    def test_on_status_stop_are_one_line(self):
        code, line = self.cli("2h")
        self.assertEqual(code, 0)
        self.assertIn("(manual), caffeinate pid 1234", line)
        self.assertEqual(line.count("\n"), 0)
        code, raw = self.cli("status", as_json=True)
        payload = json.loads(raw)
        self.assertEqual(payload["caffeinate_pid"], 1234)
        self.assertTrue(payload["line"].startswith("Awake for "))
        code, line = self.cli("stop")
        self.assertEqual(line, "Awake off")

    def test_auto_line(self):
        code, line = self.cli("auto", "on")
        self.assertEqual(line, "Auto on, release 10m after the last chat finishes")
        code, line = self.cli("auto", "off")
        self.assertEqual(line, "Auto off")


if __name__ == "__main__":
    unittest.main()
