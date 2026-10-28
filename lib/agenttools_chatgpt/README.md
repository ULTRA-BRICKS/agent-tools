# ChatGPT Guardian

> Since version 0.3.9, coverage keeps findings in local diagnostics and never sends Telegram messages. The repair coordinator owns exceptional requests for owner action. The supported `notification_mode` is `coordinator-only`; see [Notification policy](NOTIFICATION-POLICY.md) for the current contract.

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
Model probes occur only during a failure burst. `--max-pages` is the conversation-tab
budget: it defaults to 6 and accepts only 1 through 6; more conversation tabs always
pause mutation. A conversation tab is any open page showing
`https://chatgpt.com/c/<id>` or `/g/g-p-<id>-<slug>/c/<id>` (trailing slash, query and
fragment ignored); a duplicate tab of one conversation counts and also vetoes that chat.
Every other open page is auxiliary (`/space?tab=all`, a new tab, other chatgpt.com pages,
any external site) and counts against a fixed auxiliary budget of 2. Over either budget
mutation (sends, Sol selection, tab restore) is paused with the specific reason
`conversation-budget` or `auxiliary-budget`. `--min-available-mib` defaults to
2048 MiB of host MemAvailable; below it, or with unavailable memory information, mutation is
paused as `low-memory` or `memory-unavailable`. The live loop suspends its own DOM reads
under any pause; the read-only observer does not (see MONITORING.md). Memory uses Linux
`/proc/meminfo`; unsupported hosts fail closed. Chrome is never killed.

Existing browser `response` events latch durable global manual stop on HTTP 429
from exact `https://chatgpt.com` backend/auth paths, and HTTP 401/403 latch the
human-challenge stop. Recent (five-minute) resource performance entries seed the
same latch once per watched page attachment, if available. No private API request,
response body, headers, cookies or tokens are read. No timer clears a stop, and no
model switch bypasses it. The in-process hold is read from the durable `stop:*`
control, so only an explicit `resume` releases it (the running service resumes at once
without a restart) and a new 429/401/403 latches again, but never over an existing owner,
challenge or quota `stop:*`: it is written with `INSERT OR IGNORE`, so a human pause is
preserved (0.3.14). Under `live --single-tab` the patrol
may also release the exact machine 429 hold, but only on verified same-endpoint transport
evidence (see "Single-tab patrol"), never on a timer. Event listeners detach on
service exit.

A visible dialog is a human challenge only on positive login/CAPTCHA evidence (CAPTCHA
frame, password field, login/sign-up heading or button, `/auth/login` link, signed-out
page). The ChatGPT Activity panel (heading `Activity` with Subagents/Scheduled/Outputs)
is benign. Any other dialog is unknown: sends are refused while it is open, but no
durable human-challenge stop is latched. `live` cycle reports and monitoring carry
diagnostic `evidence` codes such as `failure:collapsed-button`, `dialog:unknown` or
`challenge:auth-dialog`; never message text.

`--once` performs one diagnostic cycle. `python -m agenttools_chatgpt.live` accepts the same live
options (`--workspace` after the module; use the main CLI for global `--state`).

The CLI and daemon use the same state resolver. Precedence is explicit `--state`,
then `CHATGPT_GUARDIAN_STATE`, then the per-workspace deployment file
`~/.config/agent-tools/chatgpt-guardian/<workspace>.json`, then the existing scoped
default (which still refuses a legacy unscoped file). No database is moved,
renamed, deleted or migrated. An operator can point the default command at an
existing scoped database without putting its path into `pm chatgpt`:

```json
{ "workspace": "glide.vc", "state": "/absolute/existing/file.sqlite" }
```

Deployment JSON schema (both properties required; no additional properties):

```json
{
  "type": "object",
  "required": ["workspace", "state"],
  "additionalProperties": false,
  "properties": {
    "workspace": { "enum": ["home", "glide.vc"] },
    "state": { "type": "string", "pattern": "^/" }
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

This adapter is calibrated to the observed ChatGPT UI: unique
`data-turn-key` wrappers, a cancelled latest attempt whose exact leaf `Thinking failed`
status is a visible SPAN inside either the legacy `group/activity-header` or the current
thinking `button[aria-expanded=false|true]` (outside user bubbles, markdown and
sub-agent output; quoted text in older or markdown content never counts; two status
nodes are ambiguous), an empty contenteditable
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

## Owner-approved continuation (`POST /continue`)

A _complete_ answer is not a _complete task_: a model can stop at its own turn limit or a tool quota and still report the turn as complete. The automatic recovery path never touches such a turn (it needs a verified `Thinking failed`). An owner can instead approve **one** `go` for **one exact turn**; the local trusted caller (pm, after the owner's Telegram tap) then posts that approval to the account-local control API of the Guardian that owns the chat.

```http
POST /continue          Authorization: Bearer <that account's control token>
{"request_id":"<lowercase UUID>","chat":"https://chatgpt.com/c/<id>","expected_turn":"<turn UUID>","approved_at":<unix>,"expires_at":<unix, <= approved_at+900>}

202 {"ok":true,"request_id":"…","status":"queued"}      # idempotent replay returns the current status
409 {"ok":false,"request_id":"…","status":"refused","reason":"…"}   # recorded refusal, see below
409 {"ok":false,"error":"request-id-conflict"}           # same id, different payload; nothing changes
409 {"ok":false,"error":"continuations-unavailable"}     # service is not a live `--apply` loop
400 invalid body · 401 missing/wrong token · 429 queue-full (8 waiting approvals)

GET /continuations/<request_id>   -> {"ok":true,"request_id","status","reason","chat","expected_turn","approved_at","expires_at","queued_at","updated_at","sent_at","observed":{"model","effort","turn_state"}|null,"source":"operator"}
```

The body is exactly those five fields; any extra key (including a caller-supplied `approved`) is a `400`. The route is not reachable without the account's bearer token, and the token is a Guardian credential no MCP tool exposes. The handler only validates and **queues**; it never touches the browser. Queue-time refusals are stored as `refused` receipts: `approval-expired`, `approval-in-future` (more than 30 s ahead), `not-enrolled` (the chat is not watched by _this_ account's store, which is also how a cross-account request fails) and `turn-already-requested` (a queued/sent/uncertain request already owns that chat+turn; refused requests never do).

`status` is one of `queued` (`reason: dispatching` while the click is in flight), `refused`, `sent` (the exact go bubble was seen, `reason: acknowledged`) or `uncertain` (`unacknowledged`, `interrupted`, `restart-interrupted`). `uncertain` is final: a restart, replay or fresh approval never clicks again, and the matching attempt keeps the chat blocked until a human runs `resolve-send`.

The live `--apply` loop is the **only** sender. Each cycle it expires stale approvals and then handles at most one queued approval, inside the same one-mutation-burst budget as failed-turn recovery. It requires one unique open enrolled page and re-reads the DOM, then re-reads it again immediately before the click. It refuses (terminally, with a receipt reason) on: expired/future approval, missing or duplicate page, resource pause or transport latch, an observation that is unverified, another chat or **another latest turn**, a running/Stop state, draft or attachment, challenge, ambiguous page or dialog, any quota (the Astra latch counts for every model), unknown model/effort (Astra, or Sol Extra High with the existing per-chat consent), global or chat stop, unwatched chat, an unresolved reserved/uncertain attempt, an existing attempt for that turn, the 60 s cooldown and the 3-per-hour limit. The latest turn may be `complete`, `cancelled` or have an unknown state marker; none is relabelled `Thinking failed`. The text is the exact Cyrillic `го`, no model is ever switched, nothing is navigated, and a failure after the click is invoked is `uncertain`, never retried.

A reservation, the attempt row and the receipt share the durable SQLite store (`continuations` table with a partial unique index on the owning chat+turn), so duplicate callers, restarts and crashes cannot produce a second click. Receipts hold only metadata: no message bodies, page text or tokens. There is no optional source-text diagnostic; if one is ever added it must be bounded and metadata-only by default.

The send is a normal attempt: monitoring labels it `last_go_source: operator` and correlates the following assistant reply exactly like an automatic send. A completed reply only means that _turn_ finished.

**View preparation.** A conversation scrolled away from its end reads as unobserved (the ordinary scroll-gap safety check is unchanged). Only when the first observation of an approved continuation has no readable turn, the dispatch calls the adapter's `prepare-view` once: after verifying the exact chat, the approved turn as latest turn, a non-generating state, no draft/attachment/challenge/quota/ambiguity and a present profile menu, it scrolls the conversation (and page) to its end — no focus, click, navigation, tab or model selection — and re-checks the page. `False` refuses the request as `view-not-ready` without a reservation. The normal observation, probe and pre-click re-read then run afresh.

**Approval-bounded click.** The live loop caps every browser mutation by `expires_at`: the mutation window (`allowed()`), the adapter (`approval_expires_at`) and the in-page deadline all stop at expiry, before typing and before the click. An approval that expires during the model probe sends nothing; the attempt is then `uncertain` and never repeated.

**Canceled attempts.** A refusal after reservation (changed page, stale observation, interrupt before send) keeps the attempt as `canceled`: it consumed the turn identity and the cooldown/hourly/dedup budget, so a new request id cannot retry that turn. A `canceled` attempt does not block the chat as an unresolved send (only `reserved` and `uncertain` do) and is not reported in telemetry because nothing was clicked.

## Browser restart restore (`startup`, 0.3.3)

`python -m agenttools_chatgpt.startup --workspace home|glide.vc [--cdp-port PORT] [--apply]` reopens enrolled
(watched: `controls` rows `watch:<chat>` = `1`; an unwatch `0` or a consent-only chat is not enrolled)
canonical chats that are missing from the account's dedicated browser (home port 9224, glide.vc
port 9223 unless `--cdp-port` names the port the browser really listens on, profile `~/.local/share/agent-tools/chatgpt-guardian/profiles/<workspace>`). Default is a
dry run; `--apply` authorizes opening new tabs only. It reads the existing account-validated state
database with `mode=ro` (never created), verifies from `/proc` (executable must be chrome/chromium; Chrome's rewritten single-string
command line is split safely) that exactly one browser owns that
profile and port and is the only loopback listener, uses loopback HTTP `GET /json/list` and
`PUT /json/new?<encoded url>` without redirects or proxies, bounds bodies (1 MiB) and the whole run
(30 s), waits for the browser to listen and for the page inventory to settle, and re-reads the
inventory before every creation. Existing pages are never navigated, reloaded, activated or closed;
no message is sent and no control, model or quota state changes. Project URLs count as the same chat.
It refuses on another account, missing database, unowned or duplicate browser, duplicate tabs for a
chat, or the shared page budget (6 conversation tabs plus at most 2 auxiliary tabs) or memory guard refusing one more conversation tab. A creation that times out is never retried in the same run: the output
reports `uncertain` and a rerun checks the inventory first. Output is metadata JSON only.

Unit drop-in templates live in `systemd/chatgpt-guardian-{home,glide}-browser.service.d/lifecycle.conf.template`
(`Restart=always`, `RestartMode=direct`, `ExecStartPost=-<venv python> -m agenttools_chatgpt.startup ... --apply`).
Render every `@TOKEN@` (`@VENV_PYTHON@` and `@CDP_PORT@`, the browser unit's `--remote-debugging-port`; a template with an
unrendered token makes `startup` exit 2 and, because of the `-` prefix, silently skip the restore), copy to
`~/.config/systemd/user/<unit>.service.d/lifecycle.conf` and run `systemctl --user daemon-reload`. `startup --cdp-port`
only tells `startup` where the browser listens; the browser unit, `live --cdp` and `observer --cdp` must name the same port
(a host whose 9224 is taken by another tool moves all three, e.g. to 9226). Direct restarts do not stop `BindsTo=`
dependents, and an explicit `systemctl --user stop` still keeps the unit stopped. The `-` prefix keeps a restore failure
from killing the browser.

## Coverage reconciliation (`coverage`, 0.3.5)

`chatgpt-guardian --workspace home|glide.vc coverage --cdp-port PORT [--apply]` answers one question: is every
ACTIVE chat of the account (a running or `Thinking failed` chat open in the owned browser, including newly opened
ones; not every historical dialog) genuinely supervised? `--cdp-port` has no default: it must equal the browser's
`--remote-debugging-port` (e.g. home 9226, glide.vc 9223). Evidence is only the persisted monitor snapshot, the recovery
database (read-only) and the owned browser's loopback DevTools HTTP inventory after `startup.verify_browser`; there is no
new CDP observer. `--help` and the default dry run write, start and send nothing and never create a file.

The JSON report keeps the facts apart per chat: `enrolled` (a `watch:<chat>` control of `1`), `present` (exactly one
open tab in the verified browser), `fresh_chat` (the chat's OWN observation is current and unique; a fresh snapshot
alone does not make a chat fresh), `activity` (`running`, `failed`, `rate-limited`, `finished`, `idle`, `unknown`; `finished`
needs a current complete status, an old `completion_observed_at` is history and never counts), `active` and
`recovery_eligible` (service active, browser verified, enrolled, present, fresh and no hold, quota or unresolved send;
mirrored as `eligibility_for_probe` with `model_proof: "unknown"`: a probe-level check, not validated model/send). `summary` counts them and lists
`uncovered` visible chats (finished or unknown ones are listed, never enrolled, never sent to).

Per-account flags live in `~/.config/agent-tools/chatgpt-guardian/<workspace>.coverage.json` (override: `--config`
or `CHATGPT_GUARDIAN_COVERAGE_CONFIG`), for example
`{"workspace": "home", "coverage_enabled": true, "auto_watch_active": true, "recovery_autostart": false, "notification_mode": "coordinator-only"}`.
Missing boolean flags default to `false`; the notification mode defaults to `coordinator-only` and other mode
values are rejected. An invalid file refuses (exit 2) with no effects. With `--apply` and `coverage_enabled`
the only effects are:

- `auto_watch_active`: enrol a chat that is fresh, unique in both the snapshot and the browser, running or
  `Thinking failed`, and has NO `watch:` key (`INSERT OR IGNORE`). A manual `unwatch` (`0`) is never undone, and holds,
  stops, quota, uncertain sends and models are never touched.
- Restore: missing ENROLLED chats are reopened through `startup.restore` (the default page budgets and memory guard, 6 conversation + 2 auxiliary tabs and 2048 MiB; no navigation, reload or close of
  existing tabs). The durable ledger `<state-stem>.coverage.sqlite` (next to the state database, mode 0600) allows ONE
  attempt per missing chat per inventory (browser pid plus the set of enrolled chats open exactly once, computed
  identically before and after a restore and including chats enrolled by the same run; blank or login tabs left by
  a failed restore and duplicated chats do not change it) and at least 15 minutes between attempts, so redirected or
  failed restores never accumulate blank tabs. Missing account, ambiguous or unowned browser, duplicate tabs and an exhausted page budget or memory guard are
  reported as `blocked`. `--reset-restore --apply` is the owner action that forgets spent attempts.
- `recovery_autostart` (default `false`): start (`systemctl --user start --no-block`) a recovery service whose
  ActiveState is `failed`, never an `inactive` (manually stopped) one, only after the browser is verified, never with a
  global `stop:*`, at most 3 attempts 5 minutes apart per failure (reset when the service is seen active). The browser is
  never restarted.
- Local diagnostics: failed services, missing or duplicate pages, stale observations, uncovered active chats,
  stalled current generation and existing global holds remain available in the JSON `diagnostics` list.
  `plan.alerts` and `effects.alerts` remain empty. Inspect the report with the same `coverage --cdp-port PORT`
  command without `--apply`, or inspect the existing `chatgpt-guardian-<slug>-coverage.service` user journal.
  The repair coordinator alone decides whether verified exhaustion and an unavoidable owner action justify
  contacting the owner. Coverage never invokes Telegram, retries old delivery attempts, rearms notifications,
  or prunes their historical evidence. Legacy alert data is retained without becoming a dependency of safe repair;
  the scope and restore/autostart records keep their existing validation.
- Serialisation: an applied run (`--apply` with `coverage_enabled`) holds a non-blocking `flock` on
  `<state-stem>.coverage.lock` for the whole run, released on any error; a second concurrent run reports
  `"busy": true`, exit 0, and changes nothing (`--reset-restore` while busy is blocked, exit 1, instead). `startup
--apply` takes the same lock (waits up to 60 seconds), so the
  browser unit's own restore and the timer can never both open the same missing chat. Dry runs never lock or write.
  The restore reservation is a check-and-set in one transaction and is never replaced by another caller. The ledger
  schema and its scope row are created in a single transaction; an empty or scope-less empty ledger is initialised,
  while a foreign scope, unrecognised tables or records without a scope are refused and never deleted or adopted.
  The report has the same sections on every outcome, including early failures.

Exit status is 1 when anything is `blocked`. Timer and service templates live in
`systemd/chatgpt-guardian-coverage/coverage.{service,timer}.template`: render `@VENV_PYTHON@`, `@WORKSPACE@`,
`@CDP_PORT@` (the service) and `@SLUG@` (`home` or `glide`, the timer), install as
`~/.config/systemd/user/chatgpt-guardian-<slug>-coverage.{service,timer}` and run
`systemctl --user daemon-reload && systemctl --user enable --now chatgpt-guardian-<slug>-coverage.timer`. The timer
fires every 60 seconds; the service always passes `--apply` and the flags file decides what may happen.

## Single-tab patrol (`live --single-tab`, 0.3.13)

`chatgpt-guardian --workspace home|glide.vc live --cdp http://127.0.0.1:PORT --apply --single-tab` runs the account
with ONE owned conversation tab instead of one tab per chat. It is a mode of the same `live` owner, not another
daemon: the same CDP owner lock, recovery database, idempotency/attempt ledger, control endpoint, transport latch,
sender, owner-approved continuation ledger and dedicated profile. Both modes take the owner lock, so they cannot run
side by side. A legacy `live` launch (without `--single-tab`) never touches the single-tab marker on its own: while the
mode is recorded it is refused, and only the explicit rollback below retires it. Nothing scans conversation history,
calls an API for chat lists or opens a second browser.

The patrol opens and settles its rotation file (interrupted sends, overdue due time) only after it holds BOTH the owner
lock and the account lock (0.3.14): a second launch that loses either lock refuses before touching the rotation file,
so it can never rewrite the running patrol's in-flight Go record.

Known limitations, stated plainly: navigating away from an ACTIVE response has no permitted live proof, so the patrol
never does it (see "Running responses"); a machine 429 hold is released only by verified same-endpoint transport
evidence and otherwise stays pending for the owner or an authorized coordinator (see "429 and cooldown"); a Go receipt
is not a confirmed continuation; not every enrolled chat is sent to, only a verified structural failure is.

Settings (all flags of `live`; the rotation flags are refused without `--single-tab`):

| Flag                                                                                    | Default    | Meaning                                                                                                              |
| --------------------------------------------------------------------------------------- | ---------- | -------------------------------------------------------------------------------------------------------------------- |
| `--single-tab`                                                                          | off        | Enable the patrol for this account.                                                                                  |
| `--rotation-min` / `--rotation-max`                                                     | 240 / 360  | Seconds BETWEEN VISIT STARTS; one fresh uniform draw per visit. Safe range 120 to 900, min <= max.                   |
| `--confirm-wait`                                                                        | 45         | In-visit wait (20 to 60 s) for the assistant response that follows a Go.                                             |
| `--backoff-base` / `--backoff-cap`                                                      | 600 / 3600 | Seconds of the first 429 pause and the longest adaptive pause (300 to 3600 / base to 86400), see "429 and cooldown". |
| `--retire-single-tab`                                                                   | off        | Deliberate rollback to the legacy loop; only while holding the account lock (see below).                             |
| `--poll-interval`, `--max-pages`, `--min-available-mib`, `--private-display`, `--apply` | as before  | Housekeeping tick (30 s minimum, default 60), resource guard, display handling and apply, unchanged.                 |

**Rotation state** (`<state-stem>.rotation.json`, next to the account's database, mode 0600, stdlib-only modules
`rotation.py`, `rotation_evidence.py`, `rotation_stats.py`, `backoff.py`) persists the queue (with each chat's durable
`retry` record), cursor, current target, next due time, the start of the last visit, any explicit deferral, last Go, any
cooldown, the adaptive pressure and its settings, the 429 transport evidence and the owned URLs. Every write is atomic
(temp file, fsync, replace). Schema and account are validated on every load: a wrong-account, corrupt or symlinked file
refuses to start the service (exit 2) and is never overwritten. Schema 1 (0.3.13/0.3.14) is migrated losslessly in
memory (queue order, cursor, visit counts, Go record, owned URLs and evidence are copied unchanged, pressure starts at
zero) and rewritten as schema 2 only when the patrol opens it; `status` and `monitor` never write. A schema 2 file is not
readable by 0.3.14. Each account draws from its own system-entropy generator.

**The interval is between visit starts.** One draw per visit is made when the visit STARTS (before the navigation, and
persisted) and the next visit is due exactly that draw later, so the 20 to 60 s in-visit confirmation is INSIDE the
240 to 360 s, never added to it: a 55 s confirmation with a 360 s draw is a 360 s gap between starts, not 415 s. The
measured gap is the draw plus sub-second scheduler latency (the loop wakes at the due instant with a 0.25 s floor). A
restart in the middle of a visit keeps its anchor. A genuine overrun (the visit, or a cooldown, outlasts its draw, a due
time already past after downtime, an implausible clock) is never a catch-up burst and never due at once: it is an
EXPLICIT persisted `deferred` state (`reason`: `overrun`, `overdue`, `clock`, `cooldown`, `running-protected`,
`protected:*`; visible in telemetry) scheduled as one fresh draw from now (from the end of a running cooldown), cleared
when the next visit starts. A first start also waits one full interval. The two accounts do not synchronise.

**Queue.** Enrolled chats (`watch:<chat>` = `1`) join the queue in canonical order and stay in it; the full permanent URL
(project path included, query and fragment dropped) is stored next to the canonical identity, taken from `--watch`, an
open tab or an earlier entry, never from a history scan. Chats under a per-chat or global `stop:` are skipped and keep
their pause. The chat of the oldest unexpired owner approval jumps the queue once without moving the cursor, so every
other chat is still visited once per lap. A chat without an open tab is expected queued state, not an observation failure.

**A visit** (only at the due time): navigate the one owned tab to the next chat (no new tab is opened while an owned tab
exists), read only that conversation (passive adapter plus the monitor's metadata DOM read) and classify it
`working`, `complete`, `rate-limited`, `challenge`, `draft`, `unreadable:*`, `redirected`, `nav-error:*` or a failure.
Navigation clicks nothing: no Stop, no model menu, no Send. Only a verified structural failure (newest, uniquely keyed,
cancelled turn with exactly one `Thinking failed` status, as in the legacy recovery) may receive a Go; a completed final
answer, a running response and anything uncertain never do, and no completion verdict is invented.

**Go admission** is the existing Guardian path and all of it must hold: the account's own recovery database and rotation
file, the exact visited chat and head (stable for 20 s, re-read immediately before the click), an empty composer, no running
response, no hold or stop, no unresolved or deduplicated attempt for that turn, a verified model (Astra, or Sol Extra High
with consent), no quota latch, and a rendered, unique tab in the resource budget. The patrol never selects a model or
effort, never switches a branch, never edits or deletes a message, never forges a tool approval and never clicks Stop.
After a click the bounded `--confirm-wait` correlates the Go with the NEXT assistant response (running or complete); a reply
that arrives later is correlated on the chat's next visit. The Go record is persisted before the click, so a crash
leaves `interrupted`, the attempt stays unresolved in the recovery database, and a restart never repeats it.

**One tab and consolidation.** Guardian-owned means "a conversation tab whose chat is in the queue or in the persisted owned
URLs"; login pages, `/space`, blank pages and other conversations are never read, navigated or closed. When several owned
tabs exist the patrol records ALL their URLs first and then closes only extras that read as idle (empty composer, not
running, no challenge, no unresolved send, readable); any other extra is reported in `tabs.protected` and left open. A
nonempty draft, an authentication screen or any sign-in page pauses the patrol (`protected-tab`, `login-flow`) instead of
opening a second tab, and the draft is neither read nor erased.

**429 and cooldown (adaptive backoff, 0.3.15).** A transport 429 (the legacy `stop:*` machine hold) writes a durable
cooldown and records the exact endpoint path (no query) of every 429 and, for a restricted endpoint only, every later
2xx of that same path, as the browser's own `response` events showed them (never DOM text, never a request of our own).
The cooldown deadline is `now + max(adaptive, Retry-After)` and is the ONE next retry deadline (never a second
interval after it; the ordinary minimum spacing only applies when it is longer). `adaptive` is drawn uniformly in
`[ladder, ladder * 1.25]` with `ladder = base * 2^(level-1)` (600, 1200, 2400, 3600 s with the defaults `base` 600 and
`cap` 3600), shifted to `[cap / 1.25, cap]` = 2880 to 3600 s at the cap so the jitter survives there. `Retry-After` is read as delta seconds or an HTTP date and is NEVER shortened or capped: a 15 s header
cannot shorten the 10 minute ladder, a 3 day header stays 3 days. A burst of 429s (one page load fires dozens) inside one
issued visit or probe is ONE failure episode and one level; a 429 outside our own cooldown after 120 quiet seconds is a
new episode. Pressure level rises by one per episode up to the cap and stays there indefinitely (no retry limit, no
drop); it falls by ONE level after 3 clean visits. A clean visit needs a fresh 2xx of that chat's own read endpoint in the
visit (a DOM outcome alone is never evidence; a visit without it is neutral and changes nothing), and no 429 or 5xx. An
unrelated 200 (avatar, list, another chat) never lowers it. Under pressure the spacing of ordinary visits is at least
the ladder (`max(base draw, adaptive)`), so recovery is gradual; at level 0 the interval is exactly the 240 to 360 s
draw. A late burst never moves the deadline again. Until the
cooldown ends nothing is read, navigated or sent for that account (the other account has its own file, lock and
pressure), and no account, model or endpoint is rotated to get around it.

Timeouts, navigation errors and 5xx answers during a visit are TRANSIENT: the chat keeps a durable per-chat `retry`
(`300, 600, 1200, 1800 s`, bounded, never a hold or a `stop:*`), two consecutive transient visits also push the account's
next visit by the same bounded delay, and a clean visit resets it. A failed visit stays enrolled, never counts as fresh,
successful or done (`last_ok_at` is set only by a clean visit), and due retries alternate with cursor order so one failing
chat uses at most every second slot and never starves the rest. Owner-paused or stopped chats stay enrolled in the queue
as explicit exclusions (`coverage.excluded`), never erased.

An elapsed deadline NEVER clears the hold and never permits a Go. It permits one paced CONTROLLED read-only probe per
due slot: the known chat whose read endpoint is still unverified (oldest visit first; without a mappable endpoint the
ordinary cursor chat, whose page load fetches the list), alternating with the ordinary cursor check so that one chat
that keeps answering 429 never takes every slot and the others still get paced read-only visits (a neighbour's success
never clears the hold). A tab already showing that chat is loaded again once, only when
it is idle (no draft, challenge or running response); nothing is clicked or sent. A probe that sees neither a 429 nor the
matching 2xx leaves a quiet period of one ladder rung without a strike. The machine hold (`stop:*` equal to the 429 text,
recorded together with its own cooldown) is released ONLY when the cooldown is over AND every restricted endpoint has a
strictly later success of the SAME exact path AND no 401/403/quota sighting is newer than any of those successes; the
release deletes exactly that one control (compare and delete), so a user pause, challenge, quota latch, per-chat stop or a
different `stop:*` is never cleared, and a Go afterwards still needs the normal admission. A fresh success of another
endpoint (the conversation list) never hides a newer 429 of an exact chat read. A hold without the patrol's own evidence
(a legacy hold, a foreign one, a cooldown with no recorded endpoint) becomes an explicit `legacy-unverifiable` state
(`pressure.hold_state`): it is never cleared by the clock, read-only diagnostic probes continue every `cap` seconds, and
`chatgpt-guardian resume` stays the manual action; telemetry reports
`429 hold pending: needs verified same-endpoint success / existing authorized coordinator`. Evidence is persisted across
restarts. Limitation: a restricted endpoint the tab never requests again (a deleted chat) cannot be verified, and the
hold then stays pending. Offline-tested only; nothing here claims a universally safe ChatGPT request rate.

**Pending platform approval (0.3.16).** A native tool-permission card (`data-codex-approval-surface`, one visible form
button inside the latest `in_progress` turn, no Stop, no draft, no challenge/quota/uncertain state) is the platform waiting
for a human, not a response being generated. `approval.py` merges the card facts of the same page read into an
`ApprovalObservation` that keeps `turn_state: in_progress` and sets `approval_wait` (a plain observation, a missing or
malformed fact means false); the visit outcome is `permission-blocked`, the queue row shows `state: permission-blocked`,
`reason: pending-platform-approval` with its usual visit provenance, and the chat is neither failed, done nor complete.
The tab may leave the paused card to read the next chat, so one card never starves the account. Nothing is clicked,
accepted, denied, answered or sent: every Go and continuation gate refuses it (a continuation is refused as
`running`), even when the ordinary running flag is false. Two visible cards, a card of an older turn,
a lookalike inside a user bubble or quote, Stop, a draft or a challenge keep today's protected behaviour.

**Running responses.** When the owned tab shows an active response (the existing adapter's running signal) the patrol
does not navigate away, does not send a Go, does not open a second tab and never clicks Stop. The slot becomes an
explicit `deferred` / `running-protected` visit (persisted, visible in `rotation.deferred` and `last_visit`), the next
attempt is one fresh draw later with no burst, and ordinary rotation resumes only when the tab is idle. A tab already on
the chat being visited is just read (nothing to navigate). This is deliberate and not a complete solution: navigating
away from an active response has no permitted live proof, so that path is disabled, not verified.

**Legacy coverage and startup restore.** While the patrol runs it holds the existing per-account lock for its whole life
(the same one `coverage --apply` and `startup --apply` take), so `coverage --apply` reports `busy` and `startup --apply`
(the browser unit's `ExecStartPost`) is refused: neither can reopen every enrolled chat. The independent passive observer
is unaffected. The lock is dropped with the process. After a browser restart the one-shot startup restore may run before
the patrol is back; the patrol then consolidates the reopened tabs. Drop that `ExecStartPost` line from the browser
drop-in when the account is permanently single-tab.

**The marker survives a conflicting launch.** The `mode:single-tab` control marks the recorded mode. A second legacy
`live` launch while a patrol runs fails on the account lock and leaves the marker untouched; a legacy launch with no
patrol is also refused while the mode is recorded. Rollback is deliberate and exclusive: `live --retire-single-tab`
(without `--single-tab`) takes the account lock, retires the marker only while holding it, then starts the legacy loop.

**Telemetry.** `status` gains a `rotation` block and `monitor --json` is annotated with it (see `MONITORING.md`):
besides the queue it reports `pressure` (level, effective interval, failure streak, earliest retry and its reason, the
persisted next due, the last confirmed target 2xx, controlled probes, observed 429 responses, the success rate over a
bounded window of actual visits, the hold state) and `coverage` (never-clean chats, oldest-unvisited age, retry
pending, remaining in this pass, explicit exclusions). Accounts that never ran in single-tab mode keep exactly the old
output. Ordinary Telegram stays silent about 429s and retries.

Rollback: replace `--single-tab` with `--retire-single-tab` in the unit and restart that one service (it refuses while a
patrol still runs). The rotation file is left in place and ignored; holds, pauses, quota, attempts and the browser are
untouched. Plain removal of `--single-tab` is refused while the marker is recorded, so a stray legacy start cannot undo it.

## Verification

```sh
python -m pytest tests/test_chatgpt_guardian*.py
ruff check lib/agenttools_chatgpt tests/test_chatgpt_guardian*.py
ruff format --check lib/agenttools_chatgpt tests/test_chatgpt_guardian*.py
mypy --follow-untyped-imports lib/agenttools_chatgpt
```

Policy/SQLite, cross-worker race, control authentication, lifecycle ownership, CLI and HA boundary fixtures run without account login. Chromium DOM tests require the browser optional dependency and an installed `google-chrome`; they skip explicitly when absent. The actual fixture run uses intercepted local HTML and does not exercise real account selectors. No live ChatGPT send, CDP attachment, HA Docker build, production login, CAPTCHA solution or pm-cli change is claimed. See [HA/pm-cli integration](INTEGRATION.md) for the inspected HA PR and remaining activation checks.

Conversation root (monitor and recovery): hidden clones of the conversation root (no client rects,
`display:none`/`visibility:hidden`, `hidden`/`aria-hidden` ancestor) are ignored; exactly one visible root
must remain, carrying the legacy `true` or the current URL's id. Two visible roots, or none, still refuse.
