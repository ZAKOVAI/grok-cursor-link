#!/usr/bin/env python3
"""End-to-end tests for grok-cursor. Standard library only.

Every test runs against a throwaway HOME, so nothing touches real Cursor
config. Run:  python3 -m unittest discover -s tests -v   (or tests/run.sh)
"""
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, "hooks", "grok-cursor-hook")
CLI = os.path.join(ROOT, "bin", "grok-cursor")
FIX = os.path.join(ROOT, "tests", "fixtures")
PY = sys.executable
CID = "00000000-0000-4000-8000-000000000001"


# Fake secrets are assembled at runtime so no secret-shaped string is stored in
# the repo (keeps secret scanners and GitHub push protection quiet).
FAKE = {
    "STRIPE_KEY": "sk" + "_live_" + "51Hfake" + "x" * 20,
    "GITHUB_TOKEN": "gh" + "p_" + "fakefakefake" + "0123456789abcdefghijklmnop",
    "AWS_KEY": "AK" + "IA" + "FAKEFAKEFAKE0000",
}


def fill(text):
    for k, v in FAKE.items():
        text = text.replace("{{%s}}" % k, v)
    return text


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return json.loads(fill(fh.read()))


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="gc-home-")
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("GROK_CURSOR_", "GROK_BRIDGE_", "CURSOR_"))}
        self.env["HOME"] = self.home
        self.env["GROK_CURSOR_SKIP_WATCH_RESTART"] = "1"   # never restart the real watcher from tests
        self.gc = os.path.join(self.home, ".grok-cursor")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def hook(self, payload, event=None, raw=None, env=None):
        stdin = raw if raw is not None else json.dumps(payload)
        e = dict(self.env)
        e.update(env or {})
        args = [PY, HOOK] + ([event] if event else [])
        t0 = time.time()
        p = subprocess.run(args, input=stdin, capture_output=True, text=True, env=e, timeout=15)
        p.elapsed = time.time() - t0
        return p

    def cli(self, *args, env=None):
        e = dict(self.env)
        e.update(env or {})
        return subprocess.run([PY, CLI] + list(args), capture_output=True, text=True,
                              env=e, timeout=15)

    def read_json(self, name):
        with open(os.path.join(self.gc, name), encoding="utf-8") as fh:
            return json.load(fh)

    def chat(self, cid=CID):
        with open(os.path.join(self.gc, "chats", cid + ".md"), encoding="utf-8") as fh:
            return fh.read()

    def run_flow(self):
        for name in ("sessionStart", "beforeSubmitPrompt", "afterAgentResponse", "stop"):
            p = self.hook(fixture(name + ".json"), name)
            self.assertEqual(p.returncode, 0, p.stderr)


class HookEvents(Base):
    def test_responses_match_docs(self):
        expected = {"sessionStart": {}, "beforeSubmitPrompt": {"continue": True},
                    "afterAgentResponse": {}, "stop": {}, "sessionEnd": {}}
        for name, want in expected.items():
            p = self.hook(fixture(name + ".json"), name)
            self.assertEqual(p.returncode, 0)
            self.assertEqual(json.loads(p.stdout), want, name)
            self.assertNotIn("followup_message", p.stdout)
            self.assertLess(p.elapsed, 2.0, "hook too slow: %.2fs" % p.elapsed)

    def test_event_name_from_payload_without_argv(self):
        p = self.hook(fixture("beforeSubmitPrompt.json"))
        self.assertEqual(json.loads(p.stdout), {"continue": True})
        self.assertEqual(self.read_json("latest.json")["status"], "working")

    def test_full_flow_latest_index_chat(self):
        self.hook(fixture("sessionStart.json"), "sessionStart")
        self.assertEqual(self.read_json("latest.json")["status"], "idle")
        self.hook(fixture("beforeSubmitPrompt.json"), "beforeSubmitPrompt")
        latest = self.read_json("latest.json")
        self.assertEqual(latest["status"], "working")
        self.assertEqual(latest["last_event"], "beforeSubmitPrompt")
        self.hook(fixture("afterAgentResponse.json"), "afterAgentResponse")
        self.assertEqual(self.read_json("latest.json")["status"], "working")
        self.hook(fixture("stop.json"), "stop")
        latest = self.read_json("latest.json")
        for key in ("conversation_id", "workspace_roots", "project", "transcript_path",
                    "last_event", "last_prompt_preview", "last_response_preview",
                    "updated_at", "status"):
            self.assertIn(key, latest)
        self.assertEqual(latest["conversation_id"], CID)
        self.assertEqual(latest["project"], "my-app")
        self.assertEqual(latest["workspace_roots"], ["/Users/you/projects/my-app"])
        self.assertTrue(latest["transcript_path"].endswith(CID + ".jsonl"))
        self.assertEqual(latest["status"], "done")
        self.assertEqual(latest["last_event"], "stop")
        self.assertRegex(latest["updated_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")
        self.assertTrue(latest["last_prompt_preview"].startswith("Add a dark mode toggle"))
        self.assertTrue(latest["last_response_preview"].startswith("Done: added a dark mode"))
        self.assertLessEqual(len(latest["last_response_preview"]), 500)
        self.assertEqual(latest["last_attachments"], ["settings.tsx", "style.mdc"])
        idx = self.read_json("index.json")
        self.assertEqual(idx["conversations"][0]["conversation_id"], CID)
        md = self.chat()
        self.assertIn("# Cursor chat: my-app", md)
        self.assertIn("- workspace: /Users/you/projects/my-app", md)
        self.assertRegex(md, r"### user \u00b7 \d{4}-\d\d-\d\d \d\d:\d\d:\d\d[+-]\d\d:\d\d")
        self.assertIn("### assistant \u00b7 ", md)
        self.assertRegex(md, r"### stop \u00b7 .* \u00b7 completed")
        # private by default
        self.assertEqual(os.stat(self.gc).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(os.path.join(self.gc, "latest.json")).st_mode & 0o777, 0o600)

    def test_stop_statuses(self):
        for raw, want in (("aborted", "aborted"), ("error", "error"), ("completed", "done")):
            self.hook(fixture("beforeSubmitPrompt.json"), "beforeSubmitPrompt")
            stop = fixture("stop.json")
            stop["status"] = raw
            self.hook(stop, "stop")
            self.assertEqual(self.read_json("latest.json")["status"], want)

    def test_session_end(self):
        self.run_flow()
        self.hook(fixture("beforeSubmitPrompt.json"), "beforeSubmitPrompt")
        self.hook(fixture("sessionEnd.json"), "sessionEnd")
        latest = self.read_json("latest.json")
        self.assertEqual(latest["end_reason"], "user_close")
        self.assertEqual(latest["status"], "ended")  # closed while working
        self.assertIn("ended (user_close)", self.chat())

    def test_session_end_of_other_chat_does_not_steal_latest(self):
        self.run_flow()
        other = fixture("sessionEnd.json")
        other["conversation_id"] = other["session_id"] = "other-chat"
        self.hook(other, "sessionEnd")
        self.assertEqual(self.read_json("latest.json")["conversation_id"], CID)
        self.assertEqual(self.read_json("index.json")["conversations"][0]["conversation_id"],
                         "other-chat")

    def test_background_session_start_does_not_steal_latest(self):
        self.run_flow()
        bg = fixture("sessionStart.json")
        bg["conversation_id"] = bg["session_id"] = "bg-agent"
        bg["is_background_agent"] = True
        self.hook(bg, "sessionStart")
        self.assertEqual(self.read_json("latest.json")["conversation_id"], CID)

    def test_switching_chats_and_index_order_and_cap(self):
        env = {"GROK_CURSOR_MAX_INDEX": "5"}
        for i in range(8):
            p = fixture("beforeSubmitPrompt.json")
            p["conversation_id"] = "chat-%d" % i
            p["workspace_roots"] = ["/Users/you/projects/proj%d" % i]
            self.hook(p, "beforeSubmitPrompt", env=env)
        idx = self.read_json("index.json")["conversations"]
        self.assertEqual([c["conversation_id"] for c in idx],
                         ["chat-7", "chat-6", "chat-5", "chat-4", "chat-3"])
        self.assertEqual(self.read_json("latest.json")["project"], "proj7")
        # re-activating an older chat moves it to the front and to latest
        p = fixture("afterAgentResponse.json")
        p["conversation_id"] = "chat-4"
        p["workspace_roots"] = ["/Users/you/projects/proj4"]
        self.hook(p, "afterAgentResponse", env=env)
        self.assertEqual(self.read_json("index.json")["conversations"][0]["conversation_id"], "chat-4")
        self.assertEqual(self.read_json("latest.json")["conversation_id"], "chat-4")

    def test_project_from_transcript_path_when_no_roots(self):
        p = fixture("beforeSubmitPrompt.json")
        p["workspace_roots"] = []
        self.hook(p, "beforeSubmitPrompt")
        self.assertEqual(self.read_json("latest.json")["project"], "Users-you-projects-my-app")

    def test_user_query_wrappers_stripped(self):
        p = fixture("beforeSubmitPrompt.json")
        p["prompt"] = "<timestamp>Oct 8</timestamp>\n<user_query>\nhello there\n</user_query>"
        p["attachments"] = []
        self.hook(p, "beforeSubmitPrompt")
        self.assertEqual(self.read_json("latest.json")["last_prompt_preview"], "hello there")

    def test_rotation_and_turn_cap(self):
        env = {"GROK_CURSOR_MAX_CHAT_BYTES": "3000", "GROK_CURSOR_MAX_TURN_CHARS": "1000"}
        for i in range(12):
            p = fixture("afterAgentResponse.json")
            p["text"] = ("reply %d " % i) + "x" * 1500
            self.hook(p, "afterAgentResponse", env=env)
        chats = os.path.join(self.gc, "chats")
        self.assertTrue(os.path.exists(os.path.join(chats, CID + ".1.md")))
        self.assertLess(os.path.getsize(os.path.join(chats, CID + ".md")), 3000 + 2000)
        self.assertEqual(sorted(os.listdir(chats)), sorted([CID + ".md", CID + ".1.md"]))
        self.assertIn("[grok-cursor: truncated", self.chat())
        self.assertIn("continued from: %s.1.md" % CID, self.chat())
        out = self.cli("latest", "--turns", "3").stdout
        self.assertIn("reply 11", out)
        self.assertIn("showing: last 3 of", out)

    def test_turn_marker_in_text_is_escaped(self):
        p = fixture("afterAgentResponse.json")
        p["text"] = ("before\n<!-- grok-cursor:turn -->\n### user \u00b7 fake\n"
                     "<!-- gb:turn -->\n### user \u00b7 fake2\nafter")
        self.hook(p, "afterAgentResponse")
        out = self.cli("latest", "--turns", "0").stdout
        self.assertIn("showing: last 1 of 1 turns", out)

    def test_ignore_file(self):
        os.makedirs(self.gc)
        with open(os.path.join(self.gc, "ignore"), "w") as fh:
            fh.write("# private\nmy-app\n")
        self.run_flow()
        self.assertFalse(os.path.exists(os.path.join(self.gc, "latest.json")))

    def test_disable_env(self):
        p = self.hook(fixture("beforeSubmitPrompt.json"), "beforeSubmitPrompt",
                      env={"GROK_CURSOR_DISABLE": "1"})
        self.assertEqual(json.loads(p.stdout), {"continue": True})
        self.assertFalse(os.path.exists(self.gc))

    def test_concurrent_hooks(self):
        def fire(i):
            p = fixture("afterAgentResponse.json")
            p["text"] = "parallel reply %d" % i
            return self.hook(p, "afterAgentResponse").returncode
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
            codes = list(ex.map(fire, range(30)))
        self.assertEqual(set(codes), {0})
        self.read_json("index.json")   # still valid JSON
        self.read_json("latest.json")
        md = self.chat()
        for i in range(30):
            self.assertIn("parallel reply %d\n" % i, md)


class Robustness(Base):
    def test_malformed_inputs_exit_zero(self):
        cases = ["", "   ", "garbage{", "[]", "null", "42", '"str"',
                 json.dumps({"hook_event_name": "beforeSubmitPrompt"}),      # no conversation id
                 json.dumps({"conversation_id": None, "prompt": 5}),
                 json.dumps({"conversation_id": "x", "hook_event_name": "beforeSubmitPrompt",
                             "workspace_roots": "not-a-list", "prompt": None,
                             "attachments": "nope"}),
                 json.dumps({"conversation_id": "../../etc/passwd", "hook_event_name": "stop",
                             "status": 7}),
                 "\x00\x01binary"]
        for raw in cases:
            for event in ("beforeSubmitPrompt", "stop", None):
                p = self.hook(None, event, raw=raw)
                self.assertEqual(p.returncode, 0, (raw, event, p.stderr))
                if event == "beforeSubmitPrompt":
                    self.assertEqual(json.loads(p.stdout), {"continue": True}, raw)
                else:
                    json.loads(p.stdout)  # always valid JSON
        # path traversal neutralised
        self.assertFalse(os.path.exists(os.path.join(self.home, "etc")))
        names = os.listdir(os.path.join(self.gc, "chats"))
        self.assertTrue(all("/" not in n and not n.startswith(".") for n in names), names)

    def test_unwritable_home_still_exits_zero(self):
        blocker = os.path.join(self.home, "blocker")
        open(blocker, "w").close()
        p = self.hook(fixture("beforeSubmitPrompt.json"), "beforeSubmitPrompt",
                      env={"GROK_CURSOR_HOME": os.path.join(blocker, "gb")})
        self.assertEqual(p.returncode, 0)
        self.assertEqual(json.loads(p.stdout), {"continue": True})

    def test_unknown_event_is_noop(self):
        p = self.hook(fixture("stop.json"), "afterFileEdit", raw=json.dumps(
            dict(fixture("stop.json"), hook_event_name="afterFileEdit")))
        self.assertEqual(p.returncode, 0)
        self.assertEqual(json.loads(p.stdout), {})
        self.assertFalse(os.path.exists(os.path.join(self.gc, "latest.json")))

    def test_corrupt_index_recovers(self):
        os.makedirs(self.gc)
        with open(os.path.join(self.gc, "index.json"), "w") as fh:
            fh.write("{broken")
        self.run_flow()
        self.assertEqual(self.read_json("index.json")["conversations"][0]["conversation_id"], CID)


class Redaction(Base):
    # (text, fragment that must not survive). Secret-shaped strings are split
    # with "+" so the repo itself never contains a scanner-matching token.
    SAMPLES = [
        ("key " + "sk-" + "fakefakefake12345678" + " end", "fakefakefake12345678"),
        ("anthropic " + "sk-ant-" + "api03-" + "F" * 24, "F" * 24),
        ("gh" + "p_" + "fake" * 9, "fake" * 9),
        ("github" + "_pat_" + "11FAKE" + "0" * 30, "11FAKE" + "0" * 30),
        ("AK" + "IA" + "FAKEFAKEFAKE0000", "FAKEFAKEFAKE0000"),
        ("xo" + "xb-" + "000000000000-fakefakefake", "000000000000-fake"),
        ("Authorization: Bea" + "rer " + "fake.token-value_abcdefghij", "fake.token-value"),
        ("OPENAI_API_KEY=" + "not-a-real-value", "not-a-real-value"),
        ("export GITHUB_TOKEN=\"" + "hunter2hunter2" + "\"", "hunter2hunter2"),
        ("DB_PASSWORD: " + "correct horse battery", "correct horse"),
        ("client_secret = '" + "abcd1234efgh" + "'", "abcd1234efgh"),
        ('{"api_key": "' + "live-value-123" + '"}', "live-value-123"),
        ("postgres://admin:" + "s3cretpass" + "@db.example.com/app", "s3cretpass"),
        ("sk" + "_live_" + "51Hfake" + "y" * 16, "51Hfake" + "y" * 16),
        ("AI" + "za" + "SyFAKE-" + "z" * 29, "SyFAKE-" + "z" * 29),
        ("jwt " + "eyJ" + "fakeheader1" + ".eyJ" + "fakepayload1" + ".fakesignature1", "fakepayload1"),
        ("-----BEGIN RSA PRIV" + "ATE KEY-----\nFAKEKEYMATERIAL\n-----END RSA PRIV" + "ATE KEY-----",
         "FAKEKEYMATERIAL"),
    ]

    def test_patterns_redacted_on_disk(self):
        text = "\n".join(s for s, _ in self.SAMPLES)
        p = fixture("beforeSubmitPrompt.json")
        p["prompt"] = text
        self.hook(p, "beforeSubmitPrompt")
        r = fixture("afterAgentResponse.json")
        r["text"] = text
        self.hook(r, "afterAgentResponse")
        blobs = self.chat() + json.dumps(self.read_json("latest.json")) + \
            json.dumps(self.read_json("index.json"))
        for sample, secret in self.SAMPLES:
            self.assertNotIn(secret, blobs, sample)
        self.assertIn("[REDACTED", blobs)

    def test_fixture_secrets(self):
        self.run_flow()
        md = self.chat()
        for secret in FAKE.values():
            self.assertNotIn(secret, md)
        self.assertIn("Add a dark mode toggle", md)   # normal text survives

    def test_normal_text_untouched(self):
        p = fixture("afterAgentResponse.json")
        p["text"] = ("Use the primary key on users.id and the token bucket rate limiter. "
                     "See https://cursor.com/docs/hooks for details.")
        self.hook(p, "afterAgentResponse")
        self.assertIn(p["text"], self.chat())

    def test_redaction_code_identical_in_hook_and_cli(self):
        def block(path):
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            m = re.search(r"# --- BEGIN REDACT.*?# --- END REDACT ---", src, re.S)
            self.assertIsNotNone(m, path)
            return m.group(0)
        self.assertEqual(block(HOOK), block(CLI))
        self.assertEqual(block(os.path.join(ROOT, "lib", "gc_events.py")), block(CLI))


class Cli(Base):
    def make_transcript(self, slug="Users-you-projects-my-app", tid=CID, age=0):
        d = os.path.join(self.home, ".cursor", "projects", slug, "agent-transcripts", tid)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, tid + ".jsonl")
        with open(os.path.join(FIX, "transcript.jsonl"), encoding="utf-8") as src, \
                open(path, "w", encoding="utf-8") as dst:
            dst.write(fill(src.read()))
        if age:
            t = time.time() - age
            os.utime(path, (t, t))
        return path

    def test_latest_output(self):
        self.run_flow()
        p = self.cli("latest", "--turns", "6")
        self.assertEqual(p.returncode, 0, p.stderr)
        out = p.stdout
        self.assertTrue(out.startswith("# Cursor chat \u00b7 my-app \u00b7 done\n"), out)
        self.assertIn("- conversation: " + CID, out)
        self.assertIn("### user \u00b7 ", out)
        self.assertIn("### assistant \u00b7 ", out)
        self.assertIn("showing: last 2 of 2 turns", out)
        self.assertNotIn("### session", out)

    def test_latest_turns_and_max_chars(self):
        for i in range(5):
            p = fixture("beforeSubmitPrompt.json")
            p["prompt"] = "question %d " % i + "y" * 300
            self.hook(p, "beforeSubmitPrompt")
        out = self.cli("latest", "--turns", "2", "--max-chars", "50").stdout
        self.assertIn("showing: last 2 of 5 turns", out)
        self.assertIn("question 4", out)
        self.assertNotIn("question 2", out)
        self.assertIn("more chars", out)
        self.assertIn("agent still working", out)

    def test_latest_json(self):
        self.run_flow()
        data = json.loads(self.cli("latest", "--json").stdout)
        self.assertEqual(data["conversation_id"], CID)

    def test_list_and_show(self):
        self.run_flow()
        other = fixture("beforeSubmitPrompt.json")
        other["conversation_id"] = "abc12345-other"
        other["workspace_roots"] = ["/Users/you/projects/other-app"]
        other["prompt"] = "other-app question"
        self.hook(other, "beforeSubmitPrompt")
        out = self.cli("list").stdout
        self.assertLess(out.index("other-app"), out.index("my-app"))
        self.assertIn("[working] other-app", out)
        self.assertIn("[done] my-app", out)
        by_project = self.cli("show", "my-app", "--turns", "1").stdout
        self.assertIn("# Cursor chat \u00b7 my-app", by_project)
        self.assertIn("showing: last 1 of 2 turns", by_project)
        by_prefix = self.cli("show", "abc123").stdout
        self.assertIn("other-app question", by_prefix)
        events = self.cli("show", CID, "--events", "--turns", "0").stdout
        self.assertIn("### session", events)
        self.assertIn("### stop", events)

    def test_transcript_fallback(self):
        self.make_transcript()
        p = self.cli("transcript", "--turns", "0")
        self.assertEqual(p.returncode, 0, p.stderr)
        out = p.stdout
        self.assertIn("# Cursor chat \u00b7 Users-you-projects-my-app", out)
        self.assertIn("raw Cursor transcript", out)
        self.assertIn("showing: last 4 of 4 turns", out)  # merged consecutive assistant msgs
        self.assertNotIn("<timestamp>", out)
        self.assertNotIn("<user_query>", out)
        self.assertNotIn(FAKE["GITHUB_TOKEN"], out)
        self.assertIn("Add a dark mode toggle please.", out)
        self.assertIn("Reading the settings page first.\n\nDark mode toggle added.", out)
        self.assertIn("_tools: Read_", out)
        self.assertIn("Shortcut work started.", out)

    def test_transcript_project_filter_and_list(self):
        self.make_transcript("Users-you-projects-my-app", "aaa", age=600)
        self.make_transcript("Users-you-projects-other-app", "bbb")
        self.assertIn("other-app", self.cli("transcript").stdout.splitlines()[0])
        self.assertIn("my-app", self.cli("transcript", "--project", "MY-APP").stdout.splitlines()[0])
        lst = self.cli("transcript", "--list").stdout
        self.assertLess(lst.index("bbb"), lst.index("aaa"))
        missing = self.cli("transcript", "--project", "nope")
        self.assertEqual(missing.returncode, 1)

    def test_latest_without_hooks_falls_back(self):
        self.make_transcript()
        out = self.cli("latest").stdout
        self.assertIn("no hook data yet", out)
        self.assertIn("raw Cursor transcript", out)

    def test_latest_flags_newer_unrecorded_transcript(self):
        self.run_flow()
        latest_path = os.path.join(self.gc, "latest.json")
        data = self.read_json("latest.json")
        data["updated_at"] = "2026-01-01T00:00:00-05:00"
        with open(latest_path, "w") as fh:
            json.dump(data, fh)
        self.make_transcript("Users-you-projects-cli-work", "cli-chat")
        out = self.cli("latest").stdout
        self.assertIn("newer raw transcript exists", out)

    def test_show_unknown_falls_back_to_transcripts(self):
        self.make_transcript()
        p = self.cli("show", "my-app")
        self.assertIn("raw Cursor transcript", p.stdout)

    def test_empty_states(self):
        self.assertIn("no chats recorded", self.cli("list").stdout)
        self.assertEqual(self.cli("transcript").returncode, 1)
        self.assertEqual(self.cli("doctor").returncode, 1)
        self.assertEqual(self.cli().returncode, 2)


class InstallUninstall(Base):
    def sh(self, script, *args):
        return subprocess.run(["sh", os.path.join(ROOT, script)] + list(args),
                              capture_output=True, text=True, env=self.env, timeout=30)

    def hooks_path(self):
        return os.path.join(self.home, ".cursor", "hooks.json")

    def load_hooks(self):
        with open(self.hooks_path()) as fh:
            return json.load(fh)

    def backups(self):
        d = os.path.dirname(self.hooks_path())
        return sorted(n for n in os.listdir(d) if n.startswith("hooks.json.bak-"))

    def seed(self):
        os.makedirs(os.path.dirname(self.hooks_path()))
        shutil.copy(os.path.join(FIX, "existing-hooks.json"), self.hooks_path())
        return self.load_hooks()

    def test_install_merge_idempotent_uninstall(self):
        original = self.seed()
        p = self.sh("install.sh")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        cfg = self.load_hooks()
        self.assertEqual(cfg["version"], 1)
        self.assertEqual(cfg["x-team-note"], "keep me")
        self.assertEqual(cfg["hooks"]["afterFileEdit"], original["hooks"]["afterFileEdit"])
        foreign_shell = original["hooks"]["beforeShellExecution"]
        self.assertEqual(cfg["hooks"]["beforeShellExecution"][0], foreign_shell[0])
        gate_shell = [i for i in cfg["hooks"]["beforeShellExecution"] if "grok-cursor-hook" in i["command"]]
        self.assertEqual(len(gate_shell), 1)
        self.assertIs(gate_shell[0]["failClosed"], True)
        self.assertEqual(cfg["hooks"]["beforeSubmitPrompt"][0], {"command": "./hooks/audit.sh"})
        self.assertEqual(cfg["hooks"]["stop"][0], {"command": "./audit.sh", "loop_limit": 10})
        for event in ("sessionStart", "beforeSubmitPrompt", "afterAgentResponse", "stop",
                      "sessionEnd"):
            ours = [i for i in cfg["hooks"][event] if "grok-cursor-hook" in i["command"]]
            self.assertEqual(len(ours), 1, event)
            self.assertTrue(ours[0]["command"].endswith(" " + event))
            self.assertEqual(ours[0]["timeout"], 10)
            self.assertNotIn("failClosed", ours[0])
            if event == "stop":
                self.assertEqual(ours[0]["loop_limit"], 10)
        sys.path.insert(0, os.path.join(ROOT, "lib"))
        import gc_gate
        import math as _math
        gate_timeout = int(_math.ceil(gc_gate.hold_seconds(self.gc) + 5))
        for event, matcher in (("beforeShellExecution", ""), ("beforeMCPExecution", ""),
                               ("beforeReadFile", ""),
                               ("preToolUse", "Read|Write|Delete|Grep|Glob|ListDir")):
            ours = [i for i in cfg["hooks"][event] if "grok-cursor-hook" in i["command"]]
            self.assertEqual(len(ours), 1, event)
            self.assertIs(ours[0]["failClosed"], True, event)
            self.assertEqual(ours[0]["timeout"], gate_timeout, event)
            self.assertEqual(ours[0].get("matcher", ""), matcher)
        sys.path.insert(0, os.path.join(ROOT, "lib"))
        import merge_hooks
        self.assertTrue(set(merge_hooks.EVENTS) <= merge_hooks.ACCEPTED_HOOK_TYPES)
        self.assertNotIn("beforeToolCall", merge_hooks.ACCEPTED_HOOK_TYPES)
        self.assertEqual(len(self.backups()), 1)
        with open(os.path.join(os.path.dirname(self.hooks_path()), self.backups()[0])) as fh:
            self.assertEqual(json.load(fh), original)
        for f in ("grok-cursor-hook", "grok-cursor", "merge_hooks.py", "uninstall.sh"):
            self.assertTrue(os.access(os.path.join(self.gc, "bin", f), os.R_OK), f)
        # the installed gate (not the repo copy) must find the timing facts
        self.assertTrue(os.path.isfile(os.path.join(self.gc, "docs", "PROBES.md")))
        env = {k: v for k, v in os.environ.items() if k != "GROK_CURSOR_PROBES"}
        q = subprocess.run([sys.executable, "-c",
                            "import gc_gate as g; print(g.hold_seconds('/nonexistent'), g.followup_enabled())"],
                           cwd=os.path.join(self.gc, "bin"), capture_output=True, text=True, env=env)
        self.assertEqual(q.stdout.split(), ["48.0", "True"], q.stderr)

        # second install: no changes, no extra backup
        time.sleep(1.1)
        p = self.sh("install.sh")
        self.assertEqual(p.returncode, 0)
        self.assertIn("already up to date", p.stdout)
        self.assertEqual(self.load_hooks(), cfg)
        self.assertEqual(len(self.backups()), 1)

        # the registered command works exactly as Cursor would run it (cwd ~/.cursor)
        cmd = [i for i in cfg["hooks"]["beforeSubmitPrompt"]
               if "grok-cursor-hook" in i["command"]][0]["command"]
        r = subprocess.run(["sh", "-c", cmd], input=json.dumps(fixture("beforeSubmitPrompt.json")),
                           capture_output=True, text=True, env=self.env,
                           cwd=os.path.dirname(self.hooks_path()), timeout=15)
        self.assertEqual(json.loads(r.stdout), {"continue": True})
        self.assertEqual(self.read_json("latest.json")["status"], "working")
        d = subprocess.run([os.path.join(self.gc, "bin", "grok-cursor"), "doctor"],
                           capture_output=True, text=True, env=self.env)
        self.assertEqual(d.returncode, 0, d.stdout)
        self.assertIn("OK", d.stdout)

        # uninstall: back to exactly the original config, data kept
        time.sleep(1.1)
        p = self.sh("uninstall.sh")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.load_hooks(), original)
        self.assertFalse(os.path.exists(os.path.join(self.gc, "bin")))
        self.assertTrue(os.path.exists(os.path.join(self.gc, "latest.json")))
        p = self.sh("uninstall.sh")       # idempotent
        self.assertEqual(p.returncode, 0)
        self.assertEqual(self.load_hooks(), original)
        p = self.sh("uninstall.sh", "--purge")
        self.assertFalse(os.path.exists(self.gc))

    def test_fresh_install_no_existing_file(self):
        p = self.sh("install.sh")
        self.assertEqual(p.returncode, 0, p.stderr)
        cfg = self.load_hooks()
        self.assertEqual(cfg["version"], 1)
        self.assertEqual(len(cfg["hooks"]), 9)
        self.assertEqual(self.backups(), [])
        self.sh("uninstall.sh")
        self.assertEqual(self.load_hooks(), {"version": 1, "hooks": {}})

    def test_installed_uninstaller_works(self):
        self.seed()
        original = self.load_hooks()
        self.sh("install.sh")
        p = subprocess.run(["sh", os.path.join(self.gc, "bin", "uninstall.sh")],
                           capture_output=True, text=True, env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.load_hooks(), original)

    def test_invalid_hooks_json_is_not_touched(self):
        os.makedirs(os.path.dirname(self.hooks_path()))
        with open(self.hooks_path(), "w") as fh:
            fh.write('{"version": 1, "hooks": {')
        p = self.sh("install.sh")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("refusing to edit", p.stderr)
        with open(self.hooks_path()) as fh:
            self.assertEqual(fh.read(), '{"version": 1, "hooks": {')

    def test_dry_run_changes_nothing(self):
        original = self.seed()
        p = self.sh("install.sh", "--dry-run")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("[dry-run]", p.stdout)
        self.assertEqual(self.load_hooks(), original)
        self.assertFalse(os.path.exists(self.gc))
        self.assertEqual(self.backups(), [])

    def test_upgrade_replaces_stale_path(self):
        cfg = self.seed()
        cfg["hooks"]["stop"].append({"command": "/old/place/grok-cursor-hook stop"})
        with open(self.hooks_path(), "w") as fh:
            json.dump(cfg, fh)
        self.sh("install.sh")
        stops = [i["command"] for i in self.load_hooks()["hooks"]["stop"]]
        self.assertEqual(len([c for c in stops if "grok-cursor-hook" in c]), 1)
        self.assertNotIn("/old/place/grok-cursor-hook stop", stops)

    def seed_legacy_install(self):
        """Simulate an install of the old 'grok-bridge' name with recorded data."""
        cfg = self.seed()
        legacy_bin = os.path.join(self.home, ".grok-bridge", "bin")
        for event in ("sessionStart", "beforeSubmitPrompt", "afterAgentResponse", "stop",
                      "sessionEnd"):
            cfg["hooks"].setdefault(event, []).append(
                {"command": "%s/grok-bridge-hook %s" % (legacy_bin, event), "timeout": 10})
        with open(self.hooks_path(), "w") as fh:
            json.dump(cfg, fh, indent=2)
        old = os.path.join(self.home, ".grok-bridge")
        os.makedirs(os.path.join(old, "chats"))
        os.makedirs(legacy_bin)
        open(os.path.join(legacy_bin, "grok-bridge-hook"), "w").close()
        chat = os.path.join(old, "chats", CID + ".md")
        with open(chat, "w") as fh:
            fh.write("# Cursor chat: my-app\n\n<!-- gb:turn -->\n### user \u00b7 2026-01-01 09:00:00-05:00"
                     "\n\nold question\n\n<!-- gb:turn -->\n### assistant \u00b7 "
                     "2026-01-01 09:00:05-05:00\n\nold answer\n")
        entry = {"conversation_id": CID, "project": "my-app", "status": "done",
                 "updated_at": "2026-01-01T09:00:05-05:00", "chat_file": chat,
                 "workspace_roots": ["/Users/you/projects/my-app"]}
        with open(os.path.join(old, "latest.json"), "w") as fh:
            json.dump(entry, fh)
        with open(os.path.join(old, "index.json"), "w") as fh:
            json.dump({"version": 1, "conversations": [entry]}, fh)
        return old

    def test_migrates_old_grok_bridge_install(self):
        old = self.seed_legacy_install()
        with open(os.path.join(FIX, "existing-hooks.json")) as fh:
            other_hooks = json.load(fh)
        p = self.sh("install.sh")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertIn("migrated old grok-bridge data", p.stdout)
        # data moved, old dir gone, old scripts gone, paths rewritten
        self.assertFalse(os.path.exists(old))
        self.assertFalse(os.path.exists(os.path.join(self.gc, "bin", "grok-bridge-hook")))
        self.assertTrue(os.path.exists(os.path.join(self.gc, "bin", "grok-cursor-hook")))
        self.assertEqual(self.read_json("latest.json")["chat_file"],
                         os.path.join(self.gc, "chats", CID + ".md"))
        self.assertEqual(self.read_json("index.json")["conversations"][0]["chat_file"],
                         os.path.join(self.gc, "chats", CID + ".md"))
        # hooks.json: no legacy entries, exactly one of ours per event, others kept
        cfg = self.load_hooks()
        text = json.dumps(cfg)
        self.assertNotIn("grok-bridge-hook", text)
        for event in ("sessionStart", "beforeSubmitPrompt", "afterAgentResponse", "stop",
                      "sessionEnd"):
            self.assertEqual(sum("grok-cursor-hook" in i["command"] for i in cfg["hooks"][event]),
                             1, event)
        for event, items in other_hooks["hooks"].items():
            for item in items:
                self.assertIn(item, cfg["hooks"][event])
        self.assertEqual(cfg["x-team-note"], "keep me")
        self.assertEqual(len(self.backups()), 1)
        # legacy chat log (old turn marker) is still readable
        out = self.cli("latest", "--turns", "0").stdout
        self.assertIn("old question", out)
        self.assertIn("old answer", out)
        self.assertIn("showing: last 2 of 2 turns", out)
        # new turns append to the migrated log
        self.hook(fixture("beforeSubmitPrompt.json"), "beforeSubmitPrompt")
        self.assertIn("showing: last 3 of 3 turns", self.cli("latest", "--turns", "0").stdout)
        # idempotent: second run changes nothing
        time.sleep(1.1)
        before = self.load_hooks()
        p = self.sh("install.sh")
        self.assertEqual(p.returncode, 0)
        self.assertNotIn("migrated", p.stdout)
        self.assertEqual(self.load_hooks(), before)
        self.assertEqual(len(self.backups()), 1)
        # uninstall restores the user's other hooks only
        self.sh("uninstall.sh")
        self.assertEqual(self.load_hooks(), other_hooks)

    def test_migration_keeps_old_data_if_new_dir_exists(self):
        old = self.seed_legacy_install()
        os.makedirs(os.path.join(self.gc, "chats"))
        p = self.sh("install.sh")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("old grok-bridge data left", p.stdout)
        self.assertTrue(os.path.exists(os.path.join(old, "latest.json")))
        self.assertNotIn("grok-bridge-hook", json.dumps(self.load_hooks()))

    def test_uninstall_also_removes_legacy_entries(self):
        self.seed_legacy_install()
        self.sh("uninstall.sh")
        self.assertNotIn("grok-bridge-hook", json.dumps(self.load_hooks()))
        with open(os.path.join(FIX, "existing-hooks.json")) as fh:
            self.assertEqual(self.load_hooks(), json.load(fh))

    def test_doctor_flags_legacy_entries(self):
        self.sh("install.sh")
        cfg = self.load_hooks()
        cfg["hooks"]["stop"].append({"command": "/x/.grok-bridge/bin/grok-bridge-hook stop"})
        with open(self.hooks_path(), "w") as fh:
            json.dump(cfg, fh)
        out = self.cli("doctor").stdout
        self.assertIn("old grok-bridge", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
