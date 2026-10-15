# Security

## Reporting a vulnerability

Please report it privately through GitHub's **Report a vulnerability** (Security tab of this repository). Don't open a public issue. Expect a reply within 7 days.

## What this tool can do

- **Hooks** (`~/.cursor/hooks.json`) record agent activity locally under `~/.grok-cursor/` and, in gate mode, can hold or deny shell/MCP calls. The hook always exits 0 and fails safe: errors deny high-risk work and never allow everything.
- **Send / approve / answer** drive the Cursor window over the local DevTools port (`--remote-debugging-port`, 127.0.0.1 only). Anything that can reach that port can control Cursor, so only enable it on a single-user Mac and never expose it to the network.
- **Away mode** lets an assistant decide medium-risk holds with `decide <id> <nonce>`. The nonce is per-request, random (128-bit) and single-use. High-risk allows always need the human (`--human`), and the agent being gated cannot answer its own hold.
- **Watcher** POSTs event metadata to the HTTPS URL you configure in `~/.grok-cursor/watch.env`. Command and card text are passed through a redactor first, but treat the endpoint as trusted.

## Your responsibilities

- Keep `~/.grok-cursor/watch.env` at `chmod 600`. It is never read from the repo.
- Don't put secrets in prompts. `send` refuses secret-shaped text unless you pass `--allow-secrets`.
- Review `install.sh` before running it. `./install.sh --dry-run` previews the hooks.json change; `./uninstall.sh` removes only grok-cursor entries (with a backup).

## Supported versions

Only the latest release on `main`. Cursor UI updates can break selectors; run `grok-cursor selftest` after every Cursor update (see docs/REFRESH.md).
