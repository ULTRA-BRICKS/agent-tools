# decision-request-format

**Point:** `pre-bash` · **Fail policy:** `open` · **Priority:** 25 · **Graduated verdict: advisory for a partially-structured body; BLOCKS a bare body (hatchable) and the removed `question` tag (not hatchable)**

Enforces the *self-check* half of the [`decision-request-discipline`](../../skills/universal/decision-request-discipline/SKILL.md)
skill at the one moment a skill can't: **send time.** When the agent escalates to the human's
channel (`tg --tag decision …` or `tg --tag problem …`), this parses the message body and grades
it against the structural markers the skill mandates — **Context, Options, Recommendation** —
so the human never gets a bare "A or B?".

## The graduated verdict (deliberately lenient)

A false block of the human's ONLY comms channel to his agents is worse than a malformed
message, so the hook errs HARD toward passing:

| Body | Verdict |
| --- | --- |
| COMPLETE — all three markers present (a pros/cons table counts as Options) | silent `allow`, exit 0 |
| PARTIAL — some structure, some markers missing | `allow` + an exit-0 **advisory** naming what's absent |
| genuinely BARE — no table and NONE of the three markers (a bare "A or B?") | **BLOCK, exit 10** — the message shows the exact skeleton to re-send; a justified `RIG_HATCH_REQUEST_DECISION_REQUEST_FORMAT` hatch can force it through |

Only the bare case blocks. A message that carries a pros/cons table (markdown OR HTML) — or any
one of Context / Options / Recommendation — is never blocked. Table detection is generous on
purpose: over-detecting only lets a message THROUGH.

## No `question` tag — it is blocked with a redirect (agent-tools#524)

`tg` no longer has a `question` tag (tg-cli#301, Alex 2026-09-05): an open question for the
human IS a decision request and goes out as `--tag decision` in the decision-request format. The
escalation tag set this hook grades is therefore **`decision` + `problem`**. A `tg` command
carrying the `question` tag (any case, the space or the `=` spelling) is not graded at
all — it is **BLOCKED (exit 10)** with a one-line hint to re-send as `--tag decision`, regardless
of the body, and with no hatch (there is nothing to force through: the tag does not exist). This
only moves the refusal one step earlier — `tg` itself would refuse the send with the same hint.
Multi-segment commands follow the hook's usual first-match rule: the first `tg` segment carrying
an inspected tag decides the verdict.

## Triggered (decided from a PARSED command — never a raw substring)

Only an actual `tg` command carrying an inspected `--tag` (`decision`, `problem`, or the removed
`question`):

- `tg --tag decision "<body>"` — the canonical decision request
- `--tag=decision`, and the body assembled from positional text **plus** `--title <text>`
- a `tg` later in a pipeline (`build_msg | tg --tag decision …`) — every `&&`/`||`/`;`/`|`/`&`
  segment is walked
- wrapped in leading no-op wrappers (`env TG_AI_MODEL=claude tg …`, `timeout 30 tg …`) and bare
  `NAME=VALUE` assignment prefixes (`TG_AI_MODEL=claude tg …`, the skill's own form) — the
  wrapper + its own args are peeled before tokenizing

The command is split on real shell separators **quote-awarely** — a separator inside the quoted
body (`tg --tag decision "use A | B?"`) never tears the command apart — then each segment is
**shlex-tokenized**, and the tag + body are read **off the argv** exactly as `tg` would receive
them. So none of these trip it (the `tg` / `decision` text is data, not a command + tag):

```bash
tg "remember to use --tag decision next time"   # no actual --tag flag; it's body text
echo "send a tg --tag decision report"          # the words live inside an echo operand
tg --tag report "build green"                    # a different tag — not an escalation
git commit -m "add tg --tag decision hook"       # the flag is inside the commit MESSAGE
```

## Known boundaries (all fail toward allowing)

A few shapes are missed by design, and every miss is a false NEGATIVE — the send goes through
ungraded, it is never wrongly blocked: `sudo tg …` and a command substitution `$(tg …)` aren't
unwrapped (neither is in the wrapper set, and substitutions aren't recursed), and a future `tg`
value-flag not in the known set would leak its value into the parsed body (which can only
*satisfy* a marker, never remove one). A command that shlex cannot tokenize is skipped, not
blocked.

## Not triggered (the hook only inspects escalation sends)

- any `tg` send without an escalation tag (`--tag report`, `--tag answer`, no tag at all)
- a non-`tg` command, or a `tg` substring inside a path / another flag's value
- an escalation body that **already has** all three markers → silent `allow`

## Marker detection is permissive on purpose

The three markers are matched by deliberately loose, case-insensitive regexes (e.g. *Options*
matches any of `option/variant/pros/cons/trade-off`; *Context* is also satisfied by a
`file.ext:line` code ref). The goal is to catch a body that mentions **none** of a dimension —
the bare "A or B?" — not to grade phrasing or word choice. A body that gestures at context,
lists options, and states a recommendation passes, even if it doesn't use those exact
headings. A false *positive* on a partial body costs only a one-line stderr note since the send
proceeds regardless; the block is reserved for the body with no structure at all.

## Not subagent-exempt

The skill routes the *drafting* of a decision request to a subagent precisely so it isn't done
sloppily inline — so the self-check binds that subagent too. A subagent posting a malformed
decision request is exactly what this catches; there is no `agent_id` carve-out.

## No self-service escape — external approval only

There is **no** `ALLOW_RAW_DECISION_REQUEST` env and **no** `# decision-request-ok:` inline
sentinel any more. An agent could set either on its own command, so those were self-grant
bypasses. A one-time force-through of a BARE body is requested by setting
`RIG_HATCH_REQUEST_DECISION_REQUEST_FORMAT="<written justification>"`, which routes one Telegram
approval request to Alex (deny-by-default):

A late approval is not wasted: if the human approves after the ask window expires, the tg-ctl daemon keeps it as a single-use `approved:<id>` grant bound to this exact operation (RIG_HATCH_REQUEST_DECISION_REQUEST_FORMAT="approved:<id>" inline for pre-bash retries of the SAME command in the SAME directory; for any hook — pre-write included — simply retrying the identical operation lets the shared lib consume the grant automatically via its pull, which requires this session's route identity to match the original ask — if the retry still denies, ask the human via `tg --tag decision`). A changed command/edit or a different directory is denied and does not consume the grant. See the shared lib docstring (`lib/agenttools_hatch_escalation/`) for the full Late-Approval Grant Protocol (agent-tools#742).

```bash
RIG_HATCH_REQUEST_DECISION_REQUEST_FORMAT="terse follow-up to an already-detailed thread" \
  tg --tag decision "..."
```

The inline `VAR=… <command>` prefix works because this is a **pre-bash** hook: it parses the
leading `RIG_HATCH_REQUEST_DECISION_REQUEST_FORMAT=…` assignment out of the command string the
event carries (the hook runs in its own process *before* the shell evaluates that prefix, so the
value never reaches the hook's `os.environ`). Exporting the same var into the harness environment
works too and takes precedence.

Unset means the hook never contacts Telegram; a blank or bare `1`/`true`/`yes`/`on` is rejected
without a Telegram call; a real justification runs the trusted `tg-ctl ask` and lets the bare
body through **only on exit 0**. The hatch is consulted ONLY for a bare body — a partial body is
never escalated to the human, and the removed `question` tag is blocked without consulting it.

## Fail-open, on purpose

`on_error: "open"`. A crash (an unexpected event shape, a malformed JSON event) must never
wedge the ability to send a message. Every error path returns `allow`; the only deliberate
hard stops are the narrow bare-body block and the removed-tag block.

## Test

Capture the hook's exit and stdout. Exit `0` means the send proceeds (the `allow` may carry an
advisory `message`); exit `10` is a block.

```bash
chmod +x decision_request_format.py

# a bare body (no table, none of the three markers) → BLOCK with the re-send skeleton:
echo '{"args":{"command":"tg --tag decision \"should we go with A or B?\""}}' \
  | ./decision_request_format.py; echo " exit=$?"        # → {"decision":"block","message":...} exit=10

# a partial body (one marker) → allow WITH an advisory message:
echo '{"args":{"command":"tg --tag decision \"Recommendation: go with A.\""}}' \
  | ./decision_request_format.py; echo " exit=$?"        # → {"decision":"allow","message":...} exit=0

# a complete body → allow, NO message:
echo '{"args":{"command":"tg --tag decision \"Context: the loader in app.py. Options: keep sync (simple, blocks) vs async (faster, complex). Recommendation: async.\""}}' \
  | ./decision_request_format.py; echo " exit=$?"        # → {"decision":"allow"} exit=0

# the removed question tag → BLOCK with the --tag decision redirect, whatever the body:
echo '{"args":{"command":"tg --tag=question \"should we go with A or B?\""}}' \
  | ./decision_request_format.py; echo " exit=$?"        # → {"decision":"block","message":...} exit=10

# a different tag → not an escalation, plain allow:
echo '{"args":{"command":"tg --tag report \"build green\""}}' \
  | ./decision_request_format.py; echo " exit=$?"        # → {"decision":"allow"} exit=0

# the flag inside a string, not an argv flag → plain allow:
echo '{"args":{"command":"echo \"use --tag decision\""}}' \
  | ./decision_request_format.py; echo " exit=$?"        # → {"decision":"allow"} exit=0
```

The full suite is `tests/test_decision_request_format.py` in the repo root.
