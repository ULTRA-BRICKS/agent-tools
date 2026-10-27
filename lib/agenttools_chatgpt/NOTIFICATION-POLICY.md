# Guardian notification policy

Guardian works quietly. Local coverage reports are evidence for the
repair coordinator. They do not send Telegram messages or ask the owner to inspect
an observed condition.

## Machine setting

Coverage configurations support:

```json
"notification_mode": "coordinator-only"
```

Coverage defaults to this mode when an older configuration omits the setting.
Other values are rejected; there is no local direct-send mode. A separately
integrated attention worker must implement the same quiet policy. An older
attention implementation must not be enabled with passive-alert behavior.

Coverage continues to observe, enroll eligible active chats, restore missing
enrolled tabs and start a failed recovery service under its existing safety rules.
Coverage JSON retains findings in `diagnostics`; `plan.alerts` and `effects.alerts`
remain empty. Normal reports remain available to the system journal. Existing
delivery history is retained, including ambiguous outcomes. Coverage never drains,
rearms or retries an old notification queue. An attention worker, if separately
integrated, must follow the same contract: track freshness and episodes locally,
emit diagnostics, and leave notifications empty.

The former coverage `tg_notifier` entry point remains for compatibility but refuses
direct sending. This change does not install an attention worker, a new sender,
a background service or an escalation evidence producer.

## Coordinator escalation contract

Only the repair coordinator may contact the owner. It must first silently perform
all applicable, authorized recovery and diagnostic actions. It records the actual
action, result and verification evidence for each applicable method, and the reason
for any method that cannot apply. A retry counter, an elapsed-time threshold, a
failed service, stale observation, HTTP 429, unavailable tool, or empty method list
does not establish that the owner's intervention is necessary.

The coordinator may send one concise request only when fresh evidence proves an
unresolved blocker, applicable recovery has been exhausted, and a specific action
by the owner is unavoidable. It identifies that smallest action and the verified
attempts that led to it. Completed recovery, continued progress, waiting for a
cooldown, and unchanged incidents remain silent.

The same incident and required owner action retain one durable identity across
timer runs, process restarts, changed wording and refreshed timestamps. An uncertain
delivery remains pending: reconcile the existing receipt before any resend or
fallback. Respect intentional pauses, active generations, protected authentication
and account boundaries while attempting repairs.

## Verification

The focused regression tests use real temporary telemetry, recovery and delivery
state with fake external process/browser/service boundaries. They verify no sender
is reached for passive faults, repeat intervals or historical delivery records.
They also verify that diagnostics remain available, configuration cannot enable
direct sending, and eligible enrollment and service repair continue even when an
old alert table is malformed.

```sh
python3 -m unittest discover -s tests -p test_guardian_quiet_coverage.py -v
```

The default target is the checkout's `lib/agenttools_chatgpt/coverage.py`. To check
an installed runtime, run that environment's Python with
`GUARDIAN_QUIET_TEST_TARGET=installed` and `-I`, using the same test file. The harness
then adds no source import path and requires `coverage.py` to resolve to that
interpreter's `sysconfig` installation directory. Both modes reject an unexpected
import origin before running any test.

The existing coverage scenarios check local diagnostics for observation loss,
failed services, stalled work and pauses. They also check that old delivery records
survive recurrence and quiet periods, while monitoring and repair keep their
existing assertions.
