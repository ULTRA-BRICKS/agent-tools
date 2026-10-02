# Third-party skill/tool ecosystem triage: enable · harmonize · delineate

- **Status:** research / design (read-only investigation; no tooling changes in this PR)
- **Date:** 2026-06-15
- **Tracking issue:** ultrabricks/agent-tools#19
- **ROADMAP:** §10
- **Author:** triage agent (machine inspection of `~/.claude`, `~/.codex`, `~/.gemini`,
  `~/.config/opencode`, `~/.agents/skills`, `~/.sverklo`, `.serena/`)

> This spec triages the **third-party / external** tool systems only. Our own ecosystem
> (`tg`, `review`, `draw`, `3d`, `rig`, `task-cli`, `rtk`, `linear`, and the ~50 agent-tools
> rule-skills) is the *boundary*, not the subject — the third-party tools are delineated
> *against* it.

---

## 0. TL;DR

The CTO observation holds and the data is worse than "rarely invoked": across **2172 session
transcripts** on this machine, the third-party reasoning/search/memory tools are
**statistically dead**. serena = 1 call ever (a stray `write_memory`), sverklo = 1 call ever
(`review_diff`), context7 = 20 calls, Haft = 23 calls, and `debate-swarm` /
`moshi-best-practices` / `semantic-code-search` / `web-page-reading-agent-browser` = **literal
zero**. Meanwhile `Bash` fired **61,615** times and `Read` **19,151**. The specialized tools
lose to the zero-friction grep/Read path by ~100:1.

Three compounding root causes:

1. **Deferred-tool tax.** serena/sverklo/context7/claude-in-chrome are *deferred* MCP tools —
   the agent must spend a `ToolSearch` round-trip before it can even call them. Bash+grep cost
   zero round-trips. The agent rationally stays on the cheap path.
2. **Harness fragmentation.** Not one of the four supported harnesses has the full set. Codex
   has Haft but not serena/sverklo/context7. Gemini and opencode have *nothing* but `pencil`.
   "Installed in CC" is being mistaken for "installed."
3. **No routing doctrine.** Even when the tools are reachable, there is no decision table, so
   the agent burns tokens choosing between redundant systems (serena vs sverklo vs grep;
   context7 vs WebFetch vs agent-browser) or just doesn't bother.

The recommendation is aggressive **prune** (kill 3 dead skill-dirs + demote serena/sverklo to
opt-in), targeted **enable** (make the 3 keepers — agent-browser, context7, Haft — reachable in
*every* harness, and de-defer or shortcut the high-value ones), and a single committed
**routing doctrine** wired into a rig-provisioned `tool-routing` rule-skill so the doctrine is
*loaded*, not buried in a doc nobody reads.

---

## 1. Evidence (the killer step)

Parsed every `*.jsonl` under `~/.claude/projects/` (script: `/tmp/tool_usage_parse.py`,
reproduced in Appendix A). Counts are `tool_use` blocks across the full local history
(~2172 session files; the hyper-canvas subset is 1292 files / 1112 tool-bearing sessions).

### 1.1 Third-party MCP tool calls — ALL projects

| Tool family | Total calls | Notes |
|---|---:|---|
| `mcp__playwright__*` | ~470 | **Not in the §10 inventory** — but it is the de-facto browser tool. |
| `mcp__claude-in-chrome__*` | ~40 | All in a handful of sessions. |
| `mcp__context7__*` | 20 | `query-docs` 15 + `resolve-library-id` 5. |
| `mcp__haft__*` | 23 | `query` 9, `decision` 6, `commission` 5, `note` 3. |
| `mcp__computer-use__*` | 7 | `request_access`/`screenshot`/`open_application`/`wait`. |
| `mcp__sverklo__*` | **1** | one `sverklo_review_diff`. Never used for search. |
| `mcp__serena__*` | **1** | one `write_memory`. **Zero** symbol-search calls, ever. |

### 1.2 Specialized skills — ALL projects

| Skill | Invocations |
|---|---:|
| `agent-browser` | 21 |
| `superpowers:brainstorming` | 13 |
| `superpowers:systematic-debugging` | 9 |
| `h-reason` | 4 |
| `using-superpowers` | 1 |
| `semantic-code-search` | **0** |
| `web-page-reading-agent-browser` | **0** |
| `debate-swarm` | **0** |
| `moshi-best-practices` | **0** |

### 1.3 Baseline (the gravity well)

| Tool | Calls |
|---|---:|
| `Bash` | 61,615 |
| `Read` | 19,151 |
| `Edit` | 11,668 |
| `WebFetch` | 875 |
| `WebSearch` | 527 |
| `ToolSearch` | 557 |
| **all third-party MCP combined** | **~600** |

**Interpretation.** The ROADMAP's "9/204 hyper sessions (4.4%)" finding is confirmed and, if
anything, generous — by raw call volume the specialized tools are noise next to `Bash`. The
two tools that *do* see use (agent-browser, superpowers brainstorming/debugging) are exactly
the ones that are **not deferred** and **self-assert** (skill discovery / SessionStart). That
correlation is the whole thesis: **discoverability + zero round-trip friction = usage.**

---

## 2. Harness availability matrix (the "enable" reality check)

Read straight off the configs. A tool is only "enabled" if the harness in front of the agent
can actually reach it.

| System | Claude Code | Codex | Gemini | opencode | Source of truth |
|---|---|---|---|---|---|
| **serena** (MCP) | yes (deferred) | **no** | **no** | **no** | `~/.claude.json` mcpServers |
| **sverklo** (MCP) | yes (deferred) | **no** | **no** | **no** | `~/.claude.json` mcpServers |
| **context7** (MCP) | yes (deferred) | **no** | **no** | **no** | `~/.claude.json` mcpServers |
| **Haft / h-reason** (MCP) | **no MCP** (skill only) | yes (`haft serve`) | **no** | **no** | `~/.codex/config.toml` |
| **claude-in-chrome** (MCP) | yes (per-project, deferred) | bundled `chrome` plugin | **no** | **no** | `~/.claude.json` enabled/disabledMcpServers |
| **computer-use** (MCP) | per-project only (1 repo) | bundled plugin (enabled) | **no** | **no** | `~/.codex/config.toml` plugins |
| **superpowers** (plugin) | yes (user scope) | yes (`superpowers@openai-curated`) | ships `gemini-extension.json` | **no** | plugin manifests |
| **agent-browser** (CLI skill) | yes (`/opt/homebrew/bin/agent-browser`) | yes (PATH) | yes (PATH) | yes (PATH) | `~/.agents/skills/agent-browser` |
| **h-reason** (skill dir) | yes (`~/.claude/skills`) | — | — | — | hand-authored |
| **debate-swarm** (skill dir) | yes | — | — | — | hand-authored |
| **moshi-best-practices** (skill dir) | yes | — | — | — | hand-authored |
| `playwright` (MCP, *not in §10*) | yes (deferred) | yes (`npx`) | **no** | only `pencil` | both configs |

**Findings that change the triage:**

- **Gemini and opencode are bare.** Their only MCP is `pencil`. Every reasoning/search/web tool
  in §10 is invisible to them. Any doctrine that names serena/context7/Haft is a no-op in two of
  four harnesses. This is the "cc-only" failure class, quantified.
- **The ROADMAP mislabels two systems.** "Haft is a CC MCP server" — it is **not** registered as
  an MCP in CC; CC gets only the `h-reason` *skill* (which explicitly says *don't* call Haft MCP
  tools unless persisting). Haft-the-MCP lives only in **Codex**. And "superpowers is an MCP
  server" — it is a **plugin** (skill library + hooks), not an MCP. These mislabels matter because
  they imply parity that does not exist.
- **sverklo's premise is hollow.** Its registry (`~/.sverklo/registry.json`) contains **exactly
  one repo** (hyper-canvas-draft), last indexed **2026-05-16** — a month stale. "Multi-repo code
  intelligence" describes a capability that is, on this machine, single-repo and rotting.
- **serena's memories are write-only.** `.serena/memories/` is populated in a few repos
  (hyper-canvas-draft 19 files, hc-hyp544-spec 16) — but the read side fired **once ever**. The
  onboarding writes them; no agent reads them back. A memory store nobody reads is dead weight.

---

## 3. Per-tool triage table

Axes: **ENABLE** (reachable + discoverable everywhere we run?) · **HARMONIZE** (made to compose,
not collide?) · **PRUNE** (redundant enough to remove?). Verdict is one of
`KEEP+FIX` / `KEEP-AS-IS` / `DEMOTE` (keep but opt-in, off the default doctrine) / `PRUNE`.

| System | Enable status | Overlap / collision | Verdict | Rationale |
|---|---|---|---|---|
| **agent-browser** (CLI skill) | **Best-enabled thing here.** PATH binary, works in every harness, not deferred, self-describing skill stub. 21 real calls. | Overlaps WebFetch, context7, claude-in-chrome, playwright (web reading + automation). | **KEEP+FIX** | The one third-party tool that actually earns its place. Make it the *canonical* web-read + browser-automation entry; collapse the redundant browser MCPs into it where possible. |
| **context7** (MCP) | CC-only, deferred. 20 calls — low but real. | Overlaps WebFetch + agent-browser for "docs". Distinct value: version-pinned, library-resolved docs vs arbitrary web. | **KEEP+FIX** | Genuine niche (library/framework docs with `resolve-library-id`). Worth keeping *if* made reachable beyond CC and given a clear lane in the doctrine. Otherwise it stays a 20-call curiosity. |
| **Haft / h-reason** | MCP in Codex only; CC has the skill (4 calls). 23 MCP calls. | Overlaps superpowers `brainstorming`. Distinct: persistent decision records / FPF, for *irreversible* choices. | **KEEP+FIX** (harmonize hard) | Keep for decision-record durability, but the two "think-before-build" frameworks must be delineated (see §4) or they cancel each other. The `h-reason` skill already self-limits ("don't call Haft MCP unless persisting") — lean on that. |
| **superpowers** (plugin) | Multi-harness (CC plugin, Codex plugin, Gemini extension). Brainstorming 13, debugging 9. | Overlaps `h-reason` (reasoning), our own `systematic-debugging` / `tdd-red-first` rule-skills, `using-git-worktrees`. | **KEEP-AS-IS** | The highest-usage third-party framework, and it's already cross-harness. Risk is *collision* with our rule-skills, not enablement — resolve in the doctrine, don't prune. |
| **serena** (MCP) | CC-only, deferred. **1 call ever.** Memories write-only. | Overlaps sverklo + grep + our `semantic-code-search` skill. LSP-precise symbol find/refs/rename is its only unique edge. | **DEMOTE** | Symbol-precise edits are real value, but zero adoption + deferred friction + write-only memories = not worth defaulting to. Keep installed for explicit "rename this symbol across the repo" cases; drop it from the default routing doctrine and stop pretending its memory store is one of our memory systems. |
| **sverklo** (MCP) | CC-only, deferred. **1 call ever.** Registry = 1 stale repo. | Overlaps serena + grep + `semantic-code-search`. "Cross-repo" premise unmet. | **DEMOTE** (prune-candidate) | The cross-repo concept search is the only thing grep/serena don't cover — but it indexes one stale repo and nobody calls it. Either re-index broadly and earn a doctrine lane, or prune. Defaulting to DEMOTE; flag for prune at next review if still single-repo. |
| **claude-in-chrome** (MCP) | CC per-project, deferred; Codex bundled `chrome`. ~40 calls. | Overlaps playwright (dominant, ~470) + agent-browser + computer-use browser tier. **Triple-redundant.** | **PRUNE** (from default; keep only where its session-attached Chrome is uniquely needed) | Browser automation already has two better-used paths (playwright by volume, agent-browser by portability). Three DOM-aware browser drivers is the textbook "burn tokens choosing." Route browser work to agent-browser (portable) / playwright (already wired), keep claude-in-chrome only for the "drive my *logged-in* Chrome session" edge case. |
| **computer-use** (MCP) | Codex bundled (enabled); CC one project. 7 calls. | Overlaps claude-in-chrome (browser tier) but unique at *desktop/native-app* control. | **KEEP-AS-IS** | Genuinely unique (cross-app desktop control). Low usage is fine — it's a specialist, not a default. No action beyond documenting *when* it's the right tool. |
| **h-reason** (skill dir) | CC skill. 4 calls. | Same engine as Haft MCP; this is the *entry* skill. | **KEEP-AS-IS** | It's the discovery surface for Haft and already self-limits MCP usage. Keep. |
| **debate-swarm** (skill dir) | CC skill. **0 calls.** Needs Ollama + Gemini key + matplotlib. | Overlaps `review brainstorm` / `review quorum` (our own, used) and superpowers brainstorming. | **PRUNE** | Zero usage, heavy local-model setup, and we already have a *used* multi-model panel (`review brainstorm/quorum`). Classic accreted-overlap. Remove the skill dir; keep the script in a gist if sentimental. |
| **moshi-best-practices** (skill dir) | CC skill. **0 calls.** | Niche (remote-host readiness for Moshi). No overlap, just unused. | **PRUNE** (or DEMOTE to docs) | Never fired in a month. It's a one-time host-setup runbook, not a recurring skill. Move the content to a `docs/` runbook and drop the always-listed skill so it stops eating the skill-listing budget. |
| **semantic-code-search** (our skill, *boundary*) | CC skill. **0 calls.** | It's the *doctrine pointer* to serena/sverklo — which are themselves dead. | (boundary) **REWRITE** | Not third-party, but it's the thing that's *supposed* to route to serena/sverklo and visibly fails. Fold it into the new `tool-routing` skill (§5) instead of keeping a dead pointer. |
| **playwright** (MCP, *not in §10*) | CC + Codex, deferred in CC. ~470 calls — **the actual browser tool.** | Overlaps claude-in-chrome + agent-browser. | (out of scope, but **note**) | The triage can't honestly route "browser work" without naming the tool that does ~80% of it. Flagged so §10's browser-tier delineation isn't fiction. |

---

## 4. Harmonization: the overlaps that must compose, not collide

Three explicit collision zones, each with a resolution.

### 4.1 The THREE memory systems

Confirmed three distinct, non-federated stores:

1. **agent-tools `MEMORY.md`** — `~/.claude/projects/<proj>/memory/` — **actively used** (5 files,
   edited today). Index + linked notes. This is the *real* memory.
2. **serena `.serena/memories/`** — per-repo, populated by onboarding (19 files in
   hyper-canvas-draft) but **read once ever.** Write-only.
3. **sverklo `~/.sverklo/<repo>/index.db`** — one stale repo. Code-index, not prose memory.

**Resolution:** declare **agent-tools `MEMORY.md` the single source of agent memory.** It's the
only one with read-side adoption. serena memories are an onboarding artifact, not a memory
system — stop counting them and don't write doctrine that reads them. sverklo's DB is a *code
index*, not memory; it should never have been bucketed with the other two. One memory store,
named, in the doctrine. The "three memory systems" problem is solved by demoting two of them out
of the category entirely.

### 4.2 Two "think-before-build" frameworks (Haft vs superpowers brainstorming)

Both want to intercept the agent *before* it builds. They differ on durability and shape:

- **superpowers `brainstorming`** — divergent, conversational, explores intent/requirements. No
  artifact. 13 calls. Cheap, frequent, front-of-task.
- **Haft / h-reason** — convergent, produces *persistent decision records* (FPF, Pareto compare,
  `h-decide`). Heavy ceremony. 23 calls. For irreversible/expensive choices.

**Resolution (the boundary that stops them cancelling):**
> Use **superpowers brainstorming** to *open* a problem (diverge, surface options, clarify
> intent). Use **Haft** only when the decision is *irreversible or expensive* and you want a
> durable, auditable record. Everything in between — reversible, cheap, obvious — gets neither;
> just reason in-line. The `h-reason` skill already encodes "don't call Haft MCP unless
> persisting" — make that the canonical rule.

### 4.3 Web reading & browser automation (context7 vs WebFetch vs agent-browser vs claude-in-chrome vs playwright)

Five tools, two distinct jobs:

- **Read docs/web content as text:** context7 (library docs, version-pinned) → agent-browser
  (`open` + `get text`, arbitrary long/JS pages) → WebFetch (short static pages) → WebSearch
  (find URLs).
- **Drive a browser (DOM, click, screenshot):** agent-browser (portable, every harness) and
  playwright (already wired, ~470 calls). claude-in-chrome only for a *logged-in session-attached*
  Chrome. computer-use only for *non-browser native apps*.

**Resolution:** collapse the redundancy by *routing*, not by deleting working tools — see the
doctrine in §5. The prune is claude-in-chrome out of the *default* path (it's third-best at the
one job it does), not out of existence.

---

## 5. Recommended routing doctrine

This is the deliverable agents need: pick right without burning tokens. Intended to ship as a
**rig-provisioned `tool-routing` rule-skill** (so it's *loaded*, cross-harness, not a doc rotting
in `docs/`). Decision-first, fallback-ordered.

### 5.1 Code search & navigation
```
literal string / fast path     → grep / rg          (default, zero friction)
"where is symbol X defined / who calls it / rename across repo"
                               → serena              (LSP-precise; opt-in, deferred)
cross-repo concept search      → sverklo            (ONLY if the repo is indexed; else grep)
read-whole-file-then-grep      → DON'T              (use grep + targeted Read)
```
Default stays grep — the doctrine's job is to name the *exceptions* (rename-symbol, find-refs)
where serena beats grep, so the agent reaches for it deliberately, not never.

### 5.2 Docs & web
```
library / framework / SDK docs → context7           (resolve-library-id → query-docs)
arbitrary web page, long/JS    → agent-browser       (open + get text body / eval)
short static page              → WebFetch
find a URL                     → WebSearch
```

### 5.3 Browser & desktop automation
```
portable browser automation    → agent-browser       (canonical; works in every harness)
already-wired heavy automation → playwright          (if a session already uses it)
drive my logged-in Chrome tab  → claude-in-chrome    (session-attached only)
native / cross-app desktop     → computer-use
```

### 5.4 Reasoning
```
open / diverge / clarify intent → superpowers brainstorming
irreversible / expensive choice + want a durable record
                                → Haft (h-frame → h-explore → h-compare → h-decide)
reversible / cheap / obvious    → just reason in-line (no framework)
```

### 5.5 Memory
```
agent memory (notes, lessons)   → agent-tools MEMORY.md     (the ONLY memory system)
code index                      → sverklo DB (when indexed) — NOT "memory"
serena .serena/memories         → onboarding artifact, not a memory store; do not rely on it
```

### 5.6 Enablement actions (so the doctrine isn't a no-op in 2/4 harnesses)
1. **Ship the doctrine as a rule-skill**, rig-provisioned to **every** harness skill dir
   (CC/Codex/Gemini/opencode) — same mechanism rig already uses for the other rule-skills.
2. **Reduce deferred-tool friction** for the KEEP+FIX tools: either pre-load serena/context7 for
   repos where they're warranted (project-scoped, not global), or accept they stay opt-in and the
   doctrine names them as deliberate exceptions. The deferred tax is the #1 measured cause of
   non-use; pretending a doctrine fixes discoverability without addressing the round-trip cost
   will reproduce the failure.
3. **Bring agent-browser + context7 + Haft to Gemini/opencode** or explicitly scope the doctrine
   "CC/Codex only" for the rest. Don't write doctrine that references tools a harness can't see.
4. **Re-index sverklo across the active repos** *or* prune it. A single stale repo is not
   "multi-repo intelligence."

---

## 6. Prune list

| Item | Action | Why |
|---|---|---|
| **debate-swarm** (skill dir) | **Remove** the `~/.claude/skills/debate-swarm` skill | 0 calls in a month; heavy Ollama+Gemini+matplotlib setup; `review brainstorm`/`quorum` already cover multi-model ideation and *are* used. |
| **moshi-best-practices** (skill dir) | **Demote to `docs/` runbook**, drop the always-listed skill | 0 calls; one-time host-setup runbook, not a recurring skill; it eats the skill-listing budget for nothing. |
| **claude-in-chrome** (default routing) | **Prune from the default browser path** (keep installed for session-attached Chrome only) | Third-best at the one job it does; playwright (volume) + agent-browser (portability) dominate; three DOM browser drivers = token-burn on selection. |
| **serena** (default routing) | **Demote to opt-in** (rename-symbol / find-refs only); stop counting `.serena/memories` as a memory system | 1 call ever; write-only memories; deferred friction. |
| **sverklo** (status) | **Demote now; prune-candidate at next review** unless re-indexed broadly | 1 call ever; registry = 1 stale repo; "cross-repo" premise currently false. |
| **semantic-code-search** (our skill, boundary) | **Rewrite/fold** into the new `tool-routing` skill | It points at serena/sverklo which are dead; a dead pointer is worse than none. |
| **`mcp.review` / `review --mcp`** (related, see ROADMAP) | Out of scope here but **noted**: the global review MCP is inert (`review --mcp` flag was dropped). Fix-or-remove tracked separately. | Keeps the MCP registry honest. |

**Net effect:** from ~11 third-party systems down to a defensible core — **agent-browser,
context7, Haft, superpowers, computer-use** as the keepers (each with a distinct lane), serena +
sverklo demoted to opt-in, claude-in-chrome demoted to an edge case, and debate-swarm +
moshi-best-practices removed from the skill surface. One memory system. One routing doctrine,
shipped as a loaded rule-skill instead of a doc nobody reads.

---

## 7. Follow-up work (not in this PR — this is a research spec)

- [ ] Author the `tool-routing` rule-skill from §5; add it to the rig universal skill set so it
      provisions to every harness.
- [ ] rig provisioning change: bring agent-browser + context7 (+ Haft where wanted) to
      Gemini/opencode, or scope the doctrine per-harness.
- [ ] Decide serena/sverklo fate after one more usage window with the doctrine live — if still
      ~0 calls, prune outright.
- [ ] Remove `debate-swarm`; relocate `moshi-best-practices` to `docs/`.
- [ ] Address the deferred-tool round-trip tax for the KEEP+FIX MCPs (project-scoped pre-load vs
      accept-as-opt-in). This is the measured root cause; a doctrine alone won't move the numbers.

---

## Appendix A — evidence script

```python
# /tmp/tool_usage_parse.py — counts tool_use blocks across ~/.claude/projects/**/*.jsonl
import json, glob, os, collections
proj_root = os.path.expanduser('~/.claude/projects')
files = glob.glob(os.path.join(proj_root, '**', '*.jsonl'), recursive=True)
tool_counts = collections.Counter(); skill_counts = collections.Counter()
for f in files:
    with open(f) as fh:
        for line in fh:
            try: o = json.loads(line)
            except Exception: continue
            content = (o.get('message') or {}).get('content')
            if isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict) and blk.get('type') == 'tool_use':
                        tool_counts[blk.get('name','')] += 1
                        if blk.get('name') == 'Skill':
                            sk = (blk.get('input') or {}).get('skill') \
                                 or (blk.get('input') or {}).get('command','')
                            if sk: skill_counts[sk] += 1
for n, c in tool_counts.most_common():
    if n.startswith('mcp__'): print(c, n)
```

Run on 2026-06-15 against 2172 session files. Figures in §1 are that run.

## Appendix B — config locations inspected

| What | Path |
|---|---|
| CC MCP servers (global) | `~/.claude.json` → `mcpServers` |
| CC per-project MCP enable/disable | `~/.claude.json` → `projects.<path>.{enabled,disabled}McpServers` |
| CC review MCP slot | `~/.claude/mcp/mcp.json` |
| CC plugins | `~/.claude/plugins/installed_plugins.json` |
| Codex MCP + plugins | `~/.codex/config.toml` |
| Gemini MCP | `~/.gemini/settings.json` |
| opencode MCP | `~/.config/opencode/opencode.json` |
| Shared skill dirs | `~/.agents/skills`, `~/.claude/skills` |
| serena memories | `<repo>/.serena/memories/` |
| sverklo index/registry | `~/.sverklo/registry.json`, `~/.sverklo/<repo>/index.db` |
| agent-tools memory | `~/.claude/projects/<proj>/memory/MEMORY.md` |
