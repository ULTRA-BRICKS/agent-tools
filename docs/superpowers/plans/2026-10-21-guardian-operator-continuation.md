# Guardian owner-approved continuation (0.3.0)

Refs agent-tools#891 (Guardian). No separate ticket was created: the work extends that task's
recovery service and needs no new tracking identity.

## Snapshot provenance

This branch (`guardian-operator-20261021`) starts at `ef514ca`. Its first commit imports the
deployed Guardian 0.2.2 working tree verbatim:

- source: dirty worktree `.worktrees/chatgpt-observability` (HEAD `ef514ca`), which was never
  modified;
- scope: `git diff HEAD -- lib/agenttools_chatgpt tests/test_chatgpt_guardian*.py
  .github/workflows/chatgpt-guardian.yml docs/superpowers`, applied with `git apply --index`;
- verification: every imported file compares byte-identical to the source worktree, and the
  Python files compare identical to the installed 0.2.2 package in
  `~/.local/share/agent-tools/chatgpt-guardian-venv` (only `MONITORING.md` and `pyproject.toml`
  are not installed as package files).

Everything after that commit is the 0.3.0 deliverable below.

## Problem

A *complete* turn is not a complete task (a model can stop at a tool quota and still report the
turn complete). Automatic recovery needs a verified `Thinking failed` and must not be weakened.
The owner decides such cases in Telegram; pm records the tap and posts a single-use approval to
the account-local Guardian API. Guardian stays the sole browser sender.

## Contract

`POST /continue` `{request_id, chat, expected_turn, approved_at, expires_at}` (bearer token of the
account's control service; `expires_at <= approved_at + 900`) answers `202 {ok, request_id,
status:"queued"}` (idempotent) or a recorded `409 refused`; `GET /continuations/<uuid>` returns
`queued|refused|sent|uncertain` plus a reason and metadata-only receipt. See the package README.

## Design decisions

- One durable store. A `continuations` table in the existing per-account SQLite state holds the
  ledger; a partial unique index on `(chat, turn)` for owning states (`queued`, `dispatching`,
  `sent`, `uncertain`) gives at-most-once ownership across restarts and concurrent callers.
  Refused requests never own a turn. The real dispatch is an ordinary `attempts` row, so the
  existing cooldown, hourly limit, unresolved-send block and monitoring correlation apply.
- API only queues. The loopback handler validates, persists and wakes the loop. It cannot reach
  the browser, and without a live `--apply` loop it refuses before storing anything.
- Separate operator policy. `Observation.operator_candidate()/operator_ready()` and a JS
  `mode: 'operator'` gate accept a non-generating latest turn (`complete`, `cancelled`, or no
  state marker = `unknown`) without requiring or faking `Thinking failed`. `LiveAdapter(operator=True)`
  selects it; the default adapter and every automatic-recovery test are unchanged. `turn_state`
  is a new optional `Observation` field, so the contract-driven HA adapter is untouched.
- Revalidate immediately before mutation: fresh DOM read, durable reservation, second DOM read
  that must equal the first, then a last gate inside the dispatch transaction. A refusal before
  the click releases the reservation (nothing was sent, the turn is not poisoned); anything after
  the click is invoked is `uncertain` and never retried. A restart converts an in-flight
  `dispatching` row to `uncertain/restart-interrupted` and leaves its attempt `reserved`.
- Same budget as failed-turn recovery: at most one mutation burst per live cycle and the same
  resource, transport, stop and single-page gates (`mutation_window_open`, now shared).
- Telemetry stays truthful: an operator send is an attempt labelled `last_go_source: operator`;
  the following exact `го` reply is correlated by the unchanged monitor logic.

## Not done / limits

- No live browser, send, service restart or deployment was performed; evidence is fixtures and
  fakes. The real ChatGPT DOM state markers for non-failure turns are inferred from the monitor
  fixtures and must be confirmed by main before deploy.
- pm (approval ledger, Telegram tap, HTTP client) and tg (facade) are separate deliverables.
