#!/usr/bin/env python3
"""Card ping fields, question/plan events, and the stall clock."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import gc_card_events  # noqa: E402

CID = "c1111111-0000-4000-8000-000000000001"


def payload(cards, **info):
    body = {"cards": cards, "title": "Fix login bug", "workspace": "my-app"}
    body.update(info)
    return gc_card_events.approval_payload(CID, body, "Fix login bug", "my-app", "t0", lambda s: s)


class CardPings(unittest.TestCase):
    def test_run_card_is_approval_with_risk(self):
        ping = payload([{"command": "npm test", "note": "", "buttons": ["Skip", "Run"],
                         "kind": "run", "card_id": "abcd", "options": []}])
        self.assertEqual(ping["event"], "approval_needed")
        self.assertEqual(ping["card_id"], "abcd")
        self.assertEqual(ping["kind"], "run")
        self.assertEqual(ping["options"], [])
        self.assertEqual(ping["risk"], "low")
        self.assertTrue(ping["suggest_always"])
        self.assertEqual(ping["expect"], "npm test")

    def test_high_command_does_not_suggest_always(self):
        ping = payload([{"command": "git push", "buttons": ["Skip", "Run"], "kind": "run"}])
        self.assertEqual(ping["risk"], "high")
        self.assertFalse(ping["suggest_always"])

    def test_question_and_plan_events(self):
        question = payload([{"kind": "question", "command": "Which file?", "card_id": "",
                             "options": ["a.txt", "b.txt"], "buttons": ["Skip", "Continue"]}])
        self.assertEqual(question["event"], "question_needed")
        self.assertEqual(question["options"], ["a.txt", "b.txt"])
        self.assertFalse(question["suggest_always"])
        plan = payload([{"kind": "plan", "command": "Write a.txt", "card_id": "toolu_plan",
                         "buttons": ["View Plan", "Build", "Open menu"], "options": []}])
        self.assertEqual(plan["event"], "plan_needed")
        self.assertEqual(plan["card_id"], "toolu_plan")
        self.assertEqual(plan["risk"], "medium")
        self.assertFalse(plan["suggest_always"])

    def test_edit_rows_are_not_pinged(self):
        self.assertIsNone(payload([{"kind": "edit", "command": "edit-me.txt", "buttons": []}]))
        self.assertIsNone(gc_card_events.tracked_command(
            [{"kind": "edit", "command": "edit-me.txt"}]))
        mixed = payload([
            {"kind": "edit", "command": "edit-me.txt", "buttons": []},
            {"kind": "question", "command": "Which file?", "options": ["a.txt"], "buttons": ["Continue"]},
        ])
        self.assertEqual(mixed["event"], "question_needed")
        self.assertEqual(mixed["expect"], "Which file?")

    def test_unknown_card_stays_approval_needed(self):
        ping = payload([{"command": "npm test", "buttons": ["Skip", "Run"]}])
        self.assertEqual(ping["event"], "approval_needed")
        self.assertEqual(ping["kind"], "")
        self.assertTrue(ping["suggest_always"])


class Stall(unittest.TestCase):
    def test_window_defaults_to_ten_minutes(self):
        self.assertEqual(gc_card_events.stall_seconds({}), 600)
        self.assertEqual(gc_card_events.stall_seconds({"stall_minutes": 1}), 60)
        self.assertEqual(gc_card_events.stall_seconds({"stall_minutes": "nope"}), 600)

    def test_busy_ignores_idle_and_errors(self):
        self.assertTrue(gc_card_events.is_busy("generating"))
        self.assertFalse(gc_card_events.is_busy("completed"))
        self.assertFalse(gc_card_events.is_busy("cancelled"))
        self.assertFalse(gc_card_events.is_busy(""))

    def test_stall_emits_once_then_resets_on_growth(self):
        stalls, busy = {}, {"c1": {"title": "Fix", "workspace": "app", "status": "generating"}}
        self.assertEqual(gc_card_events.consider_stalls(stalls, busy, {"c1": 10}, 1000, 600), [])
        self.assertEqual(gc_card_events.consider_stalls(stalls, busy, {"c1": 10}, 1500, 600), [])
        self.assertEqual(gc_card_events.consider_stalls(stalls, busy, {"c1": 10}, 1600, 600), ["c1"])
        self.assertEqual(gc_card_events.consider_stalls(stalls, busy, {"c1": 10}, 2000, 600), [])
        self.assertEqual(gc_card_events.consider_stalls(stalls, busy, {"c1": 20}, 2000, 600), [])
        self.assertEqual(gc_card_events.consider_stalls(stalls, busy, {"c1": 20}, 2600, 600), ["c1"])
        self.assertEqual(gc_card_events.consider_stalls(stalls, {}, {"c1": 20}, 3000, 600), [])
        self.assertEqual(stalls, {})

    def test_stall_payload_names_the_chat(self):
        ping = gc_card_events.stall_payload(
            CID, {"title": "Fix", "workspace": "app", "status": "generating"},
            "Fix login bug", "my-app", "t0")
        self.assertEqual(ping["event"], "chat_stalled")
        self.assertEqual(ping["chat_id"], CID)
        self.assertEqual(ping["status"], "generating")
        self.assertFalse(ping["minor"])


if __name__ == "__main__":
    unittest.main()
