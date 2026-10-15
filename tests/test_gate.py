#!/usr/bin/env python3
"""Gate: self-protection, hold-and-decide, nonce, stop-hook resume. Temp HOME only."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import gc_gate  # noqa: E402

HOOK = os.path.join(ROOT, "hooks", "grok-cursor-hook")
CLI = os.path.join(ROOT, "bin", "grok-cursor")
PY = sys.executable
CID = "00000000-0000-4000-8000-0000000000aa"

def shell(cmd, cid=CID):
    return {"hook_event_name": "beforeShellExecution", "command": cmd, "cwd": "/tmp/proj",
            "sandbox": True, "conversation_id": cid, "cursor_version": "3.24.12",
            "tool_use_id": "tool-1"}

# One row per layer x verb x spelling. want is the permission.
ROWS = [
    ("beforeShellExecution", shell("grok-cursor decide a b allow"), "deny"),
    ("beforeShellExecution", shell("grok-cursor approve --to x"), "deny"),
    ("beforeShellExecution", shell("grok-cursor answer q"), "deny"),
    ("beforeShellExecution", shell("grok-cursor mode away"), "deny"),
    ("beforeShellExecution", shell("grok-cursor hooks uninstall"), "deny"),
    ("beforeShellExecution", shell("grok-cursor always list"), "deny"),
    ("beforeShellExecution", shell("grok-cursor resume abc"), "deny"),
    ("beforeShellExecution", shell("grok-cursor' 'decide a b allow"), "deny"),
    ("beforeShellExecution", shell("cat ~/.grok-cursor/outbox/x"), "deny"),
    ("beforeShellExecution", shell("ls ~/.grok-cursor"), "deny"),
    ("beforeShellExecution", shell("head ~/.grok-cursor/events.jsonl"), "deny"),
    ("beforeShellExecution", shell("python -c 'print(open(\"~/.grok-cursor/outbox/x\").read())'"), "deny"),
    ("beforeShellExecution", shell('python3 -c "print(open(\'$HOME/.grok-cursor/mode\').read())"'), "deny"),
    ("beforeShellExecution", shell("sh -c 'ls ~/.grok-cursor'"), "deny"),
    ("beforeShellExecution", shell("cat ~/.grok-cursor/outbox/x > /tmp/stolen"), "deny"),
    ("beforeShellExecution", shell("cat ~/.grok-cursor/outbox/*"), "deny"),
    ("beforeShellExecution", shell("cat $HOME/.grok-cursor/events.jsonl"), "deny"),
    ("beforeShellExecution", shell("cat ${HOME}/.grok-cursor/mode"), "deny"),
    ("beforeShellExecution", shell("cat /Users/someone/.grok-cursor/mode"), "deny"),
    ("beforeShellExecution", shell("cat ~/.cursor/hooks.json"), "deny"),
    ("beforeShellExecution", shell("ls /Users/someone/.cursor/hooks.json"), "deny"),
    ("beforeShellExecution", shell("grok-cursor send hi"), "deny"),  # mode set to away in that test
    ("beforeReadFile", {"file_path": "~/.grok-cursor/mode", "conversation_id": CID}, "deny"),
    ("beforeReadFile", {"file_path": "~/.grok-cursor/outbox/x", "conversation_id": CID}, "deny"),
    ("beforeReadFile", {"file_path": "~/.cursor/hooks.json", "conversation_id": CID}, "deny"),
    ("beforeReadFile", {"file_path": "/tmp/readme.txt", "conversation_id": CID}, "allow"),
    ("preToolUse", {"tool_name": "Read", "tool_input": {"file_path": "~/.grok-cursor/events.jsonl"},
                    "conversation_id": CID}, "deny"),
    ("preToolUse", {"tool_name": "Write", "tool_input": {"file_path": "~/.cursor/hooks.json"},
                    "conversation_id": CID}, "deny"),
    ("preToolUse", {"tool_name": "Delete", "tool_input": {"path": "~/.grok-cursor/mode"},
                    "conversation_id": CID}, "deny"),
    ("preToolUse", {"tool_name": "Grep", "tool_input": {"path": "~/.grok-cursor", "pattern": "x"},
                    "conversation_id": CID}, "deny"),
    ("preToolUse", {"tool_name": "Glob", "tool_input": {"glob_pattern": "~/.grok-cursor/**"},
                    "conversation_id": CID}, "deny"),
    ("preToolUse", {"tool_name": "ListDir", "tool_input": {"target_directory": "~/.grok-cursor"},
                    "conversation_id": CID}, "deny"),
    ("preToolUse", {"tool_name": "Read", "tool_input": {"file_path": "/tmp/ok.txt"},
                    "conversation_id": CID}, "allow"),
    ("beforeMCPExecution", {"tool_name": "search", "mcp_server_name": "other",
                            "tool_input": {"q": "cat ~/.grok-cursor/mode"}, "conversation_id": CID}, "deny"),
    ("beforeMCPExecution", {"tool_name": "cursor_send", "mcp_server_name": "grok-cursor",
                            "tool_input": {"prompt": "hi"}, "conversation_id": CID}, "deny"),
]

class GateBase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="gc-gate-")
        self.gc = os.path.join(self.home, ".grok-cursor")
        os.makedirs(self.gc)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("GROK_CURSOR_", "GROK_BRIDGE_", "CURSOR_"))}
        self.env["HOME"] = self.home
        self.write_config(0.4)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def write_config(self, hold, ttl=1200):
        with open(os.path.join(self.gc, "config.json"), "w") as fh:
            json.dump({"hold": hold, "ttl": ttl}, fh)

    def write_mode(self, mode):
        with open(os.path.join(self.gc, "mode"), "w") as fh:
            fh.write(mode + "\n")

    def hook(self, event, payload, timeout=8):
        p = subprocess.run([PY, HOOK, event], input=json.dumps(payload), capture_output=True,
                           text=True, env=self.env, timeout=timeout)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        body = json.loads(p.stdout)
        self.assertIn(body.get("permission", body.get("followup_message")),
                      ("allow", "deny", body.get("followup_message")))
        self.assertNotEqual(body, {})
        return body

    def start(self, cmd):
        proc = subprocess.Popen([PY, HOOK, "beforeShellExecution"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env)
        proc.stdin.write(json.dumps(shell(cmd)))
        proc.stdin.close()
        return proc

    def wait_request(self, proc, timeout=4):
        directory = os.path.join(self.gc, "decisions")
        deadline = time.time() + timeout
        while time.time() < deadline:
            names = os.listdir(directory) if os.path.isdir(directory) else []
            names = [n for n in names if n.endswith(".json")]
            if names:
                with open(os.path.join(directory, names[0]), encoding="utf-8") as fh:
                    obj = json.load(fh)
                with open(os.path.join(self.gc, "outbox", obj["req_id"]), encoding="utf-8") as fh:
                    return obj, fh.read().strip()
            if proc.poll() is not None:
                out, err = proc.communicate()
                self.fail("hook exited early: %s %s" % (out, err))
            time.sleep(0.02)
        proc.kill()
        out, err = proc.communicate()
        self.fail("no decision request\n%s\n%s" % (out, err))

    def decide(self, req_id, nonce, verdict, reason="", human=False):
        args = [PY, CLI, "decide", req_id, nonce, verdict]
        if reason:
            args += ["--reason", reason]
        if human:
            args.append("--human")
        return subprocess.run(args, capture_output=True, text=True, env=self.env, timeout=10)

    def only_request(self):
        names = [n for n in os.listdir(os.path.join(self.gc, "decisions")) if n.endswith(".json")]
        self.assertEqual(len(names), 1, names)
        with open(os.path.join(self.gc, "decisions", names[0]), encoding="utf-8") as fh:
            obj = json.load(fh)
        with open(os.path.join(self.gc, "outbox", obj["req_id"]), encoding="utf-8") as fh:
            return obj, fh.read().strip()

class SelfProtection(GateBase):
    def test_rows(self):
        deny_rows = [r for r in ROWS if r[2] == "deny"]
        self.assertGreaterEqual(len(deny_rows), 20)
        self.write_mode("away")   # so cursor_send and grok-cursor send are in scope
        for event, payload, want in ROWS:
            with self.subTest(event=event, payload=payload):
                body = self.hook(event, payload)
                self.assertEqual(body["permission"], want, body)
                if event == "beforeReadFile":
                    self.assertNotIn("agent_message", body)

    def test_cat_outbox_denied_before_open(self):
        target = os.path.join(self.gc, "outbox", "x")
        opened = []
        real_open = open

        def guard(path, *args, **kwargs):
            if str(path).endswith(os.path.join("outbox", "x")):
                opened.append(path)
                raise AssertionError("opened %s" % path)
            return real_open(path, *args, **kwargs)

        old = os.environ.get("HOME")
        os.environ["HOME"] = self.home
        try:
            with mock.patch("builtins.open", guard):
                reply = gc_gate.handle("beforeShellExecution", shell("cat ~/.grok-cursor/outbox/x"), self.gc)
        finally:
            if old is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old
        self.assertEqual(reply["permission"], "deny")
        self.assertEqual(opened, [])
        self.assertFalse(os.path.exists(os.path.dirname(target)))
        # the same deny through the real hook process
        body = self.hook("beforeShellExecution", shell("cat ~/.grok-cursor/outbox/x"))
        self.assertEqual(body["permission"], "deny")
        self.assertFalse(os.path.exists(os.path.join(self.gc, "outbox")))

    def test_present_allows_send(self):
        # send is away-only; in present it is an ordinary medium command, so it holds
        self.write_mode("present")
        self.write_config(0.2)
        body = self.hook("beforeShellExecution", shell("grok-cursor send hi"))
        self.assertEqual(body["permission"], "deny")
        self.assertIn("waiting for the human", body["user_message"])

class HoldAndDecide(GateBase):
    def test_present_medium_decide_allow(self):
        self.write_mode("present")
        self.write_config(2)
        proc = self.start("touch /tmp/gc-a-test")
        obj, nonce = self.wait_request(proc)
        self.assertEqual(obj["decider"], "human")
        self.assertEqual(obj["risk"], "medium")
        raw = open(os.path.join(self.gc, "decisions", obj["req_id"] + ".json")).read()
        self.assertNotIn(nonce, raw)
        d = self.decide(obj["req_id"], nonce, "allow", reason="test")
        self.assertEqual(d.returncode, 0, d.stderr)
        out, err = proc.communicate(timeout=5)
        self.assertEqual(json.loads(out)["permission"], "allow", err)
        events = open(os.path.join(self.gc, "events.jsonl")).read()
        self.assertNotIn(nonce, events)
        self.assertIn("test", events)

    def test_present_high_needs_human(self):
        self.write_mode("present")
        self.write_config(3)
        proc = self.start("git push")
        obj, nonce = self.wait_request(proc)
        self.assertEqual(obj["risk"], "high")
        self.assertEqual(obj["decider"], "human")
        bad = self.decide(obj["req_id"], nonce, "allow", reason="nope")
        self.assertEqual(bad.returncode, 2, bad.stdout)
        self.assertIn("--human", bad.stderr)
        again = json.load(open(os.path.join(self.gc, "decisions", obj["req_id"] + ".json")))
        self.assertNotIn("decided", again)
        self.assertIsNone(proc.poll(), "hook returned before a valid decision")
        good = self.decide(obj["req_id"], nonce, "allow", reason="human said yes", human=True)
        self.assertEqual(good.returncode, 0, good.stderr)
        out, err = proc.communicate(timeout=5)
        self.assertEqual(json.loads(out)["permission"], "allow", err)

    def test_present_expiry_then_stop_resume(self):
        for cmd, risk in (("touch /tmp/gc-a-test", "medium"), ("git push --dry-run", "high")):
            with self.subTest(risk=risk):
                shutil.rmtree(self.gc)
                os.makedirs(self.gc)
                self.write_mode("present")
                self.write_config(0.35)
                body = self.hook("beforeShellExecution", shell(cmd), timeout=4)
                self.assertEqual(body["permission"], "deny", body)
                self.assertIn("waiting for the human on", body["user_message"])
                self.assertIn("(%s)" % risk, body["user_message"])
                self.assertIn("held", body["agent_message"])
                self.assertIn("resumed if approved", body["agent_message"])
                obj, nonce = self.only_request()
                d = self.decide(obj["req_id"], nonce, "allow", human=(risk == "high"), reason="late")
                self.assertEqual(d.returncode, 0, d.stderr)
                stop = self.hook("stop", {"hook_event_name": "stop", "status": "completed",
                                          "conversation_id": CID, "loop_count": 0})
                msg = stop["followup_message"]
                self.assertEqual(msg, "grok-cursor: decision %s allowed; re-run: %s" % (obj["req_id"], cmd))
                again = self.hook("beforeShellExecution", shell(cmd))
                self.assertEqual(again["permission"], "allow", again)
                self.write_config(0.3)
                third = self.hook("beforeShellExecution", shell(cmd), timeout=4)
                self.assertEqual(third["permission"], "deny")

    def test_present_low_and_always(self):
        self.write_mode("present")
        t0 = time.time()
        body = self.hook("beforeShellExecution", shell("ls -la"))
        self.assertEqual(body, {"permission": "allow"})
        self.assertLess(time.time() - t0, 0.8)
        self.assertFalse(os.path.isdir(os.path.join(self.gc, "decisions")))
        with open(os.path.join(self.gc, "always.json"), "w") as fh:
            json.dump({"commands": ["touch /tmp/gc-a-test"]}, fh)
        t0 = time.time()
        body = self.hook("beforeShellExecution", shell("touch /tmp/gc-a-test"))
        self.assertEqual(body["permission"], "allow")
        self.assertLess(time.time() - t0, 0.8)
        self.assertFalse(os.path.isdir(os.path.join(self.gc, "decisions")))

    def test_always_cannot_allow_high(self):
        self.write_mode("present")
        self.write_config(0.3)
        with open(os.path.join(self.gc, "always.json"), "w") as fh:
            json.dump({"commands": ["git push"]}, fh)
        body = self.hook("beforeShellExecution", shell("git push"), timeout=4)
        self.assertEqual(body["permission"], "deny")
        self.assertIn("waiting for the human", body["user_message"])

    def test_away_high_immediate_then_resume(self):
        self.write_mode("away")
        t0 = time.time()
        body = self.hook("beforeShellExecution", shell("git push"))
        self.assertLess(time.time() - t0, 0.8)
        self.assertEqual(body["permission"], "deny")
        self.assertEqual(body["agent_message"], "grok-cursor: stop and wait for the human")
        obj, nonce = self.only_request()
        self.assertEqual(obj["decider"], "human")
        bad = self.decide(obj["req_id"], nonce, "allow")
        self.assertEqual(bad.returncode, 2)
        good = self.decide(obj["req_id"], nonce, "allow", human=True, reason="ok")
        self.assertEqual(good.returncode, 0, good.stderr)
        stop = self.hook("stop", {"hook_event_name": "stop", "status": "completed",
                                  "conversation_id": CID})
        self.assertIn("re-run: git push", stop["followup_message"])

    def test_away_medium_grok_decides(self):
        self.write_mode("away")
        self.write_config(2)
        proc = self.start("touch /tmp/gc-a-test")
        obj, nonce = self.wait_request(proc)
        self.assertEqual(obj["decider"], "grok")
        d = self.decide(obj["req_id"], nonce, "allow", reason="because tests")
        self.assertEqual(d.returncode, 0, d.stderr)
        out, err = proc.communicate(timeout=5)
        self.assertEqual(json.loads(out)["permission"], "allow", err)

    def test_forged_allow_does_not_pass(self):
        self.write_mode("present")
        self.write_config(0.45)
        proc = self.start("git push")
        obj, nonce = self.wait_request(proc)
        path = os.path.join(self.gc, "decisions", obj["req_id"] + ".json")
        forged = dict(obj)
        forged.update(decided="allow", human=True, proof="forged")
        with open(path, "w") as fh:
            json.dump(forged, fh)
        out, err = proc.communicate(timeout=4)
        body = json.loads(out)
        self.assertEqual(body["permission"], "deny", err)
        self.assertNotEqual(body.get("permission"), "allow")

class DecideShape(GateBase):
    def test_rejects_before_writing(self):
        self.write_mode("away")
        self.hook("beforeShellExecution", shell("git push"))
        obj, nonce = self.only_request()
        path = os.path.join(self.gc, "decisions", obj["req_id"] + ".json")
        mtime = os.path.getmtime(path)
        for args in (
            ["nope"],
            [obj["req_id"], nonce, "allow", "extra"],
            [obj["req_id"], "0" * 32, "allow", "--human"],
            [obj["req_id"], nonce, "allow", "--human", "--reason"],
            ["no-such-id", nonce, "deny"],
        ):
            p = subprocess.run([PY, CLI, "decide"] + args, capture_output=True, text=True,
                               env=self.env, timeout=10)
            self.assertEqual(p.returncode, 2, args)
            self.assertEqual(os.path.getmtime(path), mtime, args)
            self.assertNotIn("decided", json.load(open(path)))
        ok = self.decide(obj["req_id"], nonce, "deny", human=True, reason="no")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        again = self.decide(obj["req_id"], nonce, "allow", human=True)
        self.assertEqual(again.returncode, 2)
        self.assertEqual(json.load(open(path))["decided"], "deny")

class AlwaysAndHooks(GateBase):
    def test_always_refuses_unless_low(self):
        add = lambda *a: subprocess.run([PY, CLI, "always"] + list(a), capture_output=True,
                                        text=True, env=self.env, timeout=10)
        bad = add("add", "git push")
        self.assertEqual(bad.returncode, 2, bad.stdout)
        self.assertIn("only for low", bad.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.gc, "always.json")))
        med = add("add", "touch /tmp/gc-a-test")
        self.assertEqual(med.returncode, 2)
        ok = add("add", "ls -la")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        import stat as _stat
        self.assertEqual(_stat.S_IMODE(os.stat(os.path.join(self.gc, "always.json")).st_mode), 0o600)
        listed = add("list")
        self.assertEqual(listed.stdout, "ls -la\n")
        again = add("add", "ls -la")
        self.assertEqual(again.returncode, 0)
        self.assertEqual(add("list").stdout, "ls -la\n")
        self.assertIn("not listed", add("rm", "git status").stdout)
        self.assertIn("removed", add("rm", "ls -la").stdout)
        self.assertIn("no always", add("list").stdout)

    def test_hooks_status_lists_nine_failclosed(self):
        p = subprocess.run([PY, CLI, "hooks", "install"], capture_output=True, text=True,
                           env=self.env, timeout=15)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        st = subprocess.run([PY, CLI, "hooks", "status"], capture_output=True, text=True,
                            env=self.env, timeout=10)
        self.assertEqual(st.returncode, 0, st.stdout)
        self.assertIn("9 grok-cursor entries", st.stdout)
        for event in ("sessionStart", "beforeSubmitPrompt", "afterAgentResponse", "stop", "sessionEnd",
                      "beforeShellExecution", "beforeMCPExecution", "beforeReadFile", "preToolUse"):
            self.assertIn(event, st.stdout)
        self.assertEqual(st.stdout.count("failClosed"), 4)
        self.assertIn("matcher Read|Write|Delete|Grep|Glob|ListDir", st.stdout)
        self.assertIn("cursor_version: none seen", st.stdout)
        # a gate run records the version the status command prints
        self.hook("beforeShellExecution", shell("ls"))
        st = subprocess.run([PY, CLI, "hooks", "status"], capture_output=True, text=True,
                            env=self.env, timeout=10)
        self.assertIn("cursor_version: 3.24.12", st.stdout)
        flagged = json.load(open(os.path.join(self.home, ".cursor", "hooks.json")))
        flagged["hooks"]["beforeShellExecution"] = [
            i for i in flagged["hooks"]["beforeShellExecution"]
            if "grok-cursor-hook" not in i.get("command", "")]
        flagged["hooks"]["beforeShellExecution"].append(
            {"command": "/x/grok-cursor-hook beforeShellExecution", "timeout": 53})
        with open(os.path.join(self.home, ".cursor", "hooks.json"), "w") as fh:
            json.dump(flagged, fh)
        st = subprocess.run([PY, CLI, "hooks", "status"], capture_output=True, text=True,
                            env=self.env, timeout=10)
        self.assertEqual(st.returncode, 1)
        self.assertIn("FLAG: beforeShellExecution missing failClosed", st.stdout)

class Resume(GateBase):
    def test_resume_message_and_refuses_until_allowed(self):
        self.assertEqual(gc_gate.resume_message({"req_id": "ab", "cmd": "ls"}),
                         "grok-cursor: decision ab allowed; re-run: ls")
        self.write_mode("away")
        self.hook("beforeShellExecution", shell("git push"))
        obj, nonce = self.only_request()
        early = subprocess.run([PY, CLI, "resume", obj["req_id"]], capture_output=True,
                               text=True, env=self.env, timeout=10)
        self.assertEqual(early.returncode, 2, early.stdout)
        self.assertIn("not an undelivered allow", early.stderr)
        missing = subprocess.run([PY, CLI, "resume", "no-such"], capture_output=True,
                                 text=True, env=self.env, timeout=10)
        self.assertEqual(missing.returncode, 2)
        self.decide(obj["req_id"], nonce, "allow", human=True, reason="late")
        # deliver() is what `resume` calls after send succeeds. Once delivered,
        # the stop hook must not send the follow-up a second time.
        gc_gate.deliver(self.gc, obj["req_id"])
        p = subprocess.run([PY, HOOK, "stop"], input=json.dumps(
            {"hook_event_name": "stop", "status": "completed", "conversation_id": CID}),
                           capture_output=True, text=True, env=self.env, timeout=8)
        self.assertEqual(json.loads(p.stdout), {})

class Summary(GateBase):
    def test_away_decision_shows_its_reason(self):
        import gc_events
        gc_events.append("decision_requested", home=self.gc, req_id="r9", risk="medium",
                         command="touch /tmp/gc-a-test", conversation_id=CID, mode="away",
                         decider="grok")
        gc_events.append("decided", home=self.gc, req_id="r9", verdict="allow",
                         reason="because tests", risk="medium", human=False)
        p = subprocess.run([PY, CLI, "summary", "--since", "30m"], capture_output=True,
                           text=True, env=self.env, timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(CID, p.stdout)
        self.assertIn("away", p.stdout)
        self.assertIn("reason: because tests", p.stdout)
        self.assertIn("touch /tmp/gc-a-test", p.stdout)

class HoldDefault(GateBase):
    def test_probes_default_and_fallback(self):
        os.remove(os.path.join(self.gc, "config.json"))
        self.assertEqual(gc_gate.hold_seconds(self.gc), 48)
        self.assertEqual(gc_gate.hold_from_text(""), 60)
        self.assertEqual(gc_gate.hold_from_text("Ceiling ≥ 10 s\np95 = 20 s\n"), 10)
        self.assertTrue(gc_gate.followup_enabled())

if __name__ == "__main__":
    unittest.main()
