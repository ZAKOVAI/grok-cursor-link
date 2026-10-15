"""--to resolves a unique workspace name and refuses when several chats match."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import gc_send  # noqa: E402

A = "c1111111-0000-4000-8000-000000000001"
B = "c2222222-0000-4000-8000-000000000002"
C = "c3333333-0000-4000-8000-000000000003"


def chat(cid, title, workspace, open_=False):
    return {"id": cid, "title": title, "workspace_name": workspace, "open": open_}


class ResolveWorkspace(unittest.TestCase):
    def test_unique_workspace_name(self):
        targets = [chat(A, "notes", "gc-scratch-2", True), chat(B, "other", "gc-scratch-1", True)]
        got, err = gc_send.resolve_target("gc-scratch-2", targets)
        self.assertIsNone(err)
        self.assertEqual(got["id"], A)

    def test_loose_workspace_name(self):
        targets = [chat(A, "notes", "gc-scratch-2", True)]
        got, err = gc_send.resolve_target("GC Scratch 2", targets)
        self.assertIsNone(err)
        self.assertEqual(got["id"], A)

    def test_two_open_chats_in_one_workspace_refuse(self):
        targets = [chat(A, "one", "gc-scratch-2", True), chat(B, "two", "gc-scratch-2", True)]
        got, err = gc_send.resolve_target("gc-scratch-2", targets)
        self.assertIsNone(got)
        self.assertIn("2 chats", err)
        self.assertIn(A[:8], err)
        self.assertIn(B[:8], err)

    def test_open_chat_wins_over_a_closed_one(self):
        targets = [chat(A, "old", "gc-scratch-2", False), chat(B, "live", "gc-scratch-2", True)]
        got, err = gc_send.resolve_target("gc-scratch-2", targets)
        self.assertIsNone(err)
        self.assertEqual(got["id"], B)

    def test_substring_that_hits_two_workspaces_refuses(self):
        targets = [chat(A, "one", "gc-scratch-1", True), chat(B, "two", "gc-scratch-2", True)]
        got, err = gc_send.resolve_target("scratch", targets)
        self.assertIsNone(got)
        self.assertIn("2 chats", err)

    def test_id_still_wins(self):
        targets = [chat(A, "notes", "gc-scratch-2", True)]
        got, err = gc_send.resolve_target(A, targets)
        self.assertIsNone(err)
        self.assertEqual(got["id"], A)

    def test_id_prefix_collision_does_not_guess(self):
        one = "c1111111-0000-4000-8000-000000000001"
        two = "c1111111-0000-4000-8000-000000000002"
        targets = [chat(one, "a", "alpha", True), chat(two, "b", "beta", True)]
        got, err = gc_send.resolve_target("c1111111", targets)
        self.assertIsNone(got)
        self.assertIn("id prefix", err)

    def test_closed_workspace_still_resolves_when_it_is_the_only_chat(self):
        targets = [chat(C, "done", "gc-scratch-2", False)]
        got, err = gc_send.resolve_target("gc-scratch-2", targets)
        self.assertIsNone(err)
        self.assertEqual(got["id"], C)

    def test_unknown_name_still_misses(self):
        targets = [chat(A, "notes", "gc-scratch-2", True)]
        got, err = gc_send.resolve_target("no-such-workspace", targets)
        self.assertIsNone(got)
        self.assertIsNone(err)
