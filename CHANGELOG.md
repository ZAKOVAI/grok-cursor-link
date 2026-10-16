# Changelog

## v1.0.3 (2026-10-16)

- `approve` counts only cards with a live Run/Allow/Skip/Accept button; finished edit rows and plan cards no longer trigger "N approval cards; refusing to guess".
- `config.json` `dev_roots`: file edits inside a listed checkout of this project may mention the data dir or the hooks file in their content. Writes into the data dir, the hooks file, shell commands and the catastrophic list are unchanged.
- Catastrophic `rm` check follows a leading `cd` instead of discarding the cwd, and a relative non-recursive `rm` of one named file is never catastrophic (held in-repo edit + temp-file cleanup, decision 64ac0b45).
- VERSION 1.0.3 in the CLI and the hook.
- Verified on Cursor 3.25.6: doctor OK, selftest exit 0 (hooks, gate, CDP, selectors).

## v1.0.2 (2026-10-16): durable delivery, never hold a typed turn

- Watcher delivery is durable: every ping is written to `~/.grok-cursor/queue/<event id>.json` before any network call and deleted only after a 2xx. Retries use exponential backoff with jitter (5 s doubling to 300 s). 400/408/425/429, 5xx and network errors are all retryable (the webhook reports its own rate limit as a 400). Same event id = same file, so nothing is duplicated. Nothing is ever dropped. `delivery.json` keeps last success / last error.
- Local fallback: when nothing has been delivered for 2 minutes, each queued hold or finish raises one macOS notification (`osascript`), so the human still sees it with the webhook down. `GROK_CURSOR_NOTIFY=0` turns it off.
- `grok-cursor watch status` shows the last success, the last error, queue depth, and the oldest undelivered event.
- Gate: a chat with no readable turn record is treated as human-typed and logged `prompt_origin_unknown`; only the catastrophic list is held. The gate holds only turns it can prove came from `grok-cursor send` or its own resume message. The pre-1.0.1 turn-file name is read as a fallback, so an upgrade mid-turn keeps the human's turn.
- Regression test replays the 12:10 ET 2026-10-16 case where a typed turn was held after an upgrade renamed the turn files.

## v1.0.0 (2026-10-16): one repo

- Human-turn allow lasts for the whole running turn the human's prompt started (no time limit until `stop`); the 30 min Away / 6 h Present window only counts after `stop`.
- Watcher logs the webhook's reason on a 4xx.
- This repo is the single source of truth (the old private development repo is archived).
- Watch label: `$GROK_CURSOR_WATCH_LABEL`, then `~/.grok-cursor/watch-label`, then `com.grok-cursor.watch`; install, uninstall and `watch` agree, so a reinstall never starts a second watcher.
- Audit W2: watch-state.json forgets request ids whose decision and outbox files are gone.
- Audit W3: `GROK_CURSOR_REPING_NONCE=0` drops the nonce from re-pings (default keeps it so a webhook that only sees a re-ping can still decide).
- Audit W4: plist uses `KeepAlive: {SuccessfulExit: false}`, and a missing/invalid webhook config waits 300 s before exiting, so no 10 s respawn loop.
- Audit I3: `tests/run.sh` always runs `sh -n`; `SHELLCHECK=require` fails when shellcheck is missing; `tests/test_lint.py`.
- Audit P1: README MCP snippet no longer shows a macOS-only path.

## v0.4.0 (unreleased): resilience

- Version tripwire stores known-version and emits cursor_updated.
- selftest (0 / 40 / 41), probe --dump fixtures, safe mode, fix-draft. No auto-merge.
- --to resolves a unique workspace name. Installer copies gc_card_events.py and gc_resilience.py.
- Installer copies docs/PROBES.md to ~/.grok-cursor/docs so the installed gate uses the measured hold (48 s) and stop-hook resume. Before this the installed gate fell back to 60 s with resume off.
- Human-typed turns: commands in a chat whose latest prompt the human typed are allowed without a hold (medium and high, both modes), except a short catastrophic list (gc_risk.catastrophic). grok-cursor send marks its prompts via send-pending.json. Events human_turn_allow / human_turn_held / prompt_origin.
- Human-turn window: 30 min in Away mode (6 h in Present). Cloud/data-destroy commands (gh repo delete, terraform/pulumi destroy, supabase db reset, railway down/delete, vercel remove, dropdb, DROP DATABASE/TABLE, ...) are high and on the catastrophic list.
- Watcher re-POSTs an undecided decision_requested at most 3 times (after 60 s, 120 s, 300 s), stops early when it is decided, expired or gone; state persists in watch-state.json. The first ping and new req_ids are never suppressed.

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
