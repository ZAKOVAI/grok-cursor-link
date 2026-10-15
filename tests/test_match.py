#!/usr/bin/env python3
"""approve --match: one fresh allow, one run card, same command, then click Run."""
import datetime
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_cards  # noqa: E402
import test_send  # noqa: E402

CMD = "Delete /tmp/x.txt"


def _data_home(test):
    return os.path.join(test.home, "." + "grok-cursor")


def _allow(test, req_id, cmd, age_s=0, decided="allow"):
    home = _data_home(test)
    os.makedirs(os.path.join(home, "decisions"), mode=0o700, exist_ok=True)
    when = datetime.datetime.now().astimezone() - datetime.timedelta(seconds=age_s)
    obj = {
        "req_id": req_id,
        "cmd": cmd,
        "decided": decided,
        "decided_at": when.isoformat(timespec="seconds"),
        "conversation_id": test_send.C2,
    }
    with open(os.path.join(home, "decisions", req_id + ".json"), "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


class MatchApprove(test_send.SendBase):
    extra = None

    def setUp(self):
        super(MatchApprove, self).setUp()
        self._answer = test_send.FakeCDP.answer
        identified = test_cards._identify(self._answer)

        def wrapped(cdp, msg):
            result = identified(cdp, msg)
            if not MatchApprove.extra:
                return result
            try:
                expr = msg.get("params", {}).get("expression") or ""
                marker = json.loads(expr.split("/*gc:", 1)[1].split("*/", 1)[0])
            except (IndexError, ValueError, AttributeError):
                return result
            if marker.get("op") != "cards":
                return result
            val = (result.get("result") or {}).get("value") or {}
            val["cards"] = list(val.get("cards") or []) + [dict(MatchApprove.extra)]
            return result

        test_send.FakeCDP.answer = wrapped
        MatchApprove.extra = None
        self.addCleanup(setattr, test_send.FakeCDP, "answer", self._answer)
        self.addCleanup(setattr, MatchApprove, "extra", None)

    def card_chat(self, card=None):
        f = test_send.SendCDP.start(self)
        f.status = "waiting"
        f.card = dict(card or {"command": CMD, "note": "", "buttons": ["Skip", "Run"]})
        f.tabs = [{"id": "other-chat", "text": "Fix login bug", "selected": False},
                  {"id": test_send.C2, "text": "Refactor API layer", "selected": True, "waiting": True}]
        return f

    def approve(self, f, req_id):
        return self.gc("approve", "--cdp", str(f.port), "--to", test_send.C2, "--match", req_id)

    def test_match_refuses_wrong_label(self):
        f = self.card_chat(card={"command": CMD, "note": "", "buttons": ["Skip", "Go"]})
        _allow(self, "m-label", CMD)
        p = self.approve(f, "m-label")
        self.assertEqual(p.returncode, 33, p.stderr + p.stdout)
        self.assertEqual(f.card_clicks, [])

    def test_match_refuses_wrong_command(self):
        f = self.card_chat()
        _allow(self, "m-cmd", "echo nope")
        p = self.approve(f, "m-cmd")
        self.assertEqual(p.returncode, 33, p.stderr + p.stdout)
        self.assertEqual(f.card_clicks, [])

    def test_match_refuses_stale_decision(self):
        f = self.card_chat()
        home = _data_home(self)
        os.makedirs(home, mode=0o700, exist_ok=True)
        with open(os.path.join(home, "config.json"), "w", encoding="utf-8") as fh:
            json.dump({"match_window": 5}, fh)
        _allow(self, "m-age", CMD, age_s=6)
        p = self.approve(f, "m-age")
        self.assertEqual(p.returncode, 33, p.stderr + p.stdout)
        self.assertIn("old", p.stderr)
        self.assertEqual(f.card_clicks, [])

    def test_match_refuses_two_cards(self):
        f = self.card_chat()
        MatchApprove.extra = {"command": "echo other", "note": "", "buttons": ["Skip", "Run"], "kind": "run"}
        _allow(self, "m-count", CMD)
        p = self.approve(f, "m-count")
        self.assertEqual(p.returncode, 33, p.stderr + p.stdout)
        self.assertEqual(f.card_clicks, [])

    def test_match_clicks_run_when_the_command_matches(self):
        f = self.card_chat()
        _allow(self, "m-ok", "Delete   /tmp/x.txt")
        p = self.approve(f, "m-ok")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertEqual(f.card_clicks, ["Run"])
        self.assertIn("card gone", p.stdout)


if __name__ == "__main__":
    unittest.main()
