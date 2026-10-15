# Measured timings

`lib/gc_gate.py` reads this file (or `$GROK_CURSOR_PROBES`) to size the hold for a gated command. Without it the hold falls back to 60 s.

- **Hook ceiling:** Cursor waited the full 90 s without killing a hook with `timeout: 120`. Ceiling ≥ 90 s.
- **2b: yes.** After a deny ends the turn, Cursor fires the `stop` hook and auto-submits its `followup_message`, so an expired hold can be resumed once approved.
- **Assistant round trip (Away mode decide):** 10 trials, p95 = 17.72 s. Hold = p95 + 30, rounded up = 48 s, under the ceiling.

Re-measure on your own setup and edit the two numbers above if your assistant answers slower. Set `"hold"` in `~/.grok-cursor/config.json` to override.
