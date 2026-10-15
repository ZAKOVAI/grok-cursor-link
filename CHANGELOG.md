# Changelog

## v0.4.0 (unreleased): resilience

- Version tripwire stores known-version and emits cursor_updated.
- selftest (0 / 40 / 41), probe --dump fixtures, safe mode, fix-draft. No auto-merge.
- --to resolves a unique workspace name. Installer copies gc_card_events.py and gc_resilience.py.

## v0.3.0 (unreleased): verified hands

Third pass:

- Background-tab sends over CDP: Cursor tabs ignore DOM `click()`, so `--to` now scrolls the tab into the strip, presses it with `Input.dispatchMouseEvent`, and waits for selection and a mounted composer (fixes exit 26). `send --back` returns to the previous tab.
- Delivery is confirmed when the chat goes busy right after Return and stays busy for 1.5 s, and `turn_ended: new_message_submitted` (Cursor closing the previous turn) no longer counts as a failure (fixes false exit 23/31).
- New `pending --to CHAT` and `approve --to CHAT --action run|skip|accept [--expect TEXT]`: read and answer approval cards, including in background tabs. Never clicks "Always …". New exit codes 32 (no card) and 33 (card ambiguous/changed).

Second pass:

- `--to` falls back to stepping with Cmd+] when a tab click has no effect. It re-reads the selected tab after every step, stops after one lap, and aborts if it can't tell which tab is selected.
- Accessibility tree walks have a time budget and skip code-editor and terminal subtrees.
- If the clipboard can't be read, it's treated as unsafe and the deep link is used instead.
- `--to` and `--new` are mutually exclusive.
- Chat titles are cleaned of control and bidi characters.
- The MCP server marks chat content as untrusted data.

- `send` now checks each step through the macOS Accessibility API (ctypes, standard library only). It focuses the chat input and verifies it's empty, inserts the prompt, verifies the prompt is in the focused input, and only then presses Return. Afterwards it confirms delivery from the hook log or transcripts (exit 23 if that can't be confirmed).
- `send --to <title|id>`: switch to an open chat tab and verify it's selected before typing.
- `send --method paste|deeplink`: paste saves and restores the clipboard and refuses to touch non-text clipboards. `--now` sends with Cmd+Return.
- `send --cdp PORT`: opt-in Chrome DevTools Protocol path, with a small standard-library websocket client.
- New commands: `targets` (reads Cursor's state DB, read-only), `ax-dump`, and `doctor --send`.
- More checks: whether this user owns the console session, frontmost verified by bundle id, the deep-link limit checked against the whole encoded URL, UTF-8 safe clipboard.
- MCP: `cursor_targets`; `cursor_send` gains `to`, `now`, `method` and `cdp_port`; protocol 2025-11-25; unknown tool returns -32602; `structuredContent.exit_code`.

## v0.2.0 (unreleased): hands

- `grok-cursor send`: prefill Cursor's chat through the official deep link, then press Return with macOS UI scripting. It runs preflight checks first (caffeinate, unlocked screen, Cursor running, Cursor frontmost), and each failed check has its own exit code.
  - Other options: `--repo`, `--new`, `--dry-run`, and `--headless` (Cursor CLI `agent -p`).
  - Refuses prompts that look like they contain a secret, unless `--allow-secrets` is given. Caps prompt length.
  - Logs every attempt to `~/.grok-cursor/sends.log`.
- `grok-cursor-mcp`: a standard-library stdio MCP server with `cursor_latest`, `cursor_list`, `cursor_show` and `cursor_send`. `cursor_send` requires `confirm: true`.
- `skills/grok-cursor/SKILL.md`: an agent skill that requires human approval for every send.
- install/uninstall now handle the MCP server and the skill.

## v0.1.0 (2026-10-08): grok-cursor-link

First public release.

- `grok-cursor-hook`: a Cursor hook for `sessionStart`, `beforeSubmitPrompt`, `afterAgentResponse`, `stop` and `sessionEnd`. It writes `~/.grok-cursor/latest.json`, `index.json` and `chats/<id>.md`. Local-only, with secret redaction and size caps. It never blocks Cursor.
- `grok-cursor` CLI with five commands:
  - `latest`, `list` and `show` read the recorded chats.
  - `transcript` is a fallback that reads Cursor's raw agent transcripts.
  - `doctor` checks the install.
- Idempotent `install.sh` / `uninstall.sh`:
  - merges into `~/.cursor/hooks.json`, keeping all other hooks
  - writes a timestamped backup before any change
  - has a `--dry-run` mode
- Migrates a pre-release install named `grok-bridge`:
  - removes the old hook entries
  - moves `~/.grok-bridge` to `~/.grok-cursor`
  - keeps old chat logs readable
