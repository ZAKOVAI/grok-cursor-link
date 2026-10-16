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

if __name__ == "__main__":
    unittest.main()
