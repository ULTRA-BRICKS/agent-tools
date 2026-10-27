# Read-only session monitoring

`pm chatgpt --workspace glide.vc tui` opens the interactive terminal monitor.
`pm chatgpt --workspace glide.vc monitor --tui` is equivalent.
`pm chatgpt --workspace glide.vc monitor --json` returns the persisted snapshot.
Without flags, `monitor` opens the TUI in a terminal and prints a table in a pipe.

## Meaning of the counters

- `enrolled`: chats whose existing Guardian watch control is enabled.
- `present`: enrolled chats with exactly one matching page in the observed browser.
- `available`: unique ChatGPT conversations open in the configured dedicated browser, including unenrolled chats. This is NOT the total number of account sessions.
- `running`: an observed current generation, independently of whether recovery is enabled.
- `sent_24h`: acknowledged Guardian dispatches in the last 24 hours.
- `resumed_24h` and `completed_24h`: recorded correlated assistant evidence, not dispatch acknowledgements. `completed_24h` counts an attempt only while its newest completion record has not been retracted (see below); it never means the underlying project work is finished.

Discovery does not enroll additional chats or authorize recovery. A third visible chat can be listed with `not-enrolled` while only two chats remain under automatic recovery. Chats in other browsers or closed conversations are outside this observer's coverage.

The `coverage` command (see README) is the only component that may enroll a discovered chat, and only an active (running or `Thinking failed`) one with no existing watch control, when the account's `auto_watch_active` flag is on. Everything else here stays read-only.

## Evidence and time

A sent attempt does not prove continuation. The observer must find the failed turn, the immediately following exact `го` user message and an assistant generation or completed answer associated with that next turn. Uncertain attempts remain uncertain. Existing completed evidence remains historical evidence even when that turn later leaves the visible DOM window.

A response is complete only when nothing shows that same response generating. If the correlated response is the newest turn, a page-wide running state, a visible Stop button or the turn's own generation marker keeps it `running` (or `unconfirmed` when the assistant start/body is not independently present, as a bare `го` echo is); a transient `complete` marker cannot outvote them. A different, newer turn generating does not invalidate an earlier genuine completion of the correlated response.

If a response already recorded as complete is observed generating again, the completion proof is retracted: `completion_observed_at` is reset to null, `completion_invalidated_at` and `completion_invalidated_observed_at` keep when the contradiction and the false completion were observed, and a deduplicated `completion-invalidated` event is appended (the earlier `continuation-completed` event and the recovery attempts database are never modified or deleted). `completed_24h` folds each attempt's completion and retraction events in time order, so telemetry written by earlier versions is corrected on the first fresh contradicting observation and a later real completion is recorded again exactly once. Missing, stale or failed observations neither retract nor invent a completion.

`last_go_at` is the recorded dispatch timestamp. `continuation_observed_at` and `completion_observed_at` are when evidence was observed, not reconstructed execution timestamps. `last_checked_at` is a successful actual DOM observation, not the most recent cached display refresh. `next_check_at` is the per-session deadline; the observer's next polling cycle can occur shortly after that deadline.

`last_go_source` is `operator` when the latest attempt is a send made on an owner-approved continuation (`POST /continue`) and `automatic` otherwise; the `send-acknowledged`/`send-uncertain` events carry the same `source`. Operator sends are ordinary attempts, so the correlation above applies unchanged: the exact `го` message after the approved turn, then assistant evidence on that same turn. The approved turn may have been a `complete` answer, so a later `continuation: completed` still only means the _continuation turn_ finished, never that the task is done.

Attempts the Guardian reserved but never clicked (a refused or interrupted continuation, state `canceled`) are not reported by telemetry: they are not sends and must not appear as `send-uncertain`. They still consume the turn's identity and budget in the recovery store.

The display uses Europe/Belgrade time. The telemetry snapshot age and the recovery service's journal heartbeat age are separate. The recovery service can be active while browser observation is unavailable. Missing, stale and failed observations must never be presented as current progress.

The initial history is marked `baseline=true`. Event IDs deduplicate unchanged evidence; consumers should not resend old baseline warnings. A displayed historical `Thinking failed` is not automatically a new failure event.

`Thinking failed` is read by the same structural detector as the recovery adapter: the newest, uniquely keyed, cancelled turn with exactly one visible status SPAN in a legacy activity header or a thinking button (`aria-expanded` `true` or `false`), outside user and markdown content. Each row reports `failure_evidence` (`activity-header`, `collapsed-button`, `expanded-button`, `ambiguous-status`, `not-cancelled`, `duplicate-turn` or empty). The status is independent of recovery blockers: an unresolved send keeps `reason` at `unresolved-send` while `status` still shows `Thinking failed`. A missing profile menu alone is `identity:unknown` (sends blocked, no durable challenge); the Activity dialog is recognised with its live `SubagentsN used` tab label.

`unanswered` is the row `status` of a conversation whose newest turn holds only the user's message: the page tags the turn `complete` (`data-talvt-turn-state`), but the turn has a user bubble and no assistant side at all (no assistant role element, agent-turn start marker, markdown output, sub-agent activity or assistant-side media). Each turn reports this structural proof as `assistant_absent` (and `assistant_media` for assistant-side media outside the user bubble, read only for a turn without a text answer). It is a current, valid observation (`observation: current`, empty `error`), not a stale or failed read.

The `complete` tag alone never gives status `complete`. That needs an answer in that same turn: assistant text (`assistant_present`, the same evidence a continuation needs), or assistant media inside a started assistant side. An earlier turn's answer is not credited to a newer user message. When neither an answer nor the absence of one is proven the status is `unknown`: a duplicate turn key, a bare agent-turn marker or an empty assistant role element, assistant-side media without any assistant marker, a turn with neither a user bubble nor an assistant side, or media rendered outside the user bubble (a user attachment shown beside the bubble also fails closed this way). A visible Stop button or an `in_progress` marker still gives `working`, and a quota alert still gives `quota-or-rate-limit`. A sent continuation whose go message is such a user-only turn stays `unconfirmed`: it is neither `running` nor `completed`. `unanswered` is a diagnostic label only; it triggers no recovery, Retry or Go, and the coverage report shows the chat as `idle`, not `finished`.

## Keyboard controls

Tab switches sessions/events. Up/Down or j/k selects rows; Page Up/Page Down moves faster. Enter opens details. `/` searches, `f` cycles all/watched/problems, `r` refreshes, Escape closes details or clears search, and `q` exits. Space pauses display refresh only; it does not pause Guardian.

The UI rereads local telemetry every five seconds by default. It does not attach to Chrome. `--interval 2..60` changes display refresh only.

## Read-only observer

The `observer` command connects only to existing pages in the configured local CDP browser. It never creates, navigates, reloads, activates or closes pages, opens model menus, submits messages, changes recovery controls, or clears stops/quotas/uncertain attempts. Disconnecting its driver leaves Chrome and Guardian running.

Before a conversation DOM counts as a fresh observation, the observer script inspects the page's existing Resource Timing buffer (read-only; it never requests, reloads, navigates or clears anything). The newest completed same-origin read of exactly this chat (`/backend-api/conversation/<id>` or `/backend-api/conversations/<id>`, an optional query string after that exact path, as the real reads carry `num_turns` and `include_has_versions`, which is never read and never identifies a chat; no other path shape, no hash, a real HTTP status) decides: HTTP 429, 401, 403 or 5xx makes the row an `error` observation with `error` `conversation-sync-http-<status>` and `error_detail` carrying only the status and timing (plus the root reason when the conversation root is also unverified); the status is `unknown` and `last_checked_at` is not advanced. A newer successful read supersedes an older failure, and the row recovers on the next scheduled check. Other chats, unrelated requests, encoded or foreign-origin names and missing or unreadable entries never block, so a page without proof behaves as before. An explicit app load-error page (no conversation turns and a visible `Could not load this ChatGPT conversation` message in the main area, outside navigation, composer and message content) is the observation error `conversation-load-failed` even without Resource Timing data. A failed sync is an observation error, not `Thinking failed`, and it changes no recovery gate or owner decision.

Default polling is 60 seconds. Unchanged idle chats back off to 120, 240 and 300 seconds; actual progress resets the per-chat delay. Only turn metadata and a local digest are persisted, not message contents. At most the last 100 rendered turns are inspected for correlation.

Passive evidence is separate from the sender's mutation admission. The observer reads at most 6 distinct already-open conversations per cycle (`passive_cap`; enrolled chats first, then tab order; only the first tab of a conversation is read), each with one bounded 5 s read; non-conversation pages (`/space`, new tab, external sites) are never read. Mutation admission is the sender's own policy (6 conversation tabs, 2 auxiliary tabs, memory floor) and never blanks evidence: with a pause active the observer keeps reporting the real state of every conversation it reads and labels the snapshot with `resource_paused` (`conversation-budget`, `auxiliary-budget`, `low-memory`, `memory-unavailable`, `""`, or null when the browser is unreachable) together with `page_count`, `conversation_page_count` and `auxiliary_page_count`. The label is computed with the observer's own `--max-pages` and `--min-available-mib` (defaults match `live`): start the observer with the same flags as the live service. A conversation beyond the cap is an explicit `unobserved` session (`beyond-passive-cap`), never a guessed state, even when it also has a duplicate tab. `summary.observation_counts` counts enrolled chats by `current`, `stale` (page seen, evidence too old), `missing` (no page), `duplicate`, `error`, `unobserved`, `unavailable`; `summary.unobserved` counts every unread conversation; `summary.coverage_complete` is true only when the browser is connected, the snapshot is fresh, every enrolled chat is `current` and no conversation was left unread.

On home, the new user service is `chatgpt-guardian-glide-observer.service`. It uses the existing Guardian Python environment and the `observer` command, not the recovery `live` command. Its startup does not restart the existing `chatgpt-guardian-glide.service` or browser.

The source account database is opened read-only and its account identity validated. Telemetry is stored separately as `<source-stem>.monitor.sqlite`; the matching `.monitor.lock` prevents duplicate writers. Runtime reads do not initialize or migrate recovery state. No snapshot read can trigger a recovery send.

For browser fixture tests on home, use a SHORT temporary path on disk, such as `/home/ultra/.cache/gm-tests`. The `/tmp` mount has a user quota, and a deep worktree path can exceed Chromium's Unix socket path limit. This test environment setting is independent of the recovery service.
