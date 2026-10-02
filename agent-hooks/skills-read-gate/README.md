# skills-read-gate

**Point:** `pre-bash` · **Fail policy:** `open` · **Priority:** 48

Before a **work-shaped action** — a `git commit`, or a build/test command — this checks that
the **mandatory skills were invoked** this session. A skill encodes the rules the action must
follow (e.g. `delegate-work-to-subagents` before dispatching, `visual-proof-cycle` before a
UI "done"); doing the work without reading the skill skips those rules.

## The marker contract (how it knows a skill was invoked)

A skill-invocation wrapper **touches one file per invoked skill**, nested under the Claude
Code session id that invoked it, in a marker dir:

```
~/.cache/agent-tools/skills-invoked/<session-id>/<skill-name>     # mtime = invocation time
```

A skill counts as invoked if its marker is **fresh** (within `SKILLS_FRESH_WINDOW_S`,
default `7200`s) and EITHER was written by **this session**, OR — as a lower-precedence
fallback — the pre-session-scoping global marker is fresh. The session id comes from
`args.session_id`, which `lib/cc_hook_bridge/dispatch.py` forwards with the same T2
precedence as `agent_id` (CC's own top-level value is authoritative; a value riding in via
`tool_input` is dropped) — see `_marker_path`/`_missing_skills`/`_sanitize_session_id` in
`skills_read_gate.py`. The session-scoped check is what closes the cross-session leak: in
practice, `skills-marker-writer` (CC's own producer) always writes to the session-scoped
path when a real CC session id is available (i.e. on every live CC invocation), so CC's own
writes never land on the shared global path anymore. The global-path fallback exists for
harnesses that have NO session-aware producer of their own — Codex has no
`pre-skill` mapping yet; opencode maps `skill` to `pre-skill` (agent-tools#661) but still
writes the global marker unless `args.session_id` is present — and for
the manual `touch` recipe further down in this README; without the fallback, either one
would silently stop working the moment ANY session id happens to ride along on the event
(Codex's own bridge forwards one on `pre-bash` too, even with no writer behind it). The
WARN/BLOCK escalation TIER (below) is also session-scoped, with no such fallback — see
`_tier_marker` in `skills_read_gate.py`. The wrapper is `skills-marker-writer` (sibling hook,
`pre-skill` point): it fires on every Skill-tool invocation and touches the marker for the
invoked skill under the same session-id nesting — see
`agent-hooks/skills-marker-writer/README.md`. In Claude Code this is live once rig registers
a `Skill` PreToolUse matcher for the bridge (`riglib` `hook_bridge_entries`); opencode maps
its native `skill` tool (agent-tools#661); Codex is not mapped yet — see
`agent-hooks/README.md`'s `pre-skill` section for the current per-harness status.

This is the honest, satisfiable action: invoking the skill writes its marker, which is what
the gate checks — it is real wiring, not a forge.

Configure via env:

- `SKILLS_INVOKED_DIR` — marker dir (default `~/.cache/agent-tools/skills-invoked`)
- `MANDATORY_SKILLS` — comma list (default `delegate-work-to-subagents,visual-proof-cycle`)
- `SKILLS_FRESH_WINDOW_S` — freshness window in seconds (default `7200`)
- `SKILLS_TIER_DIR` — warn/block tier marker dir (default `~/.cache/agent-tools/skills-read-tier`)

> The env-configured marker dirs are read at import time; CC re-invokes the script per call, so
> each call picks up the current env — this is fine, not a footgun.

## Tiering — WARN then BLOCK

If a mandatory skill has no fresh marker, the **first** work action WARNs (allow + message);
a **repeat** in the window BLOCKs (tracked by a marker keyed by `cwd` and, when a valid
session id is on the event, that session too — so session B's first action in a cwd where
session A already WARNed still gets its OWN warn, not an inherited block). The default
WARN-first tier means the gate never wedges while the marker-writer is still being wired
everywhere.

## Subagents: exempt from the orchestration/visual defaults, still gated on project skills

A subagent doing work should still have read its **project** skills, so this gate is **not** a
blanket subagent-exempt (unlike the orchestration gates 1–3). But the two default-mandatory
skills are **structurally N/A for a dispatched subagent** and are dropped from its demanded set:

- `delegate-work-to-subagents` — the subagent **is** the delegated work; demanding it delegate
  again is wrong (a sub-subagent dispatch hangs).
- `visual-proof-cycle` — a subagent committing a non-UI file has no visual proof to give.

A subagent is detected by an **`args.agent_id`** forwarded into the v1 event by
`lib/cc_hook_bridge` (present only inside a dispatched subagent; the bridge fills it from CC's
authoritative field and drops any model-supplied copy). Any **other** skill named in
`MANDATORY_SKILLS` (a project-specific rule) still applies to a subagent; only the two
orchestration/visual defaults are dropped. The orchestrator (no `agent_id`) gets the full gate.

> **Trust surface:** because this gate uses `agent_id` to *relax*, it reads ONLY the sanitized
> `args.agent_id` — not a top-level `event.agent_id` (which the bridge never writes and would be a
> trusted-but-unsanitized self-exempt vector). This is a deliberate narrowing vs.
> `orchestrator-stays-thin`, which still reads the top-level fallback.

> **Footgun:** the two names in `SUBAGENT_NA_SKILLS` (`delegate-work-to-subagents`,
> `visual-proof-cycle`) are dropped for subagents **by name, regardless of source** — so listing
> one of them EXPLICITLY in `MANDATORY_SKILLS` does NOT re-enable it for subagents (they are N/A
> for a subagent by their nature, not by config). If you want a subagent gated on visual proof,
> add a **differently-named** project skill to `MANDATORY_SKILLS` — that one is not dropped.

## No self-service bypass — request a Telegram approval instead

There is **no** env var or inline sentinel an agent can set on its own command to skip this
gate (a self-grant is security theater). The dominant reason one used to be forced —
`ALLOW_SKIP_SKILLS=1` on every subagent commit to dodge the orchestration/visual defaults — is
now handled **structurally** (those two defaults are dropped for a dispatched subagent, see
above), so no blanket override is needed. For a genuine one-off, **ASK the human**, or request
a **one-time Telegram approval**:

```bash
RIG_HATCH_REQUEST_SKILLS_READ_GATE="docs-only commit, the mandatory skills are N/A here" npm test
```

This is a **pre-bash** hook, so the inline prefix is honored: the hook parses the leading
`RIG_HATCH_REQUEST_SKILLS_READ_GATE=…` assignment out of the command string the event carries
(a pre-bash hook runs in its own process *before* the shell evaluates the `VAR=x cmd` prefix, so
the value never reaches its `os.environ`). Exporting the var into the harness environment works
too and takes precedence over an inline value.

A late approval is not wasted: if the human approves after the ask window expires, the tg-ctl daemon keeps it as a single-use `approved:<id>` grant bound to this exact operation (RIG_HATCH_REQUEST_SKILLS_READ_GATE="approved:<id>" inline for pre-bash retries of the SAME command in the SAME directory; for any hook — pre-write included — simply retrying the identical operation lets the shared lib consume the grant automatically via its pull, which requires this session's route identity to match the original ask — if the retry still denies, ask the human via `tg --tag decision`). A changed command/edit or a different directory is denied and does not consume the grant. See the shared lib docstring (`lib/agenttools_hatch_escalation/`) for the full Late-Approval Grant Protocol (agent-tools#742).

The request routes to the human over Telegram (`tg-ctl ask`) and the action is allowed **only**
on their approval. It is **deny-by-default**: a blank value or a bare `1`/`true` (no real
justification) is rejected without sending the message, and any nonzero/timeout/error verdict
denies. This replaces the old `ALLOW_SKIP_SKILLS` / `# skills-ok:` self-service hatch (removed).

## Fail-open, on purpose

`on_error: "open"`. Process discipline, not a security boundary — a crash must never wedge
the ability to commit/build.

## Test

Capture the hook's exit on its OWN line right after the pipe (so it's the hook's exit, not
`echo`'s):

```bash
chmod +x skills_read_gate.py
# no fresh markers → first commit WARNs, the next BLOCKs
echo '{"cwd":"/r","args":{"command":"git commit -m x"}}' | ./skills_read_gate.py
rc=$?; echo "exit=$rc"   # → exit=0 (first offense, WARN)
echo '{"cwd":"/r","args":{"command":"git commit -m x"}}' | ./skills_read_gate.py
rc=$?; echo "exit=$rc"   # → exit=10 (repeat → BLOCK)

# touch every mandatory skill's marker → allow
mkdir -p ~/.cache/agent-tools/skills-invoked
touch ~/.cache/agent-tools/skills-invoked/delegate-work-to-subagents ~/.cache/agent-tools/skills-invoked/visual-proof-cycle
echo '{"cwd":"/r","args":{"command":"git commit -m x"}}' | ./skills_read_gate.py
rc=$?; echo "exit=$rc"   # → exit=0 (markers fresh → allow)
```
