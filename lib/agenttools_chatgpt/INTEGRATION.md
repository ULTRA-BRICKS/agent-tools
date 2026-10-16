# HA and pm-cli integration

The HA adapter targets [ha-configs-rs-bg-spomenik PR #10](https://github.com/alex-mextner/ha-configs-rs-bg-spomenik/pull/10), inspected at commit `186ffce89a1cb0dcb396e13a5897d8af938164e0`. Its launcher calls `asyncio.run(agenttools_chatgpt.ha_bridge.run(bridge))`; the supplied bridge module owns its OpenAI-compatible request/response formats, authentication middleware and HA notifier. This package supplies the browser lifecycle and optional Guardian policy. The external repository is not modified by this delivery.

## HA deployment

Install the reviewed wheel, or mount this repository's `lib` directory read-only at `/opt/agent-tools/lib` with `PYTHONPATH=/opt/agent-tools/lib`. Preserve the existing HA API key, notifier configuration, noVNC restrictions, profile volume and restart policy. Install the package's optional browser dependencies and an ordinary Chrome executable; the HA PR image already includes Playwright and aiohttp. Run only one owner against each browser profile.

The adapter HTTP listener defaults to `127.0.0.1`. If the existing HA container needs API access through its Docker network, explicitly set `CHATGPT_WEB_HOST=0.0.0.0` while retaining the API authentication and restricted port/network configuration. This does not expose browser automation or Guardian controls.

```yaml
environment:
  CHATGPT_WEB_BROWSER_MODE: manual
  CHATGPT_GUARDIAN_PROFILE_ROOT: /data/guardian-profiles
  CHATGPT_GUARDIAN_WORKSPACE: home
volumes:
  - ${AGENT_TOOLS_LIB:?set reviewed agent-tools/lib path}:/opt/agent-tools/lib:ro
```

`manual` launches ordinary headed Chrome without automation attachment. The existing HTTP app remains available: `/health` reports `manual_login` and completions return HTTP 503. Complete sign-in and human challenges yourself through the existing noVNC view. Stop the process cleanly before selecting `managed`; do not open a second Chrome process against that profile.

`managed` requires a calibrated, account-verified `CHATGPT_GUARDIAN_CONTRACT` before any explicit API prompt can modify its composer or send. Without that contract, completions return HTTP 503 `verified_contract_required` while lifecycle and health remain available. It uses the shared lifecycle, preserves restored tabs, never installs the old clipboard patch and never periodically reloads a page. New conversations that fail navigation or need human attention remain open. Tab capacity refuses requests instead of closing old conversations. Answers are extracted from rendered DOM with the bridge's markdown conversion; tool-call formatting needs live verification before production use.

The shared lifecycle owns its dedicated profile directories. It does not silently adopt `/data/profile` from the legacy bridge. Sign in manually in the new dedicated profile. `home` and `glide.vc` represent separate ChatGPT accounts and use different browser/profile ownership directories; never symlink them or reuse a legacy profile to share sessions. Keep all recovery processes that represent the same account pointed at the same `CHATGPT_GUARDIAN_STATE` database and workspace identity so an Astra quota signal stops every such session. Cross-workspace database reuse is refused.

Guardian is disabled unless `CHATGPT_GUARDIAN_ENABLED=1` in managed mode. It remains observe only unless `CHATGPT_GUARDIAN_APPLY=1`; an absent or unverified UI contract cannot authorize a send. Configure `CHATGPT_GUARDIAN_CONTRACT` to the reviewed selector contract. HA state defaults to `/data/guardian-state/<workspace>/state.sqlite`, scoped by `CHATGPT_GUARDIAN_WORKSPACE` (default `home`), so HA home stops never cross-block glide.vc. `CHATGPT_GUARDIAN_STATE` explicitly selects a shared durable file for sessions of that account. Use CLI `--workspace <same-workspace> --state <mounted-path>` before every control subcommand. Existing unscoped files, including legacy `/data/guardian.sqlite3`, require offline account reconciliation as described in the README; no silent adoption, resetting or migration occurs. Register only exact permanent chat URLs through the CLI's `watch` command. Temporary API chats, the homepage and unregistered tabs are never enrolled automatically. Closing a watched tab does not cause the adapter to reopen it.

Explicit API requests use a separate basic UI snapshot when a contract is configured, so a verified ready empty conversation can accept its first user-requested prompt without a prior failed assistant turn. This snapshot never authorizes Guardian recovery, and a homepage cannot carry Sol per-chat consent. Managed API completions honor durable manual stops, uncertain recovery intents and Astra quota even when recovery is disabled. Quota refuses new or unverified API chats; a continued chat may proceed only when its current UI independently verifies Sol, Extra High and persisted consent for that exact chat. The API checks quota both before selecting a conversation and again immediately before sending. Human drafts and running answers are preserved. API quota alerts persist the shared Astra block rather than repeatedly retrying.

The HA monitor and API completions share one asynchronous lock. A busy API completion prevents Guardian observations and recovery sends. Do not start an independent Guardian browser owner from pm-cli for this profile. Persistent controls in the same SQLite database provide watch/unwatch, manual stop/resume, Astra quota clearance and per-chat Sol consent. Explicitly opt in each Sol chat; the UI must independently verify Sol and Extra High effort.

The historical PR `RECOVERY.md` described a previous source bundle that is absent. Its token-file/init/allow-sol/control-listener examples must be reconciled with the CLI help from this package before activation; they are not proof of a deployed service. This adapter does not expose an additional network control listener. Use the package's CLI against the same local database or its separately configured authenticated local service; never publish browser CDP or service controls to the LAN.

## pm-cli integration contract

No pm-cli checkout or reliable `pm chatgpt` implementation was available for this integration. These are integration instructions, not a claim that pm-cli was changed:

1. Make `pm chatgpt` a client of the local Guardian control service, or invoke `python -m agenttools_chatgpt` with the shared durable database. Discover supported flags from that installed version's `--help` instead of copying the historical PR's commands.
2. Expose status, watch/unwatch, stop/resume, per-chat Sol opt-in and explicit quota clearance. Default to observe only. Require the exact permanent chat URL for watch and consent; neither registration nor consent is permission to switch models or effort automatically.
3. Route ordinary HA API prompts to the existing bridge. Never create a second lifecycle, copy profile files, export cookies/tokens, attach to an unrelated browser, or automate challenge completion.
4. For the separate `glide.vc` account, choose that workspace and its own profile, state database and control port. Pass that workspace on status/watch/consent commands too. Share an explicit state file only among sessions bearing the same account/workspace identity. Run a separate service owner for each profile.
5. Keep control credentials local, avoid putting them in command output, and use the authenticated loopback service contract if present. Treat HTTP errors, unknown UI state and uncertain sends as stop conditions; never retry a recovery send outside Guardian's durable reservation logic.

## Verification boundaries

`tests/test_chatgpt_guardian_ha.py` exercises restored tabs, challenge preservation, tab capacity, manual mode, navigation errors, monitor locking and refresh without reload using browser boundary fixtures. It does not log in to ChatGPT, build the HA Docker image, run OpenClaw, verify actual ChatGPT selectors, or prove markdown/tool-call fidelity. Run the HA repository's existing `test_bridge.py` and `test_launcher.py` inside its reviewed image, then perform a manual account login and a disposable observe-only session before enabling any recovery writes. Complete CAPTCHA yourself. Capture only non-secret UI evidence; never collect cookie stores, session tokens or authorization headers.
