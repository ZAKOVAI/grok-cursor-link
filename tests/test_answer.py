#!/usr/bin/env python3
"""answer clicks an exact question option, then Continue. plan accept clicks Build."""
import json
import sys
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import gc_send  # noqa: E402
import test_cards  # noqa: E402
import test_send  # noqa: E402

PLAN_ID = "toolu_01Jps2MT5FStYy6x1fwpoQ8c"
QUESTION = {
    "kind": "question", "command": "Which file name?", "card_id": "",
    "options": ["a.txt", "b.txt"], "buttons": ["Skip", "Continue"],
    "ready": {"Skip": True, "Continue": False}, "note": "", "status": "completed",
}
PLAN = {
    "kind": "plan", "command": "Write a.txt probe", "card_id": PLAN_ID,
    "options": [], "buttons": ["View Plan", "Build", "Open menu"],
    "note": "", "status": "completed",
}


class AnswerPlan(test_send.SendBase):
    def setUp(self):
        super(AnswerPlan, self).setUp()
        self._answer = test_send.FakeCDP.answer
        identified = test_cards._identify(self._answer)

        def wrapped(cdp, msg):
            try:
                expr = msg.get("params", {}).get("expression") or ""
                marker = json.loads(expr.split("/*gc:", 1)[1].split("*/", 1)[0])
            except (IndexError, ValueError, AttributeError):
                return self._answer(cdp, msg)
            op = marker.get("op")
            if op == "cards":
                return identified(cdp, msg)
            if op == "press_option":
                return {"result": {"type": "object", "value": self._press_option(cdp, marker)}}
            if op == "press_continue":
                return {"result": {"type": "object", "value": self._press_continue(cdp)}}
            if op == "press_plan":
                return {"result": {"type": "object", "value": self._press_plan(cdp, marker)}}
            return self._answer(cdp, msg)

        test_send.FakeCDP.answer = wrapped
        self.addCleanup(setattr, test_send.FakeCDP, "answer", self._answer)

    def _press_option(self, cdp, marker):
        label = marker.get("label") or ""
        card = cdp.card or {}
        options = card.get("options") or []
        if card.get("kind") != "question" or label not in options or label.lower().startswith("always"):
            return {"found": True, "n": 1 if card else 0, "hits": 0, "options": options}
        cdp.card_clicks.append(label)
        ready = dict(card.get("ready") or {})
        ready["Continue"] = True
        card["ready"] = ready
        cdp.card = card
        return {"found": True, "n": 1, "hits": 1, "clicked": label, "continue_ready": True}

    def _press_continue(self, cdp):
        card = cdp.card or {}
        ready = card.get("ready") or {}
        if card.get("kind") != "question" or ready.get("Continue") is not True:
            return {"found": True, "n": 1 if card else 0, "hits": 0, "ready": ready}
        cdp.card_clicks.append("Continue")
        cdp.card = None
        return {"found": True, "n": 1, "hits": 1, "clicked": "Continue"}

    def _press_plan(self, cdp, marker):
        label = marker.get("label") or ""
        card = cdp.card or {}
        buttons = card.get("buttons") or []
        same = card.get("kind") == "plan" and (not marker.get("card_id") or marker.get("card_id") == card.get("card_id"))
        if not same or label not in buttons or label.lower().startswith("always"):
            return {"found": True, "n": 1 if same else 0, "hits": 0, "buttons": buttons}
        cdp.card_clicks.append(label)
        cdp.card = None
        return {"found": True, "n": 1, "hits": 1, "clicked": label, "card_id": card.get("card_id")}

    def card_chat(self, card):
        f = test_send.SendCDP.start(self)
        f.status = "waiting"
        f.card = dict(card)
        if isinstance(f.card.get("ready"), dict):
            f.card["ready"] = dict(f.card["ready"])
        f.tabs = [{"id": "other-chat", "text": "Fix login bug", "selected": False},
                  {"id": test_send.C2, "text": "Refactor API layer", "selected": True, "waiting": True}]
        return f

    def test_page_script_clicks_by_label(self):
        for op in ("press_option", "press_continue", "press_plan"):
            self.assertNotIn("elementFromPoint", gc_send.JS[op])
        self.assertIn("kind === 'question'", gc_send.JS["press_option"])
        self.assertIn("data-click-ready", gc_send.JS["press_continue"])
        self.assertIn("kind === 'plan'", gc_send.JS["press_plan"])
        self.assertNotIn("kind === 'edit'", gc_send.JS["press_plan"] + gc_send.JS["press_option"])

    def test_answer_clicks_the_exact_option_then_continue(self):
        f = self.card_chat(QUESTION)
        p = self.gc("answer", "--cdp", str(f.port), "--to", test_send.C2, "--option", "a.txt")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertEqual(f.card_clicks, ["a.txt", "Continue"])
        self.assertIsNone(f.card)

    def test_answer_refuses_a_label_that_is_not_listed(self):
        f = self.card_chat(QUESTION)
        p = self.gc("answer", "--cdp", str(f.port), "--to", test_send.C2, "--option", "c.txt")
        self.assertEqual(p.returncode, 33, p.stderr + p.stdout)
        self.assertEqual(f.card_clicks, [])

    def test_answer_refuses_an_always_label(self):
        card = dict(QUESTION, options=["Always a.txt", "b.txt"])
        f = self.card_chat(card)
        p = self.gc("answer", "--cdp", str(f.port), "--to", test_send.C2, "--option", "Always a.txt")
        self.assertEqual(p.returncode, 33, p.stderr + p.stdout)
        self.assertEqual(f.card_clicks, [])

    def test_answer_does_not_click_an_edit_row(self):
        f = self.card_chat({"kind": "edit", "command": "edit-me.txt", "card_id": "toolu_01LYYozpzYf6vkFGcUck1U3S",
                            "options": [], "buttons": [], "note": ""})
        p = self.gc("answer", "--cdp", str(f.port), "--to", test_send.C2, "--option", "a.txt")
        self.assertEqual(p.returncode, 33, p.stderr + p.stdout)
        self.assertEqual(f.card_clicks, [])

    def test_plan_accept_clicks_build(self):
        f = self.card_chat(PLAN)
        p = self.gc("plan", "--cdp", str(f.port), "--to", test_send.C2, "accept")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertEqual(f.card_clicks, ["Build"])
        self.assertIsNone(f.card)

    def test_plan_reject_clicks_nothing_without_a_reject_label(self):
        f = self.card_chat(PLAN)
        p = self.gc("plan", "--cdp", str(f.port), "--to", test_send.C2, "reject")
        self.assertEqual(p.returncode, 33, p.stderr + p.stdout)
        self.assertEqual(f.card_clicks, [])
        self.assertEqual(f.card["buttons"], ["View Plan", "Build", "Open menu"])

    def test_plan_does_not_click_an_edit_row(self):
        f = self.card_chat({"kind": "edit", "command": "edit-me.txt", "card_id": "toolu_01LYYozpzYf6vkFGcUck1U3S",
                            "options": [], "buttons": ["Undo", "Review"], "note": ""})
        p = self.gc("plan", "--cdp", str(f.port), "--to", test_send.C2, "accept")
        self.assertEqual(p.returncode, 33, p.stderr + p.stdout)
        self.assertEqual(f.card_clicks, [])


if __name__ == "__main__":
    unittest.main()
