# Refresh the installed CLI

The installed copy in `~/.grok-cursor` does not update when you pull. Keep one checkout of github.com/ZAKOVAI/grok-cursor and install from it. From that checkout, after pulling:

    ./install.sh

That copies the CLI, the hook, and the python modules (including the card-event and resilience modules), then merges hook entries. It is safe to run again. It does not change run mode.

A watcher that is already loaded keeps the python it started with, so `./install.sh` restarts a loaded job in place (`launchctl kickstart -k`). The plist is not rewritten, so its flags (dry run, scratch) stay. Do not run `watch install` just to pick up new modules: that rewrites the flags.

The watcher's launchd label is `com.grok-cursor.watch` unless `$GROK_CURSOR_WATCH_LABEL` or `~/.grok-cursor/watch-label` names another one. Keep that file if your install predates the default, so a reinstall restarts your existing job instead of starting a second watcher. Check with `launchctl list | grep grok-cursor`: there should be exactly one.

`./install.sh` is the update step. There is no separate updater.

# After a Cursor update

Update Cursor by hand, then check that grok-cursor still works before trusting Away mode.

1. Run `grok-cursor selftest`. It clicks nothing.
   - Exit 0: hooks, gate, CDP and selectors are fine. This also clears safe mode.
   - Exit 40: selectors or CDP are degraded. Safe mode turns on. While it is on, Away treats every non-low command as high and denies it right away.
   - Exit 41: the hooks are broken. Rerun `./install.sh`, then selftest again.
2. On exit 40, capture what changed: `grok-cursor probe --to CHAT --dump` writes a sanitized DOM fixture (no file paths or secrets).
3. Run `grok-cursor fix-draft VERSION` (for example `fix-draft 3.24.12`, optionally with `--report FILE`). It creates a `fix/cursor-VERSION` branch and a `docs/BREAKAGE-VERSION.md` note. It never commits or merges.
4. Update the selectors in `lib/gc_resilience.py` (`PROBE`, `COMPONENT_NAMES`) on that branch, run the tests, and merge after review. Then run `./install.sh`.
5. Run `grok-cursor selftest` again. Exit 0 clears safe mode.

The hook records the Cursor version in `~/.grok-cursor/known-version`. A live watcher sends a `cursor_updated` ping when it changes, which is your cue to start at step 1.
