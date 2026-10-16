# grok-cursor Keep Awake — clean-room behavioral spec

Status: spec only. License: MIT (grok-cursor).
Clean-room note: written from observed *behavior* of a GPL-3.0 app (Vorssaint). No code, identifiers, strings or
file layout were copied. Only public macOS APIs/commands are named. Implementers must not open the Vorssaint source.

## 1. Behavior to replicate (user-visible)
- Master toggle. Off = "Mac follows normal energy rules". On = a keep-awake session is active.
- Duration chips: 15m, 30m, 1h, 2h, 4h, 8h, ∞ (indefinite), "Until…" (pick a clock time).
  - Clicking a chip starts a session of that length, or replaces the running one.
  - Clicking the chip of the running session again stops it.
  - A countdown is shown, plus "extend +N min".
- Options disclosure:
  - start a session at login
  - show remaining time in the menu bar
  - let the display sleep (system stays awake)
  - pause while the screen is locked
  - optional periodic tiny pointer nudge (off by default)
  - battery limit % (end the session when on battery at or below it)
- "Keep going with the lid closed" toggle:
  - A persistent preference. While on, every session also disables lid sleep; ending the session restores it.
  - Status line: "Ready. Toggles without a password" when the passwordless rule is verified; otherwise a one-time "Set up (admin password)" button.
  - Optional sub-toggle: dim the built-in display to 0 while the lid is closed, and restore its brightness on open.

## 2. States
`idle` → `active(manual|auto, end: date|nil, lid: bool)` → `paused_locked` (optional) → `idle`.
End reasons: `manual`, `timer`, `battery`, `thermal`, `cap`, `auto_idle`, `quit`, `crash_recovered`.

## 3. Mechanisms (public macOS only)
- System: `IOPMAssertionCreateWithName(kIOPMAssertionTypePreventUserIdleSystemSleep)`.
- Display (unless "let display sleep"): `kIOPMAssertionTypePreventUserIdleDisplaySleep`.
- Release with `IOPMAssertionRelease`. CLI fallback: `caffeinate -i [-d] -w <pid>`, which ties the assertion to the holder process's life.
- Lid closed: assertions cannot stop lid sleep. Use `pmset -a disablesleep 1`, verified by reading `pmset -g` for `SleepDisabled 1`. Restore with `disablesleep 0`.

## 4. Passwordless lid setup (opt-in)
- One admin prompt, via `osascript … with administrator privileges`, installs `/etc/sudoers.d/grok-cursor-awake` (mode 0440) containing exactly:
  `#<uid> ALL=(root) NOPASSWD: /usr/bin/pmset -a disablesleep 1, /usr/bin/pmset -a disablesleep 0`
- Write it to a temp file, validate with `visudo -c -f`, then move it into place. Delete it on failure.
- Verify: `sudo -n /usr/bin/pmset -a disablesleep 0` succeeds non-interactively → "Ready".
- If a set fails while the rule still looks valid, retry setup once, then show an error. Never loop.
- Uninstall removes the rule (one admin prompt). It restores sleep first if we own the flag.
- Coexistence: if another app's rule already permits these exact commands (e.g. `/etc/sudoers.d/vorssaint-clamshell`, whose rule omits `-a`), detect it with `sudo -n -l` and use that form of the command. Don't install a duplicate.

## 5. Ownership and teardown (most important)
- Persist `we_set_disablesleep=true` (with a timestamp) *before* setting 1. Clear it after a verified 0.
- Only restore `disablesleep 0` if we set it. If SleepDisabled=1 and we don't own it, report "held by another app" and never touch it.
- On start (and every 5 min from launchd), recovery: if we own the flag but no live session/holder pid exists → restore it, log `crash_recovered`, notify.
- On quit, logout or SIGTERM: release assertions, restore lid, then exit. Reboot clears the flag anyway.
- Power-source change (plug/unplug): re-verify and re-apply lid mode, since macOS can drop it.

## 6. Safety
- Battery watchdog every 30 s: on battery and ≤ limit (default 20 %) → end the session (`battery`). Low Power Mode with lid on → end it.
- Thermal: `ProcessInfo.thermalState` of serious or critical → end lid mode (`thermal`) and notify.
- Hard cap: lid sessions max 4 h (configurable, ≤ 12 h). ∞ sessions are still re-confirmed by Grok Bot every 8 h.
- Screen lock: watch `com.apple.screenIsLocked/Unlocked`. "Pause while locked" defaults OFF for grok-cursor, because agents run while locked.
  - Warn that AX/keystroke sends fail while locked. CDP is expected to keep working (UNVERIFIED — test).
- Never leave the session running silently. Every end emits an event with its reason.

## 7. grok-cursor mapping
CLI:
```
grok-cursor awake on [15m|30m|1h|2h|4h|8h|inf|--until 18:30] [--lid] [--display-sleep]
grok-cursor awake off | extend 30m | status [--json]
grok-cursor awake auto on|off [--idle 10m] [--lid]
grok-cursor awake lid setup|verify|remove
```
- Auto mode:
  - The watcher holds the assertion while busy > 0: a chat is busy from submit or first tool event until `stop`, or has an approval pending, confirmed by the CDP busy scan across windows.
  - Release after `--idle` with busy == 0.
  - An auto session never overrides a manual one, and a manual "off" suppresses auto until the next new task.
- Events: `awake_started`, `awake_ended{reason}`, `awake_lid_failed`, `awake_foreign_holder`.
- MCP tools for Grok Bot:
  - `awake_status`
  - `awake_start{duration|until, lid}`
  - `awake_stop`
  - `awake_auto{enabled, idle_min, lid}`
  - Lid on battery requires the user's explicit yes per session unless they opt into "always".
- Optional menu-bar app (SwiftUI `MenuBarExtra`): mirrors §1. It is a thin client over the CLI/daemon socket, and the daemon owns all state.
- "When done": default = release only. Opt-in `pmset sleepnow`. No shutdown.

## 8. Tests
- Parse durations and until-times, including crossing midnight.
- Chip toggle semantics.
- Ownership matrix: ours/foreign × 0/1.
- Crash recovery.
- Battery/thermal/cap triggers, using a fake power source.
- sudoers text plus a `visudo` dry run in a temp dir.
- Auto busy/idle state machine.
- No real `pmset` or sudo calls in tests.
