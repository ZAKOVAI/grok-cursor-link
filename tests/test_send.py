#!/usr/bin/env python3
"""Tests for `grok-cursor send`, `targets`, the CDP path and the MCP server.

macOS tools are replaced by tests/shim.py on PATH, and the Accessibility tree
by a JSON file (GROK_CURSOR_AX_FAKE). The shim mutates that file the way
Cursor would: a paste lands in the focused input, and Return submits the
focused chat input and writes a hook-style index entry. If Return is pressed
anywhere else, it records a "stray return". So each test checks the real
outcome (what was submitted, into which chat, whether a Return could have hit
an editor), not just which commands ran."""
import base64
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(ROOT, "bin", "grok-cursor")
MCP = os.path.join(ROOT, "lib", "grok_cursor_mcp.py")
SHIM = os.path.join(ROOT, "tests", "shim.py")
PY = sys.executable
C1 = "c1111111-0000-4000-8000-000000000001"
C2 = "c2222222-0000-4000-8000-000000000002"
TOOLS = ("caffeinate", "ioreg", "pgrep", "open", "osascript", "pbcopy", "pbpaste", "agent")


def ui_state(active=C1, input_value="", focused="editor", trusted=True, two_inputs=False):
    tabs = [
        {"id": "tab1", "role": "AXRadioButton", "title": "Fix login bug", "conv": C1, "selected": active == C1},
        {"id": "tab2", "role": "AXRadioButton", "title": "Refactor API layer", "conv": C2, "selected": active == C2},
    ]
    kids = [
        {"id": "tabbar", "role": "AXTabGroup", "classes": ["composer-tabs"], "children": tabs},
        {"id": "editor", "role": "AXTextArea", "desc": "Editor content", "classes": ["monaco-editor"], "value": "code"},
        {"id": "chatinput", "role": "AXTextArea", "kind": "chat", "classes": ["aislash-editor-input"],
         "placeholder": "Plan, search, build anything", "value": input_value},
        {"id": "msg", "role": "AXStaticText", "value": "Refactor API layer"},   # same text in a message
    ]
    if two_inputs:
        kids.append({"id": "chatinput2", "role": "AXTextArea", "kind": "chat",
                     "classes": ["aislash-editor-input"], "value": ""})
    return {"trusted": trusted, "active_conv": active, "focused": focused,
            "windows": [{"id": "w1", "role": "AXWindow", "title": "my-app \u2014 Cursor", "children": kids}]}


def make_state_db(user_dir):
    g = os.path.join(user_dir, "globalStorage")
    w = os.path.join(user_dir, "workspaceStorage", "abc123")
    os.makedirs(g)
    os.makedirs(w)
    for path, key, val in (
        (os.path.join(g, "state.vscdb"), "composer.composerHeaders", {"allComposers": [
            {"composerId": C1, "name": "Fix login bug", "lastUpdatedAt": 1760000000000,
             "workspaceIdentifier": {"uri": {"fsPath": "/tmp/my-app"}}},
            {"composerId": C2, "name": "Refactor API layer", "lastUpdatedAt": 1760000100000,
             "workspaceIdentifier": {"uri": {"fsPath": "/tmp/my-app"}}},
            {"composerId": "c3333333-old", "name": "Old closed chat", "lastUpdatedAt": 1700000000000},
            {"composerId": "c4444444-arch", "name": "Archived", "isArchived": True}]}),
        (os.path.join(w, "state.vscdb"), "composer.composerData",
         {"selectedComposerIds": [C1, C2], "lastFocusedComposerIds": [C1]}),
    ):
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
        con.execute("INSERT INTO ItemTable VALUES (?, ?)", (key, json.dumps(val)))
        con.commit()
        con.close()
    with open(os.path.join(w, "workspace.json"), "w") as fh:
        json.dump({"folder": "file:///tmp/my-app"}, fh)


class SendBase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="gc-send-")
        self.bin = os.path.join(self.home, "shims")
        os.mkdir(self.bin)
        for n in TOOLS:
            p = os.path.join(self.bin, n)
            shutil.copy(SHIM, p)
            os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        self.log = os.path.join(self.home, "calls.log")
        self.state = os.path.join(self.home, "ax.json")
        self.user_dir = os.path.join(self.home, "CursorUser")
        make_state_db(self.user_dir)
        self.set_state(ui_state())
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("GROK_CURSOR_", "CURSOR_", "SHIM_"))}
        self.env.update(HOME=self.home, PATH=self.bin + os.pathsep + os.path.dirname(PY) + ":/usr/bin:/bin",
                        SHIM_LOG=self.log, GROK_CURSOR_PLATFORM="darwin", GROK_CURSOR_SEND_DELAY="0",
                        GROK_CURSOR_AX_FAKE=self.state, CURSOR_USER_DIR=self.user_dir, USER="tester")
        self.repo = os.path.join(self.home, "my-app")
        os.mkdir(self.repo)
        with open(os.path.join(self.home, ".shim-clipboard"), "w") as fh:
            fh.write("user's clipboard \u2603")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def set_state(self, st):
        with open(self.state, "w") as fh:
            json.dump(st, fh)

    def st(self):
        with open(self.state) as fh:
            return json.load(fh)

    def gc(self, *args, env=None, stdin=None):
        e = dict(self.env)
        e.update(env or {})
        return subprocess.run([PY, CLI] + list(args), capture_output=True, text=True,
                              env=e, input=stdin, timeout=60)

    def send(self, *args, env=None, stdin=None):
        return self.gc("send", "--verify", "3", *args, env=env, stdin=stdin)

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as fh:
            return [json.loads(l) for l in fh]

    def sends_log(self):
        with open(os.path.join(self.home, ".grok-cursor", "sends.log")) as fh:
            return [json.loads(l) for l in fh]

    def clipboard(self):
        with open(os.path.join(self.home, ".shim-clipboard")) as fh:
            return fh.read()

    def assertNoSubmit(self):
        st = self.st()
        self.assertFalse(st.get("submitted"), "something was submitted")
        self.assertFalse(st.get("stray_returns"), "Return pressed outside the chat input")
        self.assertFalse(any(c[0] == "osascript" and "key code 36" in c[-1] for c in self.calls()))


class SendFocused(SendBase):
    """Goal A: send into the currently open chat."""

    def test_duplicate_frontmost_window_is_one_match(self):
        """The focused window can show up twice (main window plus the window list). Same
        title and bounds, no window id: that is one window, so --repo is not ambiguous."""
        st = ui_state()
        front = st["windows"][0]
        front["bounds"] = [0, 0, 1440, 900]
        dup = json.loads(json.dumps(front))
        dup["id"] = "w1-again"
        other = {"id": "w2", "role": "AXWindow", "title": "notes \u2014 Cursor",
                 "bounds": [40, 40, 800, 600], "children": []}
        st["windows"] = [front, dup, other]
        self.set_state(st)
        p = self.send("--repo", self.repo, "once")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.st()["submitted"], [{"conv": C1, "text": "once"}])
        self.assertNotIn("several Cursor windows", p.stderr)

    def test_success_end_to_end(self):
        p = self.send("fix the failing test & push? \u00e9\u00e8 \u2014 ok")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("delivered", p.stdout)
        self.assertIn("via transcript", p.stdout)
        st = self.st()
        self.assertEqual(st["submitted"], [{"conv": C1, "text": "fix the failing test & push? \u00e9\u00e8 \u2014 ok"}])
        self.assertFalse(st.get("stray_returns"))
        self.assertEqual(st["focused"], "chatinput")            # moved focus off the editor first
        self.assertEqual(self.clipboard(), "user's clipboard \u2603")   # clipboard restored
        self.assertEqual(self.sends_log()[-1]["result"], "delivered")
        self.assertEqual(self.sends_log()[-1]["conversation_id"], C1)
        self.assertIn(["caffeinate", "-u", "-t", "120"], self.calls())

    def test_multiline_prompt_is_one_message(self):
        prompt = "line one\nline two\n\n- bullet"
        p = self.send("-", stdin=prompt)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.st()["submitted"][0]["text"], prompt)

    def test_deeplink_method(self):
        p = self.send("--method", "deeplink", "hello deep link")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(any(c[0] == "open" and c[1].startswith("cursor://anysphere.cursor-deeplink/prompt?text=hello%20deep%20link")
                            for c in self.calls()))
        self.assertEqual(self.clipboard(), "user's clipboard \u2603")
        self.assertFalse(any(c[0] == "pbcopy" for c in self.calls()))

    def test_auto_uses_deeplink_when_clipboard_has_image(self):
        p = self.send("hi", env={"SHIM_CLIP_INFO": "\u00abclass PNGf\u00bb, 2048, \u00abclass 8BPS\u00bb, 100"})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(any(c[0] == "pbcopy" for c in self.calls()))
        self.assertTrue(any(c[0] == "open" and c[1].startswith("cursor://") for c in self.calls()))

    def test_now_uses_cmd_return(self):
        p = self.send("--now", "hurry")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(any(c[0] == "osascript" and "key code 36 using {command down}" in c[-1] for c in self.calls()))

    def test_dry_run_touches_nothing(self):
        p = self.gc("send", "--dry-run", "--repo", self.repo, "hello")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("DRY RUN OK", p.stdout)
        self.assertIn("chat input found and empty", p.stdout)
        names = [c[0] for c in self.calls()]
        for bad in ("open", "pbcopy", "caffeinate"):
            self.assertNotIn(bad, names)
        self.assertEqual(self.st()["focused"], "editor")
        self.assertNoSubmit()
        self.assertEqual(self.sends_log()[-1]["result"], "dry-run")

    def test_verify_zero_skips_wait(self):
        p = self.gc("send", "--verify", "0", "x")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("SENT\n", p.stdout)

    def test_unverified_when_hook_never_fires(self):
        p = self.send("hello", env={"SHIM_NO_HOOK": "1"})
        self.assertEqual(p.returncode, 23, p.stderr)
        self.assertIn("unconfirmed", p.stderr)
        self.assertNotIn("delivered", p.stdout)
        self.assertEqual(self.sends_log()[-1]["result"], "unverified")
        self.assertTrue(self.sends_log()[-1]["pressed"])

    def test_transcript_fallback_verification(self):
        cid = C1
        tdir = os.path.join(self.home, ".cursor", "projects", "p", "agent-transcripts", cid)
        os.makedirs(tdir)
        line = json.dumps({"role": "user", "message": {"content": [
            {"type": "text", "text": "transcript check ok"}]}})
        script = ("import time,sys; time.sleep(0.6); open(sys.argv[1],'w').write(sys.argv[2] + '\\n')")
        proc = subprocess.Popen([PY, "-c", script, os.path.join(tdir, cid + ".jsonl"), line])
        p = self.send("transcript check ok", env={"SHIM_NO_HOOK": "1"})
        proc.wait()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("delivered via transcript", p.stdout)

    def test_transcript_error_is_failed_not_delivered(self):
        cid = C1
        tdir = os.path.join(self.home, ".cursor", "projects", "p", "agent-transcripts", cid)
        os.makedirs(tdir)
        err = "AI Model Not Found Model name is not valid: \"my-model\""
        line = json.dumps({"type": "turn_ended", "status": "error", "error": err})
        script = ("import time,sys; time.sleep(0.4); open(sys.argv[1],'w').write(sys.argv[2] + '\\n')")
        proc = subprocess.Popen([PY, "-c", script, os.path.join(tdir, cid + ".jsonl"), line])
        p = self.send("gc test 1", env={"SHIM_NO_HOOK": "1"})
        proc.wait()
        self.assertEqual(p.returncode, 31, p.stderr)
        self.assertIn("failed: AI Model Not Found", p.stderr)
        self.assertIn("my-model", p.stderr)
        self.assertNotIn("delivered", p.stdout)
        self.assertEqual(self.sends_log()[-1]["result"], "failed")

    def test_new_chat_ignores_old_errors_in_the_focused_chat(self):
        """--new: verify falls back to the focused chat. Its old error must not fail the send."""
        tdir = os.path.join(self.home, ".cursor", "projects", "p", "agent-transcripts", C1)
        os.makedirs(tdir)
        with open(os.path.join(tdir, C1 + ".jsonl"), "w") as fh:
            fh.write(json.dumps({"type": "turn_ended", "status": "error",
                                 "error": "Bad Request This model does not support custom API keys."}) + "\n")
        p = self.gc("send", "--verify", "1", "--new", "fresh start", env={"SHIM_NO_HOOK": "1"})
        self.assertNotEqual(p.returncode, 31, p.stderr)
        self.assertNotIn("custom API keys", p.stderr)

    def test_delivery_unconfirmed_on_timeout(self):
        p = self.gc("send", "--verify", "1", "nobody home", env={"SHIM_NO_HOOK": "1"})
        self.assertEqual(p.returncode, 23, p.stderr)
        self.assertIn("unconfirmed", p.stderr)
        self.assertNotIn("delivered", p.stdout)
        self.assertTrue(self.sends_log()[-1]["pressed"])

    def abort(self, code, *args, env=None):
        p = self.send(*args, env=env)
        self.assertEqual(p.returncode, code, p.stdout + p.stderr)
        self.assertIn("ABORT", p.stderr)
        self.assertNoSubmit()
        self.assertEqual(self.sends_log()[-1]["code"], code)
        self.assertEqual(self.clipboard(), "user's clipboard \u2603")
        return p

    def test_abort_not_macos(self):
        self.abort(10, "hi", env={"GROK_CURSOR_PLATFORM": "linux"})

    def test_abort_empty(self):
        self.abort(11, "  \n ")

    def test_abort_secret(self):
        tok = "gh" + "p_" + "a" * 36
        p = self.abort(12, "use key " + tok)
        self.assertIn("github token", p.stderr)
        self.assertNotIn(tok, json.dumps(self.sends_log()))
        self.assertEqual(self.calls(), [])

    def test_allow_secrets(self):
        p = self.send("--allow-secrets", "AK" + "IA" + "FAKEFAKEFAKE0000")
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_abort_too_long(self):
        self.assertIn("cap is 10", self.abort(13, "--max-chars", "10", "x" * 11).stderr)

    def test_abort_deeplink_url_too_long(self):
        self.abort(13, "--method", "deeplink", "\u00e9" * 2000)    # 2000 chars -> 12000-char URL

    def test_abort_locked(self):
        self.abort(15, "hi", env={"SHIM_LOCKED": "1"})

    def test_abort_lock_unknown(self):
        self.abort(15, "hi", env={"SHIM_IOREG_FAIL": "1"})

    def test_abort_not_on_console(self):
        self.abort(30, "hi", env={"SHIM_OFF_CONSOLE": "1"})

    def test_abort_not_running(self):
        self.abort(16, "hi", env={"SHIM_PGREP_RC": "1"})

    def test_abort_bad_repo(self):
        self.abort(17, "--repo", os.path.join(self.home, "nope"), "hi")

    def test_abort_repo_open_fails(self):
        self.abort(17, "--repo", self.repo, "hi", env={"SHIM_OPEN_RC": "1"})

    def test_abort_deeplink_fails(self):
        self.abort(18, "--method", "deeplink", "hi", env={"SHIM_OPEN_LINK_RC": "1"})

    def test_abort_not_frontmost(self):
        self.assertIn("Safari", self.abort(19, "hi", env={"SHIM_FRONT": "Safari"}).stderr)

    def test_abort_focus_stolen_after_paste(self):
        p = self.send("hi", env={"SHIM_FRONT_AFTER_PASTE": "Finder"})
        self.assertEqual(p.returncode, 19, p.stderr)
        self.assertNoSubmit()
        self.assertEqual(self.clipboard(), "user's clipboard \u2603")

    def test_abort_automation_denied(self):
        self.abort(19, "hi", env={"SHIM_OSA_DENIED": "1"})

    def test_abort_keystroke_fails(self):
        p = self.send("hi", env={"SHIM_KEY_FAIL": "1"})
        self.assertEqual(p.returncode, 20)
        self.assertIn("Accessibility", p.stderr)
        self.assertFalse(self.st().get("submitted"))

    def test_abort_not_trusted(self):
        self.set_state(ui_state(trusted=False))
        self.assertIn("System Settings", self.abort(24, "hi").stderr)

    def test_abort_input_has_draft(self):
        self.set_state(ui_state(input_value="my half-written draft"))
        self.abort(27, "hi")
        self.assertEqual(self.st()["windows"][0]["children"][2]["value"], "my half-written draft")

    def test_abort_no_input(self):
        st = ui_state()
        st["windows"][0]["children"] = [c for c in st["windows"][0]["children"] if c["id"] != "chatinput"]
        self.set_state(st)
        self.abort(27, "hi")

    def test_abort_two_inputs_none_focused(self):
        self.set_state(ui_state(two_inputs=True))
        self.abort(27, "hi")

    def test_abort_paste_did_not_land(self):
        p = self.send("hi", env={"SHIM_PASTE_NOOP": "1"})
        self.assertEqual(p.returncode, 27, p.stderr)
        self.assertNoSubmit()
        self.assertEqual(self.clipboard(), "user's clipboard \u2603")

    def test_abort_focus_refused(self):
        st = ui_state()
        st["windows"][0]["children"][2]["focus"] = "fail"
        self.set_state(st)
        self.abort(27, "hi")

    def test_unreadable_clipboard_falls_back_to_deeplink(self):
        p = self.send("hi", env={"SHIM_CLIP_INFO_FAIL": "1"})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(any(c[0] == "pbcopy" for c in self.calls()))

    def test_ax_skips_editor_subtree(self):
        st = ui_state()
        st["windows"][0]["children"][1]["children"] = [
            {"id": "fake-in-editor", "role": "AXTextArea", "classes": ["aislash-editor-input"], "value": ""}]
        self.set_state(st)
        p = self.send("hi")      # the input inside the code editor must not count as a 2nd chat input
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_abort_clipboard_image_with_paste(self):
        self.abort(29, "--method", "paste", "hi",
                   env={"SHIM_CLIP_INFO": "\u00abclass PNGf\u00bb, 2048"})


class SendTargets(SendBase):
    """Goal B (switch to an open tab) and goal C (new chat)."""

    def test_targets_lists_open_tabs(self):
        p = self.gc("targets")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("Fix login bug", p.stdout)
        self.assertIn("Refactor API layer", p.stdout)
        self.assertNotIn("Old closed chat", p.stdout)
        self.assertNotIn("Archived", p.stdout)
        p = self.gc("targets", "--all", "--json", "--ax")
        data = json.loads(p.stdout)
        ids = {c["id"]: c for c in data["chats"]}
        self.assertTrue(ids[C1]["focused"] and ids[C1]["open"])
        self.assertFalse(ids["c3333333-old"]["open"])
        self.assertEqual({t["text"] for t in data["on_screen"]}, {"Fix login bug", "Refactor API layer"})

    def test_send_to_other_tab_by_title(self):
        p = self.send("--to", "refactor api", "please continue")
        self.assertEqual(p.returncode, 0, p.stderr)
        st = self.st()
        self.assertEqual(st["submitted"], [{"conv": C2, "text": "please continue"}])
        self.assertIn("switched to tab", p.stdout)
        self.assertIn("verified", p.stdout)

    def test_send_to_by_id_prefix(self):
        p = self.send("--to", C2[:8], "by id")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.st()["submitted"][0]["conv"], C2)

    def test_send_to_already_active(self):
        p = self.send("--to", "Fix login bug", "same tab")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("already active", p.stdout)

    def test_send_to_screen_title_not_in_db(self):
        st = ui_state()
        st["windows"][0]["children"][0]["children"].append(
            {"id": "tab3", "role": "AXRadioButton", "title": "Brand new chat", "conv": "c5", "selected": False})
        self.set_state(st)
        p = self.gc("send", "--verify", "0", "--to", "Brand new chat", "x", env={"SHIM_HOOK_CONV": "c5"})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.st()["submitted"][0]["conv"], "c5")

    def test_truncated_tab_title_matches(self):
        st = ui_state()
        st["windows"][0]["children"][0]["children"][1]["title"] = "Refactor API\u2026"
        self.set_state(st)
        p = self.send("--to", "Refactor API layer", "x")
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_real_style_tab_label_matches(self):
        """Cursor 3 tab buttons expose an aria-label with a suffix. The clean title is the child."""
        st = ui_state()
        tab2 = st["windows"][0]["children"][0]["children"][1]
        tab2["title"] = "Refactor API layer, pinned, Chat Editors: Editor Group 1"
        tab2["children"] = [
            {"id": "tab2id", "role": "AXGroup", "classes": ["monaco-icon-label"],
             "title": "c2222222-0000-4000-8000-000000000002"},
            {"id": "tab2label", "role": "AXGroup", "classes": ["monaco-icon-label", "tab-label"],
             "title": "Refactor API layer"},
        ]
        self.set_state(st)
        p = self.send("--to", "Refactor API layer", "suffix ok")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertEqual(self.st()["submitted"], [{"conv": C2, "text": "suffix ok"}])
        self.assertIn("switched to tab 'Refactor API layer'", p.stdout)
        self.assertNotIn("stepped", p.stdout)

    def test_abort_target_unknown(self):
        p = self.send("--to", "nonexistent chat", "x")
        self.assertEqual(p.returncode, 25, p.stderr)
        self.assertNoSubmit()

    def test_abort_target_ambiguous(self):
        p = self.send("--to", "i", "x")     # "i" is in both open tab titles
        self.assertEqual(p.returncode, 25, p.stderr)
        self.assertNoSubmit()

    def test_abort_tab_not_on_screen(self):
        p = self.send("--to", "Old closed chat", "x")
        self.assertEqual(p.returncode, 25, p.stderr)

    def test_abort_switch_not_verified(self):
        st = ui_state()
        st["windows"][0]["children"][0]["children"][1]["press"] = "noop"   # click does nothing
        self.set_state(st)
        p = self.send("--to", "Refactor API layer", "x", env={"SHIM_STEP_NOOP": "1"})   # and stepping too
        self.assertEqual(p.returncode, 26, p.stderr)
        self.assertNoSubmit()

    def test_abort_delivered_to_wrong_chat(self):
        p = self.send("--to", "Refactor API layer", "x", env={"SHIM_HOOK_CONV": "someone-else"})
        self.assertEqual(p.returncode, 23, p.stderr)
        self.assertIn("unconfirmed", p.stderr)
        self.assertNotIn("delivered", p.stdout)

    def test_dry_run_to_does_not_click(self):
        p = self.gc("send", "--dry-run", "--to", "Refactor API layer", "x")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("would click tab", p.stdout)
        self.assertEqual(self.st()["active_conv"], C1)

    def three_tabs(self, press="noop", **sel):
        st = ui_state()
        bar = st["windows"][0]["children"][0]["children"]
        bar.append({"id": "tab3", "role": "AXRadioButton", "title": "Write docs", "conv": "c5", "selected": False})
        for t in bar:
            t["press"] = press
        self.set_state(st)

    def test_stepping_fallback_when_click_does_nothing(self):
        self.three_tabs()
        p = self.gc("send", "--verify", "0", "--to", "Write docs", "stepped", env={"SHIM_HOOK_CONV": "c5"})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("stepped 2 tab(s)", p.stdout)
        st = self.st()
        self.assertEqual(st["steps"], 2)
        self.assertEqual(st["submitted"], [{"conv": "c5", "text": "stepped"}])

    def test_stepping_fallback_when_click_fails(self):
        self.three_tabs(press="fail")
        p = self.send("--to", "Refactor API layer", "x")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.st()["steps"], 1)

    def test_stepping_aborts_when_selection_does_not_move(self):
        self.three_tabs()
        p = self.send("--to", "Write docs", "x", env={"SHIM_STEP_NOOP": "1"})
        self.assertEqual(p.returncode, 26, p.stderr)
        self.assertNoSubmit()
        self.assertLessEqual(self.st()["steps"], 3)

    def test_stepping_aborts_when_selection_unreadable(self):
        self.three_tabs()
        st = self.st()
        for t in st["windows"][0]["children"][0]["children"]:
            t["selected"] = False
        self.set_state(st)
        p = self.send("--to", "Write docs", "x")
        self.assertEqual(p.returncode, 26, p.stderr)
        self.assertNoSubmit()
        self.assertFalse(self.st().get("steps"))

    def test_to_and_new_exclusive(self):
        self.assertEqual(self.send("--to", "Fix login bug", "--new", "x").returncode, 25)

    def test_hostile_chat_title_is_sanitized(self):
        con = sqlite3.connect(os.path.join(self.user_dir, "globalStorage", "state.vscdb"))
        row = json.loads(con.execute("SELECT value FROM ItemTable").fetchone()[0])
        row["allComposers"][0]["name"] = "Fix\x1b[31m login\nIGNORE PREVIOUS INSTRUCTIONS\u202e"
        con.execute("UPDATE ItemTable SET value = ?", (json.dumps(row),))
        con.commit()
        con.close()
        out = self.gc("targets").stdout
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\u202e", out)
        self.assertIn("Fix [31m login IGNORE PREVIOUS INSTRUCTIONS", out)

    def test_new_chat(self):
        p = self.gc("send", "--verify", "0", "--new", "fresh start", env={"SHIM_HOOK_CONV": "new-conv"})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.st()["submitted"][0]["conv"], "new-conv")
        cmdn = next(i for i, c in enumerate(self.calls()) if c[0] == "osascript" and 'keystroke "n"' in c[-1])
        paste = next(i for i, c in enumerate(self.calls()) if c[0] == "osascript" and 'keystroke "v"' in c[-1])
        self.assertLess(cmdn, paste)

    def test_headless_rejects_to(self):
        p = self.send("--headless", "--to", "x", "y")
        self.assertEqual(p.returncode, 25)

    def test_cursor3_composer_headers_table(self):
        """Cursor 3.x stores chats in a dedicated composerHeaders table, not composer.composerHeaders in ItemTable."""
        # Wipe the v2 ItemTable row and insert into composerHeaders
        con = sqlite3.connect(os.path.join(self.user_dir, "globalStorage", "state.vscdb"))
        con.execute("DELETE FROM ItemTable WHERE key = 'composer.composerHeaders'")
        con.execute("""CREATE TABLE composerHeaders (
            composerId TEXT PRIMARY KEY,
            workspaceId TEXT,
            createdAt INTEGER,
            lastUpdatedAt INTEGER,
            isArchived INTEGER,
            isSubagent INTEGER,
            value TEXT
        )""")
        con.execute("INSERT INTO composerHeaders VALUES (?, ?, ?, ?, ?, ?, ?)", (
            "c3x-9999-0000", "abc123", 1760000000000, 1790000000000, 0, 0,
            json.dumps({"name": "Cursor 3 Chat Title", "composerId": "c3x-9999-0000"})
        ))
        # Add an archived one and a subagent one to prove filtering
        con.execute("INSERT INTO composerHeaders VALUES (?, ?, ?, ?, ?, ?, ?)", (
            "c3x-archived", "abc123", 1760000000000, 1780000000000, 1, 0,
            json.dumps({"name": "Archived Chat", "composerId": "c3x-archived"})
        ))
        con.execute("INSERT INTO composerHeaders VALUES (?, ?, ?, ?, ?, ?, ?)", (
            "c3x-subagent", "abc123", 1760000000000, 1780000000000, 0, 1,
            json.dumps({"name": "Subagent Chat", "composerId": "c3x-subagent"})
        ))
        con.commit()
        con.close()

        p = self.gc("targets", "--all", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        ids = {c["id"]: c for c in data["chats"]}
        self.assertIn("c3x-9999-0000", ids)
        self.assertEqual(ids["c3x-9999-0000"]["title"], "Cursor 3 Chat Title")
        self.assertEqual(ids["c3x-9999-0000"]["workspace_name"], "my-app")
        self.assertNotIn("c3x-archived", ids)
        self.assertNotIn("c3x-subagent", ids)


class SendHeadless(SendBase):
    def test_success(self):
        p = self.send("--headless", "--repo", self.repo, "do it")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("agent did: do it", p.stdout)
        c = [x for x in self.calls() if x[0] == "agent"]
        self.assertEqual(c, [["agent", "-p", "do it", "--output-format", "text"]])
        self.assertFalse(any(x[0] in ("osascript", "open") for x in self.calls()))

    def test_works_off_macos(self):
        self.assertEqual(self.send("--headless", "x", env={"GROK_CURSOR_PLATFORM": "linux"}).returncode, 0)

    def test_missing_cli(self):
        os.remove(os.path.join(self.bin, "agent"))
        self.assertEqual(self.send("--headless", "x").returncode, 21)

    def test_agent_fails(self):
        p = self.send("--headless", "x", env={"SHIM_AGENT_FAIL": "1"})
        self.assertEqual(p.returncode, 22)
        self.assertIn("boom", p.stderr)

    def test_secret_scan_applies(self):
        self.assertEqual(self.send("--headless", "sk" + "_live_" + "z" * 24).returncode, 12)
        self.assertEqual(self.calls(), [])


# ---------------------------------------------------------------- fake CDP endpoint

class FakeCDP(threading.Thread):
    """Serves /json/list and one websocket page target. Runtime.evaluate is answered
    from the /*gc:{op}*/ marker that gc_send puts in every script."""

    def __init__(self, tab_click_works=True):
        threading.Thread.__init__(self, daemon=True)
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        self.tabs = [{"text": "Fix login bug", "selected": True}, {"text": "Refactor API layer", "selected": False}]
        self.input, self.focused, self.submitted = "", False, []
        self.status = "cancelled"
        self.flip_busy = True
        self.settle = "completed"
        self._status_polls = 0
        self.tab_click_works = tab_click_works
        self.page_titles = None
        self.ws_title = None
        self.composer_inputs = None
        self.visible_inputs = 2
        self.owned_ambiguous = False
        self.tab_point_ok = True
        self._pressable = None
        self.mouse = []
        self.card = None            # approval card: {"buttons": [...], "command": ..., "note": ...}
        self.card_clicks = []
        self.slash_commands = ["/explain", "/code-review"]
        self.pill = None            # command picked from the slash menu
        self.slash_keys = []

    def menu_focus(self):
        """Mirrors Cursor: an exact name is focused; an unknown one focuses "Create /x skill"."""
        if self.pill or not self.input.startswith("/"):
            return None
        return self.input if self.input in self.slash_commands else "Create"

    def run(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    def handle(self, conn):
        req = b""
        while b"\r\n\r\n" not in req:
            req += conn.recv(4096)
        line = req.split(b"\r\n")[0].decode()
        if line.startswith("GET /json/list"):
            titles = self.page_titles or ["my-app \u2014 Cursor"]
            ws_title = self.ws_title or titles[0]
            body = json.dumps([
                {"type": "page", "title": title,
                 "url": "vscode-file://vscode-app/workbench/workbench.html",
                 "webSocketDebuggerUrl": "ws://127.0.0.1:%d/devtools/page/%s" % (
                     self.port, "1" if title == ws_title else "other%d" % i)}
                for i, title in enumerate(titles)]).encode()
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
            conn.close()
            return
        key = [l.split(b":", 1)[1].strip() for l in req.split(b"\r\n") if l.lower().startswith(b"sec-websocket-key")][0]
        acc = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     b"Sec-WebSocket-Accept: " + acc + b"\r\n\r\n")
        buf = b""

        def read(n):
            nonlocal buf
            while len(buf) < n:
                d = conn.recv(65536)
                if not d:
                    raise EOFError
                buf += d
            out, buf = buf[:n], buf[n:]
            return out
        try:
            while True:
                b1, b2 = read(2)
                n = b2 & 0x7F
                if n == 126:
                    n = struct.unpack(">H", read(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", read(8))[0]
                mask = read(4)
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(read(n)))
                msg = json.loads(data)
                out = json.dumps({"id": msg["id"], "result": self.answer(msg)}).encode()
                hdr = bytes([0x81]) + (bytes([len(out)]) if len(out) < 126 else bytes([126]) + struct.pack(">H", len(out)))
                conn.sendall(hdr + out)
        except (EOFError, OSError):
            conn.close()

    def answer(self, msg):
        m, p = msg["method"], msg.get("params", {})
        if m == "Runtime.evaluate":
            marker = json.loads(p["expression"].split("/*gc:", 1)[1].split("*/", 1)[0])
            op = marker["op"]
            if op == "tabs":
                val = [dict(t, group=t.get("group", 0)) for t in self.tabs]
            elif op == "tabpoint":
                if marker.get("id"):
                    hits = [t for t in self.tabs if t.get("id") == marker["id"]]
                else:
                    hits = [t for t in self.tabs if t["text"] == marker["title"]]
                self._pressable = hits[0] if len(hits) == 1 else None
                val = {"n": len(hits), "x": 10, "y": 10, "ok": self.tab_point_ok}
            elif op == "input" and self.composer_inputs is not None:
                want = marker.get("id") or ""
                if want and want in self.composer_inputs:
                    self.focused = True
                    self.input = self.composer_inputs[want]
                    val = {"n": 1, "text": self.input, "focused": True}
                else:
                    val = {"n": self.visible_inputs, "owned": bool(self.owned_ambiguous)}
            elif op == "cards":
                val = {"found": True, "status": self.status, "cards": [self.card] if self.card else []}
            elif op == "press_card":
                # mirrors JS press_card: exact label, never "Always ..."
                hit = [b for b in (self.card or {}).get("buttons", [])
                       if b in marker["labels"] and not b.lower().startswith("always")]
                if not self.card:
                    val = {"found": True, "n": 0}
                elif len(hit) != 1:
                    val = {"found": True, "n": 1, "hits": len(hit)}
                else:
                    self.card_clicks.append(hit[0])
                    self.card, self.status = None, "generating"
                    val = {"found": True, "n": 1, "hits": 1, "clicked": hit[0]}
            elif op == "status":
                if self.submitted and self.flip_busy:
                    self._status_polls += 1
                    self.status = self.settle if self.settle and self._status_polls >= 2 else "generating"
                val = {"found": True, "empty": not (self.input or "").strip(), "status": self.status}
            elif op == "slash_menu":
                f = self.menu_focus()
                val = {"open": 1, "focused": f, "names": self.slash_commands,
                       "x": 5, "y": 5, "ok": True} if f else {"open": 0}
                self._menu_row = f
            elif op == "slash_pill":
                val = {"n": 1, "text": self.input, "pills": [self.pill] if self.pill else []}
            else:
                self.focused = True
                val = {"n": 1, "text": self.input, "focused": self.focused}
            return {"result": {"type": "object", "value": val}}
        if m == "Input.dispatchMouseEvent":
            self.mouse.append(p["type"])
            if p["type"] == "mousePressed" and self.menu_focus():
                f = self.menu_focus()
                if f.startswith("/"):
                    self.pill, self.input = f[1:], f + " "
                else:
                    self.slash_keys.append("CREATED-SKILL")
                return {}
            hit = self._pressable
            if p["type"] == "mousePressed" and hit is not None and self.tab_click_works:
                for t in self.tabs:
                    t["selected"] = t is hit
                if self.composer_inputs is not None and hit.get("id"):
                    self.composer_inputs.setdefault(hit["id"], "")
            return {}
        if m == "Input.insertText":
            self.input += p["text"]
            return {}
        if m == "Input.dispatchKeyEvent":
            if p["type"] == "keyDown" and p["key"] != "Enter":
                self.slash_keys.append(p["key"])
                if p["key"] == "/":
                    self.input += "/"
                elif p["key"] == "Backspace":
                    self.input, self.pill = "", None
                return {}
            if p["type"] == "keyDown" and p["key"] == "Enter" and self.menu_focus():
                # Real Cursor: Enter on the menu may just close it with no pick.
                self.input = self.input.rstrip()
                return {}
            if p["type"] == "keyDown" and p["key"] == "Enter":
                active = [t["text"] for t in self.tabs if t["selected"]][0]
                self.submitted.append((active, self.input))
                self.input = ""
                if self.flip_busy:
                    self.status = "generating"
                    self._status_polls = 0
            return {}
        return {}


class SendCDP(SendBase):
    def start(self, **kw):
        f = FakeCDP(**kw)
        f.start()
        self.addCleanup(f.srv.close)
        return f

    def test_cdp_send_to_tab(self):
        f = self.start()
        p = self.gc("send", "--verify", "0", "--cdp", str(f.port), "--to", "Refactor API layer", "via cdp \u2713")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(f.submitted, [("Refactor API layer", "via cdp \u2713")])
        self.assertFalse(any(c[0] == "osascript" and "key code" in c[-1] for c in self.calls()))

    def test_to_chat_id_picks_the_window_that_owns_it(self):
        """Four workbench windows. --to the chat id, no --repo, must open the owning workspace."""
        f = self.start()
        owned = "Acme IO | Web \u2014 my-app (Workspace)"
        f.page_titles = [
            "gc-scratch-1",
            "gc-scratch-2",
            "Notes App V2 \u2014 notes-app",
            owned,
        ]
        f.ws_title = owned
        p = self.gc("send", "--dry-run", "--cdp", str(f.port), "--to", C2, "dry run only")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertIn(owned, p.stdout)
        self.assertIn("DRY RUN OK", p.stdout)
        self.assertEqual(f.submitted, [])

    def test_parent_repo_does_not_hide_the_chats_window(self):
        f = self.start()
        owned = "Acme IO | Web \u2014 my-app (Workspace)"
        f.page_titles = ["gc-scratch-1", "Notes App V2 \u2014 notes-app", owned]
        f.ws_title = owned
        repo = os.path.join(self.home, "Cursor_AI_Brain")
        os.mkdir(repo)
        p = self.gc("send", "--dry-run", "--cdp", str(f.port), "--repo", repo, "--to", C2, "dry run only")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertIn(owned, p.stdout)
        self.assertEqual(f.submitted, [])

    def test_pick_cdp_page_named_workspace_among_four_windows(self):
        pages = [
            {"type": "page", "title": "gc-scratch-1", "url": "vscode-file://vscode-app/workbench/workbench.html"},
            {"type": "page", "title": "gc-scratch-2", "url": "vscode-file://vscode-app/workbench/workbench.html"},
            {"type": "page", "title": "Notes App V2 \u2014 notes-app",
             "url": "vscode-file://vscode-app/workbench/workbench.html"},
            {"type": "page", "title": "Acme IO | Web, SEO \u2014 Acme-IO-Site (Workspace)",
             "url": "vscode-file://vscode-app/workbench/workbench.html"},
        ]
        sys.path.insert(0, os.path.join(ROOT, "lib"))
        import gc_send
        page = gc_send.pick_cdp_page(pages, workspace_name="Acme-IO-Site")
        self.assertIn("Acme-IO-Site", page["title"])
        page = gc_send.pick_cdp_page(
            pages, window_hint="/Users/me/Documents/notes", workspace_name="Acme-IO-Site")
        self.assertIn("Acme-IO-Site", page["title"])
        with self.assertRaises(gc_send.SendAbort) as caught:
            gc_send.pick_cdp_page(pages)
        self.assertIn("4 Cursor workbench windows", str(caught.exception))

    def test_cdp_is_primary_when_its_port_is_open(self):
        f = self.start()
        p = self.gc("send", "--verify", "2", "--to", C2, "via auto cdp",
                    env={"GROK_CURSOR_AX_FAKE": "", "GROK_CURSOR_CDP_PORT": str(f.port)})
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertIn("skipping frontmost and Accessibility", p.stdout)
        self.assertIn("screen unlocked: ok", p.stdout)
        self.assertNotIn("frontmost: ok", p.stdout)
        self.assertIn("delivered via cdp", p.stdout)
        self.assertEqual(f.submitted, [("Refactor API layer", "via auto cdp")])
        self.assertFalse(any(c[0] == "osascript" and "key code" in c[-1] for c in self.calls()))

    def test_cdp_delivered_when_input_clears_and_busy_flips(self):
        f = self.start()
        p = self.gc("send", "--verify", "2", "--cdp", str(f.port), "--to", C2, "gc test cdp")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("delivered via cdp", p.stdout)
        self.assertEqual(f.status, "completed")
        self.assertEqual(f.submitted, [("Refactor API layer", "gc test cdp")])

    def test_cdp_cancelled_is_not_delivered(self):
        f = self.start()
        f.settle = "cancelled"
        p = self.gc("send", "--verify", "1", "--cdp", str(f.port), "--to", C2, "gc test cdp")
        self.assertEqual(p.returncode, 23, p.stderr)
        self.assertIn("unconfirmed", p.stderr)
        self.assertNotIn("delivered", p.stdout)
        self.assertEqual(f.status, "cancelled")
        self.assertEqual(f.submitted, [("Refactor API layer", "gc test cdp")])

    def test_cdp_unconfirmed_when_busy_does_not_flip(self):
        f = self.start()
        f.flip_busy = False
        p = self.gc("send", "--verify", "1", "--cdp", str(f.port), "--to", C2, "gc test cdp")
        self.assertEqual(p.returncode, 23, p.stderr)
        self.assertIn("unconfirmed", p.stderr)
        self.assertNotIn("delivered", p.stdout)
        self.assertEqual(f.submitted, [("Refactor API layer", "gc test cdp")])

    def test_cdp_prefers_chat_id_over_similar_title(self):
        """A tab whose title equals the target must lose to the tab whose chat id equals it."""
        f = self.start()
        f.tabs = [
            {"id": "other-chat", "text": "Refactor API layer", "selected": True},
            {"id": C2, "text": "Refactor API layer, pinned, Chat Editors: Editor Group 1", "selected": False},
        ]
        p = self.gc("send", "--verify", "0", "--cdp", str(f.port), "--to", C2, "id wins")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertEqual(f.submitted, [
            ("Refactor API layer, pinned, Chat Editors: Editor Group 1", "id wins")])

    def test_cdp_switch_unverified(self):
        f = self.start(tab_click_works=False)
        p = self.gc("send", "--verify", "0", "--cdp", str(f.port), "--to", "Refactor API layer", "x")
        self.assertEqual(p.returncode, 26, p.stderr)
        self.assertEqual(f.submitted, [])

    def test_cdp_uses_the_target_chats_input_when_another_is_visible(self):
        f = self.start()
        f.tabs = [
            {"id": C2, "text": "Refactor API layer", "selected": True},
            {"id": "other-chat", "text": "Fix login bug", "selected": False},
        ]
        f.composer_inputs = {C2: "", "other-chat": "draft in the other chat"}
        p = self.gc("send", "--dry-run", "--cdp", str(f.port), "--to", C2, "dry run only")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertIn("chat input for %s" % C2[:8], p.stdout)
        self.assertNotIn("GROK_CURSOR_CDP_INPUT_SEL", p.stderr + p.stdout)
        self.assertEqual(f.submitted, [])

    def test_cdp_dry_run_names_the_input_before_the_tab_is_mounted(self):
        f = self.start()
        f.tabs = [
            {"id": "other-chat", "text": "Fix login bug", "selected": True},
            {"id": C2, "text": "Refactor API layer", "selected": False},
        ]
        f.composer_inputs = {"other-chat": ""}
        p = self.gc("send", "--dry-run", "--cdp", str(f.port), "--to", C2, "dry run only")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertIn("would use the composer input for chat %s" % C2[:8], p.stdout)
        self.assertEqual(f.submitted, [])

    def test_cdp_aborts_when_the_selected_chat_has_no_owned_input(self):
        f = self.start()
        f.tabs = [{"id": C2, "text": "Refactor API layer", "selected": True}]
        f.composer_inputs = {}
        f.visible_inputs = 2
        p = self.gc("send", "--dry-run", "--cdp", str(f.port), "--to", C2, "x")
        self.assertEqual(p.returncode, 27, p.stderr)
        self.assertIn("none is the target chat's", p.stderr)
        self.assertNotIn("GROK_CURSOR_CDP_INPUT_SEL", p.stderr)
        self.assertEqual(f.submitted, [])

    def test_cdp_input_not_empty(self):
        f = self.start()
        f.input = "draft"
        p = self.gc("send", "--verify", "0", "--cdp", str(f.port), "x")
        self.assertEqual(p.returncode, 27, p.stderr)
        self.assertEqual(f.submitted, [])

    def test_cdp_unreachable(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        p = self.gc("send", "--cdp", str(port), "x")
        self.assertEqual(p.returncode, 28, p.stderr)

    def test_cdp_refuses_when_locked(self):
        f = self.start()
        p = self.gc("send", "--verify", "0", "--cdp", str(f.port), "x", env={"SHIM_LOCKED": "1"})
        self.assertEqual(p.returncode, 15, p.stderr)
        self.assertIn("screen is locked", p.stderr)
        self.assertEqual(f.submitted, [])


    def test_cdp_background_tab_send_and_back(self):
        """Target open but not in view: press it, verify, send, then switch back."""
        f = self.start()
        f.tabs = [{"id": "other-chat", "text": "Fix login bug", "selected": True},
                  {"id": C2, "text": "Refactor API layer", "selected": False}]
        p = self.gc("send", "--verify", "0", "--back", "--cdp", str(f.port), "--to", C2, "bg send")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertEqual(f.submitted, [("Refactor API layer", "bg send")])
        self.assertIn("mousePressed", f.mouse)
        self.assertEqual([t["text"] for t in f.tabs if t["selected"]], ["Fix login bug"])

    def test_cdp_delivered_when_chat_stays_busy(self):
        """Cursor starts working at once and is still busy at the deadline: delivered, not 23."""
        f = self.start()
        f.settle = None
        p = self.gc("send", "--verify", "4", "--cdp", str(f.port), "--to", C2, "long task")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertIn("delivered via cdp", p.stdout)
        self.assertEqual(f.status, "generating")

    def test_slash_command_picked_from_menu_and_verified_by_status(self):
        """No user turn is written; busy -> idle after Enter proves delivery."""
        f = self.start()
        p = self.gc("send", "--verify", "2", "--cdp", str(f.port), "--to", C2, "/explain the watcher")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertIn("slash menu: /explain focused", p.stdout)
        self.assertIn("delivered via cdp", p.stdout)
        self.assertEqual(f.submitted, [("Refactor API layer", "/explain the watcher")])
        self.assertEqual(f.pill, "explain")
        self.assertIn("mousePressed", f.mouse)

    def test_slash_command_alone(self):
        f = self.start()
        p = self.gc("send", "--verify", "2", "--cdp", str(f.port), "--to", C2, "/code-review")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertEqual(f.submitted, [("Refactor API layer", "/code-review ")])

    def test_unknown_slash_command_is_never_entered(self):
        """Cursor focuses "Create /summary skill" for an unknown name: refuse, clear, exit 34."""
        f = self.start()
        p = self.gc("send", "--verify", "2", "--cdp", str(f.port), "--to", C2, "/summary")
        self.assertEqual(p.returncode, 34, p.stderr + p.stdout)
        self.assertIn("no /summary command", p.stderr)
        self.assertEqual(f.submitted, [])
        self.assertNotIn("CREATED-SKILL", f.slash_keys)
        self.assertEqual(f.input, "")
        self.assertNotIn("Return WAS pressed", p.stderr)

    def test_slash_dry_run(self):
        f = self.start()
        f.tabs[1]["selected"], f.tabs[0]["selected"] = True, False
        p = self.gc("send", "--dry-run", "--cdp", str(f.port), "--to", "Refactor API layer", "/explain x")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertIn("would pick /explain from the slash menu", p.stdout)
        self.assertEqual(f.slash_keys, [])

    def test_slash_needs_cdp(self):
        p = self.gc("send", "--verify", "0", "/explain x")
        self.assertEqual(p.returncode, 34, p.stderr + p.stdout)
        self.assertIn("need CDP", p.stderr)

    def test_slash_re(self):
        sys.path.insert(0, os.path.join(ROOT, "lib"))
        import gc_send
        self.assertEqual(gc_send.SLASH_RE.match("/explain a\nb").groups(), ("explain", "a\nb"))
        self.assertEqual(gc_send.SLASH_RE.match("/vercel/quick_status").group(1), "vercel/quick_status")
        for text in ("/ x", "//x", "path /x"):
            self.assertIsNone(gc_send.SLASH_RE.match(text), text)

    def test_new_message_submitted_is_not_a_failure(self):
        sys.path.insert(0, os.path.join(ROOT, "lib"))
        import gc_send
        self.assertIsNone(gc_send._turn_error(
            {"type": "turn_ended", "status": "error", "error": "new_message_submitted"}))
        self.assertEqual(gc_send._turn_error(
            {"type": "turn_ended", "status": "error", "error": "invalid model"}), "invalid model")

class Approval(SendBase):
    start = SendCDP.start
    CARD = {"command": "Delete /tmp/x.txt", "note": "Deletes a file outside the repo", "buttons": ["Skip", "Run"]}

    def card_chat(self, in_view=True, card=None):
        f = self.start()
        f.status = "waiting"
        f.card = dict(card or self.CARD)
        f.tabs = [{"id": "other-chat", "text": "Fix login bug", "selected": not in_view},
                  {"id": C2, "text": "Refactor API layer", "selected": in_view, "waiting": True}]
        return f

    def test_pending_reports_card(self):
        f = self.card_chat()
        p = self.gc("pending", "--cdp", str(f.port), "--to", C2)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("Delete /tmp/x.txt", p.stdout)
        self.assertIn("review note: Deletes a file", p.stdout)
        self.assertIn("Refactor API layer", p.stdout)
        self.assertEqual(f.card_clicks, [])

    def test_pending_background_tab_switches_back(self):
        f = self.card_chat(in_view=False)
        p = self.gc("pending", "--cdp", str(f.port), "--to", C2, "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertFalse(out["in_view"])
        self.assertEqual(out["cards"][0]["command"], "Delete /tmp/x.txt")
        self.assertEqual([t["text"] for t in f.tabs if t["selected"]], ["Fix login bug"])

    def test_pending_none(self):
        f = self.card_chat(in_view=False)
        f.card = None
        f.tabs[1]["waiting"] = False
        p = self.gc("pending", "--cdp", str(f.port), "--to", C2)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("no approval card", p.stdout)
        self.assertEqual(f.mouse, [])

    def test_approve_run_in_background_tab(self):
        f = self.card_chat(in_view=False)
        p = self.gc("approve", "--cdp", str(f.port), "--to", C2, "--action", "run", "--back")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertEqual(f.card_clicks, ["Run"])
        self.assertIn("card gone; chat status: generating", p.stdout)
        self.assertEqual([t["text"] for t in f.tabs if t["selected"]], ["Fix login bug"])

    def test_approve_skip_and_accept(self):
        f = self.card_chat()
        self.assertEqual(self.gc("approve", "--cdp", str(f.port), "--to", C2, "--action", "skip").returncode, 0)
        self.assertEqual(f.card_clicks, ["Skip"])
        f.card = {"command": "Edit a.py", "note": "", "buttons": ["Reject", "Accept"]}
        self.assertEqual(self.gc("approve", "--cdp", str(f.port), "--to", C2, "--action", "accept").returncode, 0)
        self.assertEqual(f.card_clicks, ["Skip", "Accept"])

    def test_approve_never_clicks_always(self):
        f = self.card_chat(card={"command": "rm x", "note": "", "buttons": ["Skip", "Always Run"]})
        p = self.gc("approve", "--cdp", str(f.port), "--to", C2, "--action", "run")
        self.assertEqual(p.returncode, 33, p.stderr)
        self.assertEqual(f.card_clicks, [])

    def test_approve_expect_mismatch_refuses(self):
        f = self.card_chat()
        p = self.gc("approve", "--cdp", str(f.port), "--to", C2, "--action", "run", "--expect", "other.txt")
        self.assertEqual(p.returncode, 33, p.stderr)
        self.assertEqual(f.card_clicks, [])

    def test_approve_dry_run_clicks_nothing(self):
        f = self.card_chat(in_view=False)
        p = self.gc("approve", "--cdp", str(f.port), "--to", C2, "--action", "run", "--dry-run")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertIn("would click 'Run'", p.stdout)
        self.assertEqual(f.card_clicks, [])
        self.assertEqual([t["text"] for t in f.tabs if t["selected"]], ["Fix login bug"])

    def test_approve_no_card(self):
        f = self.card_chat()
        f.card = None
        p = self.gc("approve", "--cdp", str(f.port), "--to", C2, "--action", "run")
        self.assertEqual(p.returncode, 32, p.stderr)

class Doctor(SendBase):
    def test_doctor_send_reports(self):
        p = self.gc("doctor", "--send")
        self.assertIn("send readiness", p.stdout)
        self.assertIn("Accessibility trusted", p.stdout)
        self.assertIn("chat input visible via AX: 1 found", p.stdout)
        self.assertIn("2 open tabs", p.stdout)

    def test_doctor_send_flags_lock(self):
        p = self.gc("doctor", "--send", env={"SHIM_LOCKED": "1"})
        self.assertIn("[!!] screen unlocked: LOCKED", p.stdout)
        self.assertNotEqual(p.returncode, 0)

    def test_ax_dump(self):
        p = self.gc("ax-dump", "--grep", "aislash")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("AXTextArea", p.stdout)


class Mcp(SendBase):
    def rpc(self, *msgs, env=None):
        e = dict(self.env)
        e.update(env or {})
        data = "".join(json.dumps(m) + "\n" for m in msgs)
        p = subprocess.run([PY, MCP], input=data, capture_output=True, text=True, env=e, timeout=60)
        return [json.loads(l) for l in p.stdout.splitlines() if l.strip()]

    def call(self, name, args, env=None):
        r = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": name, "arguments": args}}, env=env)
        return r[0]

    def test_initialize_and_list(self):
        r = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                 "clientInfo": {"name": "t", "version": "0"}}},
                     {"jsonrpc": "2.0", "method": "notifications/initialized"},
                     {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                     {"jsonrpc": "2.0", "id": 3, "method": "bogus"},
                     {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope"}})
        self.assertEqual(len(r), 4)
        self.assertEqual(r[0]["result"]["protocolVersion"], "2025-11-25")
        names = {t["name"] for t in r[1]["result"]["tools"]}
        self.assertEqual(names, {"cursor_latest", "cursor_list", "cursor_show", "cursor_targets", "cursor_send",
                                 "cursor_pending", "cursor_approve", "cursor_answer", "cursor_decide", "cursor_summary"})
        send = next(t for t in r[1]["result"]["tools"] if t["name"] == "cursor_send")
        self.assertIn("explicit yes", send["description"])
        self.assertTrue(send["annotations"]["destructiveHint"])
        self.assertEqual(r[2]["error"]["code"], -32601)
        self.assertEqual(r[3]["error"]["code"], -32602)

    def test_unknown_protocol_version_gets_latest(self):
        r = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}})
        self.assertEqual(r[0]["result"]["protocolVersion"], "2025-11-25")

    def test_send_requires_confirm(self):
        for args in ({"prompt": "hi"}, {"prompt": "hi", "confirm": "true"}, {"prompt": "hi", "confirm": 1}):
            res = self.call("cursor_send", args)["result"]
            self.assertTrue(res["isError"])
            self.assertIn("confirm", res["content"][0]["text"])
        self.assertEqual(self.calls(), [])

    def test_send_confirmed_to_tab(self):
        res = self.call("cursor_send", {"prompt": "hi there", "confirm": True, "to": "Refactor API layer",
                                        "verify_seconds": 3})["result"]
        self.assertFalse(res["isError"], res)
        self.assertEqual(self.st()["submitted"][0], {"conv": C2, "text": "hi there"})
        self.assertEqual(res["structuredContent"]["exit_code"], 0)

    def test_send_dry_run_without_confirm(self):
        res = self.call("cursor_send", {"prompt": "hi", "dry_run": True})["result"]
        self.assertFalse(res["isError"], res)
        self.assertNoSubmit()

    def test_send_abort_is_tool_error_with_code(self):
        res = self.call("cursor_send", {"prompt": "hi", "confirm": True}, env={"SHIM_LOCKED": "1"})["result"]
        self.assertTrue(res["isError"])
        self.assertEqual(res["structuredContent"]["exit_code"], 15)

    def test_targets_tool(self):
        res = self.call("cursor_targets", {})["result"]
        self.assertFalse(res["isError"], res)
        self.assertIn("Refactor API layer", res["content"][0]["text"])

    def test_bad_arguments(self):
        r = self.call("cursor_send", {"prompt": 5, "confirm": True})
        self.assertTrue(r["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
