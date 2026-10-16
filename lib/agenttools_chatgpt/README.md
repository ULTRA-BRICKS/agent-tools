# ChatGPT Guardian

A reusable, observe-first Python package for controlled recovery of a current ChatGPT UI failure. It sends only the exact text `го`. It does not use private ChatGPT APIs, automate account login, solve CAPTCHA, extract cookies/tokens, install stealth patches, or attach to a home browser. Linux and macOS are supported; profile ownership uses `flock`.

The previous ChatGPT delivery bundle is absent. This package reconstructs the requested behavior from the requirements; it does not inherit the previous bundle's asserted tests or live compatibility.

## Safety contract

Recovery requires the latest unique, visible conversation turn to be an assistant turn with an active UI status exactly `Thinking failed`. The status must be outside the message body, have UI `role=status` and the verified active marker, and carry a stable turn and generation identity. Ordinary message text, older failures, hidden/detached statuses, duplicate identities, unknown UI, missing identity and unknown model labels cannot authorize sending.

The exact same complete evidence must remain observed for at least 20 seconds, with no observation gap over six seconds. Running generation pauses recovery and resets stability. Restart, changed model/effort/status/turn, missing evidence and polling gaps reset stability. Drafts including whitespace/attachments, when armed, and human challenges persist a manual stop; removing the draft or solving the challenge does not resume automation. Explicit `resume` is required.

A transaction durably reserves a unique chat/turn/generation before touching the UI. Every attempt consumes the identity and budget, even a canceled, uncertain or crashed attempt. A chat has at least 60 seconds between reservations and at most three in a rolling hour. Pending/uncertain attempts stop that chat forever until a human acknowledges the outcome with `resolve-send`; acknowledgement consumes the original identity permanently. No lease expiry or automatic resend exists. Backward wall clock movement persists a global stop.

All workers and profiles for the account must share **one SQLite database** on a local filesystem. Every observed Astra quota signal latches a durable global Astra block, including signals in restored/unregistered owned tabs. Clearing it requires explicit `quota-clear`; there is no model-switch fallback. This controls Guardian and the managed HA API, not arbitrary uninstrumented browser sessions. Sol requires exact per-chat consent plus independently verified UI labels `Sol` and `Extra High`. Generic GPT labels are never treated as aliases for Astra or Sol.

Immediately before send, Guardian reobserves all evidence and checks durable controls. A database dispatch transaction serializes stop/quota/consent writes until the browser operation conclusively finishes, including after timeout or repeated cancellation. Five seconds is the dispatch timeout, not a guarantee that cancellation or a control write completes within five seconds: cancellation never aborts JavaScript already executing in Chrome. A completed control write prevents later dispatch. Python supplies a five-second deadline before sending browser work; the browser checks it before mutation and immediately before clicking, including after synchronous input listeners. Recovery also requires exactly one visible calibrated message body, with the status outside that body. Browser/user DOM changes are not made atomic by SQLite; synchronous DOM checks bound that separate race. A subsequent unique user turn containing `го` is required to acknowledge the send; any exception, timeout or missing acknowledgement remains uncertain, with no retry. This does not provide exactly-once delivery.

A hung renderer or a transport error during a mutation fails closed by retaining the dispatch transaction indefinitely, even if cancellation repeats. Stop/quota/consent writes may time out while this quarantine holds. An uncertain operation may have clicked before the failure was detected; it is never retried automatically. Operator recovery must first close the owned Chrome process and confirm it has exited, then terminate the Guardian owner, inspect the chat and durable intent, and reconcile controls before restarting. Killing the Guardian owner first releases SQLite protection while Chrome may still execute and is unsafe. A transport error alone is not evidence that Chrome stopped. The same protection covers HA API preparation and sending; an initial API quota snapshot is persisted before busy rejection.

## Install and local operation

```sh
uv pip install './lib/agenttools_chatgpt[browser]'
chatgpt-guardian init
chatgpt-guardian --workspace home run --manual
```

`manual` opens owned ordinary Chrome without Playwright or CDP. Complete login/human checks yourself, then stop the foreground process cleanly. It owns only its exact child browser process and never kills unrelated browser processes. Manual mode supports a foreground browser executable; detached/daemonizing launchers are unsupported. A leftover Chromium lock after the launcher exits requires manually closing and inspecting that browser. Teardown attempts the context and driver independently and retains ownership on an unresolved failure. The default profile root is `~/.local/share/agent-tools/chatgpt-guardian/profiles`; `home` and `glide.vc` are different marked profile directories. Existing unowned directories, profile symlinks and leftover Chromium `SingletonLock` files are refused. Inspect crashed browser state manually; never delete a live profile lock to force startup.

```sh
chatgpt-guardian --workspace home run --chat https://chatgpt.com/c/CHAT_ID
chatgpt-guardian --workspace glide.vc run --control-port 8791 --chat https://chatgpt.com/c/OTHER_ID
```

Run is a foreground local service, suitable for systemd/launchd supervision. It reuses matching restored pages, or opens explicitly configured permanent URLs once on startup. It preserves other tabs and never periodically reloads. Observe mode navigates existing chat URLs but never submits a prompt. A redirected login/challenge page must be handled by the human. Multiple profiles use separate ports.

`--workspace home|glide.vc` identifies a separate ChatGPT account for **every** command; the default is `home`. Put it before the subcommand, including `status`, `watch`, and consent/stop commands. The old `run --workspace` position remains an alias; conflicting global/run values refuse. Defaults are `~/.local/state/agent-tools/chatgpt-guardian/<workspace>/state.sqlite`, with separate control tokens. Account-wide quota, stops, consent and budgets therefore stay within that account. For example, `chatgpt-guardian --workspace glide.vc status` and `chatgpt-guardian --workspace glide.vc watch URL` address the same database as its run command.

Use `--state PATH` **before** the subcommand, or `CHATGPT_GUARDIAN_STATE`, to share a mounted database with HA or another session of the **same account**. Each managed database records its workspace/account identity and rejects opening through the other workspace. These names are operator-assigned identities: log into the correct account in each dedicated profile; the package cannot independently identify a ChatGPT account from credentials.

Existing unscoped databases are refused, including an explicit `--state` path. An existing legacy default `.../chatgpt-guardian/state.sqlite` also blocks default startup instead of silently abandoning its stops/budgets. There is no automatic migration or adoption flag. Stop all owners, back up the database, and have its account attribution and every quota/stop/consent/attempt reconciled offline before moving to scoped state. Mixed-account history cannot safely be assigned wholesale. Preserve the original; never delete it or start empty state to evade uncertain sends. An explicit path can select an already reconciled, account-bound database while the preserved legacy backup remains.

`run --apply --contract PATH` explicitly arms recovery for the configured chats. It refuses an absent/unverified contract. Watch enrollment and Sol consent do not arm the process. `--apply` must be explicitly supplied on each process start; HTTP has no remote-enable route.

```sh
chatgpt-guardian watch https://chatgpt.com/c/CHAT_ID
chatgpt-guardian sol-opt-in https://chatgpt.com/c/CHAT_ID
chatgpt-guardian stop                       # durable global manual stop
chatgpt-guardian resume                     # does not clear quota or uncertain sends
chatgpt-guardian status
chatgpt-guardian resolve-send https://chatgpt.com/c/CHAT_ID TURN_ID GENERATION_ID
chatgpt-guardian quota-clear                # human acknowledgement of account quota state
```

`unwatch`, `sol-opt-out`, and per-chat `stop --chat URL` / `resume --chat URL` are also available. Never erase/recreate state to clear a stop: doing so loses durable dedup and budgets.

## UI calibration gate

### Observed ordinary Chrome adapter

`live` attaches to an explicitly supplied, already running **dedicated ordinary
Chrome** account profile. The operator must verify the account in that profile;
workspace names remain operator assertions. It never launches/relaunches Chrome,
reads cookies or closes Chrome on shutdown. The polling service never changes models. Do not point it at a
personal browser. Keep the same dedicated profile and scoped SQLite state across
restarts; do not clear uncertain attempts.

```sh
chatgpt-guardian --workspace glide.vc live --cdp http://127.0.0.1:9223 \
  --watch https://chatgpt.com/c/CHAT_ID --once
chatgpt-guardian --workspace glide.vc live --cdp http://127.0.0.1:9223 \
  --watch https://chatgpt.com/g/g-p-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-demo/c/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb
chatgpt-guardian --workspace glide.vc live --cdp http://127.0.0.1:9223 \
  --poll-interval 60 --max-pages 6 --min-available-mib 2048
```

The endpoint must be exactly `http://127.0.0.1:<port>`. One OS owner lock per
endpoint lives under `~/.local/share/agent-tools/chatgpt-guardian`; it covers both
workspaces. The authenticated control server uses the selected state's token and
`--control-port` (default 8790). Use the same owner root for all service instances.
Ctrl-C disconnects the driver and releases the owner lock, leaving Chrome open.
JSON reports health/status changes and a five-minute heartbeat.
Only explicitly watched pages are inspected. Repeated `--watch URL` arguments
enroll new chats; durable `/watch`, `/unwatch` and CLI watch controls are reread
each cycle. Stored watches work without startup `--watch`, and an explicit
unwatch survives restart even if its URL remains in startup arguments. Existing
project URLs are matched by canonical `/c/<id>` identity. Missing, closed or
redirected tabs stay missing across cycles and restarts. The service never opens,
navigates, reloads or closes a tab. Foreground activation is disabled by default.
Duplicate tabs fail closed.
Unrelated history/settings tabs are ignored.

When Chrome runs on an operator-managed private virtual screen, add
`live --private-display` alongside the other live arguments to allow activation
of the single eligible failed tab inside its confirmation burst. Use this option
only on that private screen: it calls `page.bring_to_front()` and can otherwise
steal desktop focus. Idle scans never activate tabs. Safety and structural checks
run before and after activation. This provides no headless or login guarantee;
the operator must provide the private display and authenticated ordinary browser.

Idle polling is passive, including with `--apply`. Only a structurally confirmed
latest `Thinking failed` on exactly one enrolled page can start a confirmation
burst when apply is enabled. Each burst observes that same page about every three
seconds for at most 35 seconds. Fresh model menu evidence is required; unavailable
proof reports `confirmation-blocked` and ends the burst. The model probe polls for
up to 1.5 seconds for a unique visible selected GPT-6 or GPT-5.6 Sol after opening
the menu/full panel; hidden or transparent selection and the generic Pro label
cannot identify a model. Probing never selects a model. The existing
Guardian requires 20 seconds of unchanged verified evidence with gaps of at most
six seconds before reserving and sending. A burst stops after its first dispatch
result, including an uncertain send; durable dedup and rate limits still apply.
There is one burst at a time, at most one candidate per passive cycle, and at least
60 seconds after a burst ends before another starts. Stops, drafts, running turns,
identity/mode changes, duplicate pages, transport errors and resource changes abort
recovery. Existing transport quarantine retains the dispatch lock on cancellation
or lost mutation transport; operator recovery may be required to end that hold.
The existing Sol selection helper is available only in a failure burst under the
existing exact Astra quota and per-chat consent checks.

`--poll-interval` defaults to 60 seconds and accepts finite values of at least 30;
the live loop always waits at least 60 seconds between passive cycles.
Model probes occur only during a failure burst. `--max-pages` defaults to 6 total
open pages and accepts only 1 through 6; more than six always suspends recovery.
`--min-available-mib` defaults to 2048 MiB of host MemAvailable.
Exceeding the page ceiling or falling below the memory floor suspends DOM reads;
unavailable memory information also suspends them. Memory uses Linux
`/proc/meminfo`; unsupported hosts fail closed. Chrome is never killed.

Existing browser `response` events latch durable global manual stop on HTTP 429
from exact `https://chatgpt.com` backend/auth paths, and HTTP 401/403 latch the
human-challenge stop. Recent (five-minute) resource performance entries seed the
same latch once per watched page attachment, if available. No private API request,
response body, headers, cookies or tokens are read. No timer clears a stop, and no
model switch bypasses it. Event listeners detach on service exit.

`--once` performs one diagnostic cycle. `python -m agenttools_chatgpt.live` accepts the same live
options (`--workspace` after the module; use the main CLI for global `--state`).

The CLI and daemon use the same state resolver. Precedence is explicit `--state`,
then `CHATGPT_GUARDIAN_STATE`, then the per-workspace deployment file
`~/.config/agent-tools/chatgpt-guardian/<workspace>.json`, then the existing scoped
default (which still refuses a legacy unscoped file). No database is moved,
renamed, deleted or migrated. An operator can point the default command at an
existing scoped database without putting its path into `pm chatgpt`:

```json
{"workspace":"glide.vc","state":"/absolute/existing/file.sqlite"}
```

Deployment JSON schema (both properties required; no additional properties):

```json
{
  "type": "object",
  "required": ["workspace", "state"],
  "additionalProperties": false,
  "properties": {
    "workspace": {"enum": ["home", "glide.vc"]},
    "state": {"type": "string", "pattern": "^/"}
  }
}
```

The workspace must match the filename and selected workspace. The configured
database must already exist and contain the Guardian schema and matching persisted
`account_scope`; malformed configuration, missing/empty databases or wrong scope
refuse startup without creating a replacement. Store's account check also remains
mandatory for explicit and environment-selected paths. `status` includes the
effective absolute `state` path alongside the same controls and attempt history.
`pm chatgpt` remains a delegator; installation/configuration is an operator step.

This adapter is calibrated to the observed 2026-10-16 account UI: unique
`data-turn-key` wrappers, exact leaf `Thinking failed` inside
`group/activity-header`, a cancelled latest attempt, an empty contenteditable
composer and no running/Stop indication. The stable turn key also identifies the
generation, deliberately allowing at most one nudge per original user turn.
The page must be at the bottom. Preparation uses synchronous native editor input;
the final check waits for the rendered Send control, retains node/URL identity,
checks a single shared deadline, then clicks synchronously. Missing user-turn
acknowledgement leaves an uncertain intent. Human drafts are never cleared or
restored. Transport loss during preparation or dispatch retains the existing
mutation quarantine and dispatch transaction.

The explicit account mapping is `GPT-6` → policy Astra and `GPT-5.6 Sol` → policy
Sol. `Pro` and the trigger's `medium` attribute alone are never model evidence.
A visible checked selected-model menu item is probed after explicitly exposing
the full model panel when its inactive panel is aria-hidden. The signature is
captured after closing the menu and observing a stable expected trigger label.
Sol additionally requires visible exact `Extra High, 4 of 5.` effort status and
closed `5.6 Extra High`, independently of the raw `max` attribute. Probe evidence
is cached briefly and reprobed before dispatch. Structural safety is checked
before any menu probe: running, drafted, stopped and completed chats never open
the model menu. Historical `in_progress` turns
do not make a cancelled latest failure running; a visible Stop still blocks.

The live polling service never selects Sol. A visible Sol quota alert durably
stops that chat. The separate diagnostic helper requires explicit per-chat Sol
consent. To grant consent:

```sh
chatgpt-guardian --workspace glide.vc sol-opt-in https://chatgpt.com/c/CHAT_ID
```

The diagnostic `live-sol` helper remains available with the live service stopped
(the same CDP owner lock applies):

```sh
chatgpt-guardian --workspace glide.vc live-sol --cdp http://127.0.0.1:9223 \
  --chat https://chatgpt.com/c/CHAT_ID
```

`live-sol` never opens/navigates a chat or sends. It requires existing watch,
per-chat consent and quota latch, no stop/unresolved intent, and an idle empty
composer with stable URL/latest-turn/node identities before each selection step.
It selects only exact `GPT-5.6 Sol`, then uses one bounded ArrowLeft key event on
focused Power only from independently visible `Pro, 5 of 5.`; already Extra High
needs no adjustment. Unknown effort refuses without substituting Pro or Medium.
The helper reopens the full model panel to independently verify model identity,
reads effort in the simple panel and closes/stabilizes the trigger. It holds the
existing SQLite guard through protected dispatch; transport loss quarantines.
A refusal after partial selection may leave Sol/its inherited effort selected;
inspect manually before retrying. Synthetic DOM keyboard handling is fixture
verified; the parent owns real UI acceptance of that event path. Restarting the
service remains observe-only unless `--apply` is explicitly supplied.
Conversation-level visible GPT-6/Astra limit alerts latch quota; unrelated
WORK/CODEX Settings percentages cannot latch Chat quota. Further account UI
calibration, including quota notice variants and attachment selectors, belongs
to live verification. Sanitized Chromium fixture tests do not establish a real
account send; the operator must verify disposable-chat recovery before arming
valuable conversations.

The generic/HA `Contract` in `browser.py` defines every required selector and identity attribute separately from the observed `live` adapter. `Contract.load` requires exactly those fields, explicit nonempty strings and a boolean `verified_account_ui`. Its DOM test contract is **fixture-only** and must not be copied as a live configuration.

Before arming, manually inspect the logged-in account and record evidence for the conversation root, turn order, unique turn ID, generation ID, author role, active UI failure status, body exclusion, composer, send control, selected model/effort, logged-in marker, generation stop, attachments, human challenge and Astra quota. Selectors must identify actual dedicated UI controls and statuses, never arbitrary prose. Do not fabricate IDs or inject attributes to satisfy this contract. If the actual UI does not expose trustworthy status/generation/model/effort evidence, recovery remains blocked; it needs a reviewed adapter change. Keep the contract local and capture only non-secret selector evidence.

## Authenticated local controls

The service binds only `127.0.0.1` (default port 8790). `init` creates a mode-0600 random control token next to the SQLite state; it prints the path, never the value. All routes, including `GET /status`, require `Authorization: Bearer <local control token>`. It is a Guardian credential, never a ChatGPT token. Mutation routes take bounded JSON: `/stop`, `/resume`, `/watch`, `/unwatch`, `/sol-opt-in`, `/sol-opt-out`, `/quota-clear`. `chat` is the exact permanent URL, or `*` for global stop/resume. There is no endpoint that enables apply or resolves uncertain sends. Avoid putting control credentials in logs, shell arguments or LAN-facing proxies.

For a local supervisor use a foreground service command such as:

```ini
[Service]
ExecStart=/absolute/venv/bin/chatgpt-guardian --workspace home run --chat https://chatgpt.com/c/CHAT_ID
Restart=on-failure
RestartSec=10
```

Restart remains observe only, resets stability, and preserves pending sends/stops/quota. Explicit `unwatch` persists across supervised restarts and is checked again inside the recovery dispatch transaction. Re-enroll with an explicit `watch` command. Explicitly review any unit that adds `--apply`. Do not launch two owners for the same profile.

## Verification

```sh
python -m pytest tests/test_chatgpt_guardian*.py
ruff check lib/agenttools_chatgpt tests/test_chatgpt_guardian*.py
ruff format --check lib/agenttools_chatgpt tests/test_chatgpt_guardian*.py
mypy --follow-untyped-imports lib/agenttools_chatgpt
```

Policy/SQLite, cross-worker race, control authentication, lifecycle ownership, CLI and HA boundary fixtures run without account login. Chromium DOM tests require the browser optional dependency and an installed `google-chrome`; they skip explicitly when absent. The actual fixture run uses intercepted local HTML and does not exercise real account selectors. No live ChatGPT send, CDP attachment, HA Docker build, production login, CAPTCHA solution or pm-cli change is claimed. See [HA/pm-cli integration](INTEGRATION.md) for the inspected HA PR and remaining activation checks.
