#!/usr/bin/env python3
"""Phase B MCP tools: pending, approve, answer, decide, summary."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import grok_cursor_mcp as mcp  # noqa: E402


class PhaseBTools(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self._run = mcp.run_cli

        def fake(argv, stdin=None):
            self.calls.append((argv, stdin))
            return "ok", False, 0

        mcp.run_cli = fake

    def tearDown(self):
        mcp.run_cli = self._run

    def test_list_names_and_contracts(self):
        names = {t["name"] for t in mcp.TOOLS}
        self.assertTrue({"cursor_pending", "cursor_approve", "cursor_answer",
                         "cursor_decide", "cursor_summary"} <= names)
        approve = next(t for t in mcp.TOOLS if t["name"] == "cursor_approve")
        answer = next(t for t in mcp.TOOLS if t["name"] == "cursor_answer")
        decide = next(t for t in mcp.TOOLS if t["name"] == "cursor_decide")
        self.assertIn("human approved this exact action", approve["description"])
        self.assertIn("human approved this exact action", answer["description"])
        self.assertIn("Away mode", decide["description"])
        self.assertIn("medium risk only", decide["description"])
        self.assertIn("reason is required", decide["description"])

    def test_approve_and_answer_require_confirm(self):
        for name, args in (("cursor_approve", {"to": "c1", "action": "run"}),
                           ("cursor_answer", {"to": "c1", "option": "a.txt"})):
            text, err, code = mcp.call_tool(name, args)
            self.assertTrue(err)
            self.assertIn("confirm=true", text)
            self.assertIsNone(code)
        self.assertEqual(self.calls, [])

    def test_approve_match_and_answer_argv(self):
        text, err, code = mcp.call_tool("cursor_approve", {"confirm": True, "match": "ab12"})
        self.assertFalse(err)
        self.assertEqual(self.calls[-1][0], ["approve", "--match", "ab12"])
        mcp.call_tool("cursor_answer", {"confirm": True, "to": "chat", "option": "a.txt"})
        self.assertEqual(self.calls[-1][0], ["answer", "--to", "chat", "--option", "a.txt"])
        text, err, _code = mcp.call_tool("cursor_answer", {"confirm": True, "to": "chat", "option": "Always a.txt"})
        self.assertTrue(err)
        self.assertIn("Always", text)
        self.assertEqual(len(self.calls), 2)

    def test_decide_requires_reason_and_does_not_pass_human(self):
        text, err, _code = mcp.call_tool("cursor_decide", {"req_id": "ab", "nonce": "n", "decision": "allow"})
        self.assertTrue(err)
        self.assertIn("reason", text)
        self.assertEqual(self.calls, [])
        mcp.call_tool("cursor_decide", {"req_id": "ab", "nonce": "n", "decision": "allow", "reason": "test"})
        self.assertEqual(self.calls[-1][0], ["decide", "ab", "n", "allow", "--reason", "test"])
        self.assertNotIn("--human", self.calls[-1][0])

    def test_pending_and_summary_are_read_only_argv(self):
        mcp.call_tool("cursor_pending", {"to": "chat"})
        self.assertEqual(self.calls[-1][0][:3], ["pending", "--to", "chat"])
        mcp.call_tool("cursor_summary", {"since": "30m"})
        self.assertEqual(self.calls[-1][0], ["summary", "--since", "30m"])


if __name__ == "__main__":
    unittest.main()
