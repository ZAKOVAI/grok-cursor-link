#!/usr/bin/env python3
"""Card identity: kind, card_id, options, status. Fixtures are the Phase 0 probe dumps."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import gc_send  # noqa: E402
import test_send  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures", "dom")


def _load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return json.load(fh)


def _identify(answer):
    """FakeCDP does not run the page script. Stamp the same fields the script returns."""
    def wrapped(self, msg):
        result = answer(self, msg)
        try:
            expr = msg.get("params", {}).get("expression") or ""
            marker = json.loads(expr.split("/*gc:", 1)[1].split("*/", 1)[0])
        except (IndexError, ValueError, AttributeError):
            return result
        if marker.get("op") != "cards":
            return result
        val = (result.get("result") or {}).get("value") or {}
        cid = marker.get("id") or ""
        stamped = []
        for card in val.get("cards") or []:
            if not isinstance(card, dict):
                stamped.append(card)
                continue
            info = dict(card)
            kind = info.get("kind") or "run"
            info["kind"] = kind
            info["options"] = list(info.get("options") or [])
            info.setdefault("status", self.status)
            if kind == "run":
                info["card_id"] = gc_send.run_card_id(cid, info.get("command") or "")
            else:
                info.setdefault("card_id", "")
            stamped.append(info)
        val["cards"] = stamped
        return result
    return wrapped


class ProbeFixtures(unittest.TestCase):
    def test_question_plan_edit_shapes(self):
        q = _load("question.json")
        self.assertIsNone(q["tool_call_id_on_card"])
        self.assertEqual(q["options"], ["a.txt", "b.txt"])
        ready = {a["label"]: a["data-click-ready"] for a in q["actions"]}
        self.assertEqual(ready, {"Skip": "true", "Continue": "false"})
        plan = _load("plan.json")
        self.assertEqual(plan["data-tool-call-id"], "toolu_01Jps2MT5FStYy6x1fwpoQ8c")
        self.assertEqual(plan["buttons"], ["View Plan", "Build", "Open menu"])
        self.assertIsNone(plan["footer"])
        edit = _load("edit.json")
        self.assertEqual(edit["data-tool-call-id"], "toolu_01LYYozpzYf6vkFGcUck1U3S")
        self.assertEqual(edit["buttons"], [])
        self.assertFalse(edit["keep_all"])
        self.assertNotIn("Accept", edit["review_bar_buttons"])

    def test_page_script_matchers(self):
        with open(os.path.join(ROOT, "lib", "gc_send.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("composer-questionnaire-toolbar-option-label", src)
        self.assertIn('data-component="split-button"', src)
        self.assertIn("kind: 'edit'", src)
        self.assertIn("kind === 'run'", src)
        self.assertIn("sha16(chatId + command)", src)

    def test_sha16_matches_hashlib(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        with open(os.path.join(ROOT, "lib", "gc_send.py"), encoding="utf-8") as fh:
            src = fh.read()
        js = src.split("// sha256-start", 1)[1].split("// sha256-end", 1)[0]
        text = "touch /tmp/gc-a-case5"
        js += "\nsha16(%s).then(h => process.stdout.write(h));\n" % json.dumps(text)
        p = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=15)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, hashlib.sha256(text.encode("utf-8")).hexdigest()[:16])

    def test_identical_commands_share_one_id(self):
        a = gc_send.run_card_id("chat", "touch /tmp/gc-a-case5")
        b = gc_send.run_card_id("chat", "touch /tmp/gc-a-case5")
        c = gc_send.run_card_id("chat", "touch /tmp/other")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(len(a), 16)


class PendingIdentity(test_send.SendBase):
    def setUp(self):
        super(PendingIdentity, self).setUp()
        self._answer = test_send.FakeCDP.answer
        test_send.FakeCDP.answer = _identify(self._answer)
        self.addCleanup(setattr, test_send.FakeCDP, "answer", self._answer)

    def card_chat(self, card=None):
        f = test_send.SendCDP.start(self)
        f.status = "waiting"
        f.card = dict(card or {"command": "Delete /tmp/x.txt", "note": "Deletes a file outside the repo",
                               "buttons": ["Skip", "Run"]})
        f.tabs = [{"id": "other-chat", "text": "Fix login bug", "selected": False},
                  {"id": test_send.C2, "text": "Refactor API layer", "selected": True, "waiting": True}]
        return f

    def test_pending_json_names_the_run_card(self):
        f = self.card_chat()
        p = self.gc("pending", "--cdp", str(f.port), "--to", test_send.C2, "--json")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        card = json.loads(p.stdout)["cards"][0]
        self.assertEqual(card["kind"], "run")
        self.assertEqual(card["options"], [])
        self.assertEqual(card["card_id"], gc_send.run_card_id(test_send.C2, card["command"]))
        self.assertEqual(f.card_clicks, [])

    def test_question_card_keeps_option_labels(self):
        f = self.card_chat(card={
            "kind": "question", "command": "Which file name?", "card_id": "",
            "options": ["a.txt", "b.txt"], "buttons": ["Skip", "Continue"],
            "status": "completed", "note": ""})
        p = self.gc("pending", "--cdp", str(f.port), "--to", test_send.C2, "--json")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        card = json.loads(p.stdout)["cards"][0]
        self.assertEqual(card["kind"], "question")
        self.assertEqual(card["card_id"], "")
        self.assertEqual(card["options"], ["a.txt", "b.txt"])
        self.assertEqual(f.card_clicks, [])


if __name__ == "__main__":
    unittest.main()
