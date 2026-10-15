"""Phase C: version tripwire, safe mode, selftest scores, fixture replay, fix-draft."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import gc_resilience  # noqa: E402
import gc_send  # noqa: E402
import test_send  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures", "dom")


def _load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return json.load(fh)


class VersionTripwire(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.events = []

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def _append(self, kind, home=None, **fields):
        self.events.append((kind, fields))

    def test_first_version_is_stored_without_an_event(self):
        change = gc_resilience.note_cursor_version(self.home, {"cursor_version": "3.24.12"}, self._append)
        self.assertIsNone(change)
        self.assertEqual(self.events, [])
        self.assertEqual(gc_resilience.read_known(self.home), "3.24.12")

    def test_same_version_is_quiet(self):
        gc_resilience.note_cursor_version(self.home, {"cursor_version": "3.24.12"}, self._append)
        change = gc_resilience.note_cursor_version(self.home, {"cursor_version": "3.24.12"}, self._append)
        self.assertIsNone(change)
        self.assertEqual(self.events, [])

    def test_a_change_emits_old_to_new(self):
        gc_resilience.note_cursor_version(self.home, {"cursor_version": "3.24.12"}, self._append)
        change = gc_resilience.note_cursor_version(self.home, {"cursor_version": "3.25.0"}, self._append)
        self.assertEqual(change, {"old": "3.24.12", "new": "3.25.0"})
        self.assertEqual(self.events, [("cursor_updated", {"old": "3.24.12", "new": "3.25.0"})])
        self.assertEqual(gc_resilience.read_known(self.home), "3.25.0")

    def test_legacy_file_counts_as_the_previous_version(self):
        with open(os.path.join(self.home, "cursor-version"), "w", encoding="utf-8") as fh:
            fh.write("3.24.11\n")
        change = gc_resilience.note_cursor_version(self.home, {"cursor_version": "3.24.12"}, self._append)
        self.assertEqual(change["old"], "3.24.11")
        self.assertEqual(change["new"], "3.24.12")


class SafeMode(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_away_non_low_becomes_high(self):
        self.assertEqual(gc_resilience.effective_risk("medium", True, "away"), "high")
        self.assertEqual(gc_resilience.effective_risk("high", True, "away"), "high")
        self.assertEqual(gc_resilience.effective_risk("low", True, "away"), "low")
        self.assertEqual(gc_resilience.effective_risk("medium", True, "present"), "medium")
        self.assertEqual(gc_resilience.effective_risk("medium", False, "away"), "medium")

    def test_flag_and_selftest_clear(self):
        gc_resilience.set_safe(self.home, True)
        self.assertTrue(gc_resilience.safe_on(self.home))
        self.assertFalse(gc_resilience.watch_cards_enabled(self.home))
        code, bad, _parts = gc_resilience.build_selftest(self.home, True, "", True, [], True)
        self.assertEqual((code, bad), (0, []))
        self.assertFalse(gc_resilience.safe_on(self.home))

    def test_degraded_and_hooks_turn_safe_on(self):
        code, bad, _parts = gc_resilience.build_selftest(
            self.home, True, "", True,
            [{"name": "selectors", "ok": False, "fatal": False, "detail": ""}], True)
        self.assertEqual(code, 40)
        self.assertEqual(bad, ["selectors"])
        self.assertTrue(gc_resilience.safe_on(self.home))
        code, bad, _parts = gc_resilience.build_selftest(self.home, False, "missing", True, [], True)
        self.assertEqual(code, 41)
        self.assertIn("registry", bad)
        self.assertTrue(gc_resilience.safe_on(self.home))


class CardStreak(unittest.TestCase):
    def test_emits_once_on_the_third_failure_and_resets(self):
        state, payload = {"streak": 0, "emitted": False}, None
        state, payload = gc_resilience.note_card_result(state, False, ["split-button"])
        state, payload = gc_resilience.note_card_result(state, False, ["split-button"])
        self.assertIsNone(payload)
        state, payload = gc_resilience.note_card_result(state, False, ["split-button"])
        self.assertEqual(payload["event"], "degraded")
        self.assertEqual(payload["selectors"], ["split-button"])
        self.assertIn("chat_id", payload)
        state, again = gc_resilience.note_card_result(state, False, ["split-button"])
        self.assertIsNone(again)
        state, payload = gc_resilience.note_card_result(state, True)
        self.assertEqual(state["streak"], 0)
        state, _p = gc_resilience.note_card_result(state, False, ["split-button"])
        state, _p = gc_resilience.note_card_result(state, False, ["split-button"])
        state, payload = gc_resilience.note_card_result(state, False, ["split-button"])
        self.assertEqual(payload["event"], "degraded")

    def test_version_event_posts_once(self):
        events = [{"kind": "cursor_updated", "old": "3.24.12", "new": "3.25.0"}]
        posts, ids = gc_resilience.event_posts(events, [])
        self.assertEqual(posts[0]["event"], "cursor_updated")
        self.assertEqual(posts[0]["snippet"], "3.24.12 -> 3.25.0")
        again, _ids = gc_resilience.event_posts(events, ids)
        self.assertEqual(again, [])


class RegistryAndGate(unittest.TestCase):
    def _cfg(self, fail_closed=True):
        import merge_hooks
        hooks = {}
        for event, _matcher in merge_hooks.GATE:
            item = {"command": "grok-cursor-hook"}
            if fail_closed:
                item["failClosed"] = True
            hooks[event] = [item]
        for event in merge_hooks.EVENTS:
            hooks[event] = [{"command": "grok-cursor-hook"}]
        return {"version": 1, "hooks": hooks}

    def test_complete_registry_passes(self):
        ok, detail = gc_resilience.registry_problems(self._cfg())
        self.assertTrue(ok, detail)

    def test_unknown_name_and_missing_fail_closed(self):
        bad = self._cfg()
        bad["hooks"]["notARealHook"] = [{"command": "grok-cursor-hook"}]
        ok, detail = gc_resilience.registry_problems(bad)
        self.assertFalse(ok)
        self.assertIn("unknown name", detail)
        weak = self._cfg(fail_closed=False)
        ok, detail = gc_resilience.registry_problems(weak)
        self.assertFalse(ok)
        self.assertIn("failClosed", detail)

    def test_synthetic_high_denies_quickly(self):
        hook = os.path.join(ROOT, "hooks", "grok-cursor-hook")
        ok, reply, elapsed = gc_resilience.synthetic_gate(hook)
        self.assertTrue(ok, (reply, elapsed))
        self.assertLess(elapsed, 1)
        self.assertEqual(reply["permission"], "deny")


class Replay(unittest.TestCase):
    def test_every_fixture_replays_through_fakecdp(self):
        names = [n for n in sorted(os.listdir(FIX)) if n.endswith(".json")]
        self.assertIn("3.24.12-run.json", names)
        self.assertIn("3.24.12-question.json", names)
        self.assertIn("3.24.12-plan.json", names)
        self.assertIn("3.24.12-edit.json", names)
        cdp = test_send.FakeCDP()
        try:
            for name in names:
                card = gc_resilience.card_from_dump(_load(name))
                self.assertIn(card["kind"], ("run", "edit", "question", "plan"))
                cdp.card = card
                result = cdp.answer({
                    "method": "Runtime.evaluate", "id": 1,
                    "params": {"expression": gc_send.cdp_js("cards", id="x")},
                })
                got = result["result"]["value"]["cards"][0]
                self.assertEqual(got["kind"], card["kind"])
                self.assertEqual(got["buttons"], card["buttons"])
                self.assertEqual(got["card_id"], card["card_id"])
                if card["kind"] == "question":
                    self.assertEqual(got["card_id"], "")
                else:
                    self.assertTrue(got["card_id"])
        finally:
            cdp.srv.close()

    def test_run_id_matches_the_hash(self):
        doc = _load("3.24.12-run.json")
        card = gc_resilience.card_from_dump(doc)
        self.assertEqual(card["buttons"], ["Skip", "Run"])
        self.assertEqual(card["card_id"], gc_send.run_card_id(doc["composer_id"], doc["command"]))

    def test_sanitize_drops_paths_and_secrets(self):
        cleaned = gc_resilience.sanitize_dump({"command": "touch /tmp/a.txt", "note": "sk-abcdefghijklmnopqrst"})
        self.assertNotIn("/tmp", cleaned["command"])
        self.assertEqual(cleaned["note"], "[secret]")

    def test_selectors_are_in_the_page_script(self):
        script = gc_send.cdp_js("selectors")
        for needle in ("composer-bar", "data-composer-status", "aislash-editor-input",
                       "composer-questionnaire-toolbar-option-label", "data-click-ready",
                       "transcript-card-footer", "split-button", "data-tool-call-id",
                       "data-tool-status", "composer-tab", "contentDocument"):
            self.assertIn(needle, script)
        self.assertNotIn(".focus()", gc_send.JS["input_shape"])

    def test_js_override_replaces_the_library(self):
        fd, path = tempfile.mkstemp(suffix=".js")
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("/* ONLY_OVERRIDE_LIB */\n")
            os.environ["GROK_CURSOR_JS_OVERRIDE"] = path
            script = gc_send.cdp_js("tabs")
            self.assertIn("ONLY_OVERRIDE_LIB", script)
            self.assertNotIn("sha256-start", script)
        finally:
            os.environ.pop("GROK_CURSOR_JS_OVERRIDE", None)
            os.remove(path)


class SelectorProbe(unittest.TestCase):
    """The live failure: a question card was on screen and selftest still exited 40."""

    LIVE_MISSING = (
        "composer-questionnaire-toolbar-option-label",
        "transcript-card-footer",
        "transcript-card-root",
        "split-button",
        "data-tool-call-id",
        "data-tool-status",
        "data-composer-status",
        ".composer-tab",
    )

    def setUp(self):
        path = os.path.join(ROOT, "tests", "fixtures", "selftest", "question-screen.json")
        with open(path, encoding="utf-8") as fh:
            self.doc = json.load(fh)

    def test_tokens_become_real_selectors(self):
        self.assertEqual(
            gc_resilience.css_selector("composer-questionnaire-toolbar-option-label"),
            ".composer-questionnaire-toolbar-option-label")
        self.assertEqual(
            gc_resilience.css_selector("transcript-card-footer"),
            '[data-component="transcript-card-footer"]')
        self.assertEqual(
            gc_resilience.css_selector("transcript-card-root"),
            '[data-component="transcript-card-root"]')
        self.assertEqual(
            gc_resilience.css_selector("split-button"),
            '[data-component="split-button"]')
        self.assertEqual(gc_resilience.css_selector("data-tool-call-id"), "[data-tool-call-id]")
        self.assertEqual(gc_resilience.css_selector("data-tool-status"), "[data-tool-status]")
        self.assertEqual(gc_resilience.css_selector("data-composer-status"), "[data-composer-status]")
        self.assertEqual(gc_resilience.css_selector(".composer-tab"), ".composer-tab")
        self.assertEqual(
            gc_resilience.css_selector(".composer-bar[data-composer-id]"),
            ".composer-bar[data-composer-id]")

    def test_question_screen_passes_and_a_quiet_window_passes(self):
        elements = self.doc["elements"]
        for token in self.LIVE_MISSING:
            self.assertFalse(gc_resilience.dom_matches(elements, token), token)
        self.assertTrue(gc_resilience.dom_matches(
            elements, ".composer-questionnaire-toolbar-option-label"))
        self.assertTrue(gc_resilience.dom_matches(elements, "[data-composer-status]"))
        part = gc_resilience.selector_part(gc_resilience.found_in_dom(elements))
        self.assertTrue(part["ok"])
        self.assertIn("question seen", part["detail"])
        self.assertIn("plan not on screen", part["detail"])
        self.assertIn("run not on screen", part["detail"])
        self.assertIn(".composer-tab not on screen", part["detail"])
        self.assertNotIn("missing", part["detail"])
        quiet = gc_resilience.selector_part(gc_resilience.found_in_dom(self.doc["quiet"]))
        self.assertTrue(quiet["ok"])
        self.assertIn("question not on screen", quiet["detail"])
        self.assertIn("plan not on screen", quiet["detail"])
        self.assertIn("run not on screen", quiet["detail"])

    def test_question_screen_clears_safe_mode(self):
        home = tempfile.mkdtemp()
        try:
            gc_resilience.set_safe(home, True)
            part = gc_resilience.selector_part(gc_resilience.found_in_dom(self.doc["elements"]))
            code, bad, _parts = gc_resilience.build_selftest(
                home, True, "", True,
                [part,
                 {"name": "cdp", "ok": True, "fatal": False, "detail": ""},
                 {"name": "scratch", "ok": True, "fatal": False, "detail": ""}],
                True)
            self.assertEqual(code, 0)
            self.assertEqual(bad, [])
            self.assertFalse(gc_resilience.safe_on(home))
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_missing_composer_still_fails(self):
        part = gc_resilience.selector_part(gc_resilience.found_in_dom([]))
        self.assertFalse(part["ok"])
        self.assertIn(".composer-bar[data-composer-id]", part["detail"])
        self.assertIn("[data-composer-status]", part["detail"])
        self.assertIn("chat input", part["detail"])


class FixDraft(unittest.TestCase):
    def test_branch_and_note_without_a_commit(self):
        repo = tempfile.mkdtemp()
        try:
            subprocess.check_call(["git", "init", "-q"], cwd=repo)
            subprocess.check_call(["git", "checkout", "-q", "-b", "send"], cwd=repo)
            with open(os.path.join(repo, "README.txt"), "w", encoding="utf-8") as fh:
                fh.write("x\n")
            author = ["git", "-c", "user.email=dev@example.com", "-c", "user.name=Dev"]
            subprocess.check_call(author + ["add", "README.txt"], cwd=repo)
            subprocess.check_call(author + ["commit", "-q", "-m", "init"], cwd=repo)
            before = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True)
            code = gc_resilience.cmd_fix_draft(repo, "9.9.9", "selector missing", ["9.9.9-run.json"])
            self.assertEqual(code, 0)
            branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=repo, text=True).strip()
            self.assertEqual(branch, "fix/cursor-9.9.9")
            after = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True)
            self.assertEqual(before, after)
            doc = os.path.join(repo, "docs", "BREAKAGE-9.9.9.md")
            with open(doc, encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn("selector missing", text)
            self.assertIn("9.9.9-run.json", text)
            self.assertIn("Do not merge", text)
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    def test_closed_port_is_degraded_not_hooks(self):
        parts = gc_resilience.collect_probe(1, [])
        names = {p["name"]: p for p in parts}
        self.assertFalse(names["cdp"]["ok"])
        self.assertFalse(names["cdp"]["fatal"])
        self.assertTrue(names["scratch"]["ok"])

    def test_shapes(self):
        self.assertTrue(gc_resilience.shapes_ok({
            "tabs": [{"text": "gc-scratch-1", "id": "abc"}],
            "cards": {"found": True, "cards": []},
            "input": {"n": 1, "text": "", "focused": False},
        }))
        self.assertFalse(gc_resilience.shapes_ok({"tabs": {}, "cards": {}, "input": []}))
        self.assertEqual(gc_resilience.EXTRA_MODULES, ("gc_card_events.py", "gc_resilience.py"))
        self.assertEqual(gc_resilience.STATE_FILES, ("known-version", "safe-mode"))
