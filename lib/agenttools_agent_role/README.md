# agenttools_agent_role

The ONE place agent-tools decides **"is this agent run a subagent?"** — pure, stdlib-only, no
global state (the environment is an explicit `environ` argument that defaults to `os.environ`).

The four hook bridges (`cc_hook_bridge`, `codex_hook_bridge`, `omp_hook_bridge`,
`opencode_hook_bridge`) turn their harness' native signal into `args.agent_id` with the
primitives below, and every agent-hook that needs the answer asks `is_subagent(...)` instead of
carrying its own copy of the env / `PI_SESSION_FILE` logic. Bridges run as
`PYTHONPATH=<agent-tools>/lib python3 -m <bridge>`, so this sibling package imports the same way;
hooks insert `<agent-tools>/lib` (two levels above the script) on `sys.path` first.

## The role model (short form)

```
role = subagent    iff a TRUSTED subagent signal is present
role = coordinator otherwise
```

A signal is **trusted** only if the harness or the launcher wrote it and the model cannot forge
it: a hook-payload top-level field, a harness plugin/extension context value, harness-managed
env (omp clears it on the main session), or launcher env set on the CHILD at exec. Anything in
`tool_input` / tool args is model-controlled and is stripped by every bridge. There is
deliberately **no** positive "I am the coordinator" marker — env is inherited by children, so it
would label a coordinator's own `claude -p` / `omp -p` children as coordinators.

| Harness            | Trusted signal → `args.agent_id`                                                                                                 | Primitive                 |
| ------------------ | -------------------------------------------------------------------------------------------------------------------------------- | ------------------------- |
| Claude Code        | top-level `agent_id` (+`agent_type`) of the hook payload — present ONLY inside a Task/Agent subagent                             | `native_payload_agent`    |
| Codex              | top-level `agent_id` (+`agent_type`) — optional in the `PreToolUse`/`PermissionRequest` schema, present only in a spawned thread | `native_payload_agent`    |
| omp                | extension ctx `agentId`; nested `sessionFile`; nested `PI_SESSION_FILE`; `isMainSession === false`                               | `omp_nested_agent_id`     |
| opencode           | `session.parentID != null` of the tool call's session (plugin looks it up via its client)                                        | `opencode_child_agent_id` |
| any detached child | launcher env on the child: `RIG_AGENT_ID=<name>` or `RIG_DETACHED_AGENT=1`                                                       | `launcher_agent_id`       |

**Not** signals: `CLAUDE_CODE_CHILD_SESSION` (`=1` on the main thread of `claude -p` too),
`CLAUDECODE`/`AI_AGENT`/`CLAUDE_CODE_ENTRYPOINT`, `session_id`/`transcript_path` (cc/codex
subagents share the parent's), tmux pane, cwd, `RIG_SESSION`/`RIG_OMP_SESSION` (session
_membership_), and any field under `tool_input`.

## API

| Function                                            | Returns                                                                                                                                                                                                                    |
| --------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `DETACHED_LABEL`                                    | `"detached"` — the id of an anonymous detached child                                                                                                                                                                       |
| `launcher_agent_id(environ=None)`                   | `RIG_AGENT_ID` (stripped, non-empty), else `"detached"` when `RIG_DETACHED_AGENT == "1"`, else `""`                                                                                                                        |
| `native_payload_agent(payload)`                     | `(agent_id, agent_type)` from the **top level** of a raw cc/codex hook payload — never `tool_input`; `("", "")` when absent/blank                                                                                          |
| `has_omp_session_ancestor(path)`                    | `True` when any ancestor directory is `<timestamp>_<uuid>`-shaped (omp Task/eval child transcript at any depth)                                                                                                            |
| `nested_id_from_session_file(raw)`                  | the agent id (file stem) of a nested omp transcript path, else `""`; path shape only, no filesystem probe                                                                                                                  |
| `omp_nested_agent_id(omp_event=None, environ=None)` | nested-session agent id: payload `sessionFile`, else env `PI_SESSION_FILE` (a present flat/main file short-circuits to `""`), else — with no file evidence at all — `omp-nested:<sessionId>` when `isMainSession is False` |
| `NESTED_SESSION_MARKER_PREFIX`                      | `"omp-nested:"`                                                                                                                                                                                                            |
| `opencode_child_agent_id(session_id, parent_id)`    | the session id when `parent_id` is non-blank, else `""`                                                                                                                                                                    |
| `is_subagent(args, environ=None)`                   | the hook predicate: sanitized `args.agent_id` non-blank **or** `launcher_agent_id(environ)` **or** a nested `PI_SESSION_FILE`                                                                                              |

`is_subagent` reads exactly what the bridges sanitize — never a top-level event `agent_id` and
never `tool_input`. The nested-`PI_SESSION_FILE` tier is not restricted to the omp harness tag
(a Claude-Code-backed omp child fires through `cc_hook_bridge`, agent-tools#676).

## Trust note

Launcher env is trusted because a running agent cannot retroactively mutate its OWN process
environment — it can only set these variables for a CHILD process it dispatches, which is the
sanctioned act of dispatching a subagent. Cooperative-orchestrator threat model,
`on_error: open`: discipline, not a security boundary.

## Tests

`tests/test_agenttools_agent_role.py` pins every primitive; each bridge's own test file pins its
harness row (trusted source wins, forged `tool_input` identity stripped, coordinator stays
coordinator).
