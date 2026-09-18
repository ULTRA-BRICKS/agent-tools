# Enterprise Refactoring & Tech-Debt Tracker

## Purpose

A `refactoring/` directory at the project root. One file per feature/flow, plus an overview `README.md`. GitHub checkbox syntax (`- [ ]` / `- [x]`). **Every task MUST carry a rich description** — Location, Severity, Problem, Fix — so anyone can pick it up months later.

## Why Not "Fix Everything at Once"

Single mega-PR that rewrites all concurrency: unreviewable (actor isolation bugs are subtle), unrevertable (one Sendable fix may mask a data race), blocks team, impossible to bisect regressions.

## Task Description Requirements

| Field | Example |
| --- | --- |
| **Title** | `Cooperative pool deadlock in VisionService` |
| **Location** | `VisionService.swift:47` |
| **Severity** | 🔴 Critical / 🟡 High / 🟢 Medium |
| **Problem** | 2-4 sentences: what's wrong, what breaks |
| **Fix** | Concrete steps |

Optional: Dependencies, Blocker, PR link, Found during, Branch, Verification, TSan output.

---

## Directory Structure

```text
refactoring/
├── README.md                  ← overview dashboard (always up to date)
├── image-loading-pipeline.md  ← one feature / flow / screen
├── checkout-flow.md
├── cross-cutting.md           ← work that spans multiple features
└── discovered.md              ← triage inbox for new findings
```

**Rules:**

- One file per feature, flow, or screen that needs refactoring
- File name = kebab-case feature name
- `cross-cutting.md` for infrastructure work (TSan CI, strict concurrency flags, Sendable audit)
- `discovered.md` for new findings not yet assigned to a feature — triage regularly
- `README.md` is the only file stakeholders need to read for status

---

## README.md Template

When created for a new project, you **MUST** create a `refactoring/` directory with this `README.md`:

```markdown
# Refactoring Plan

Last Updated: YYYY-MM-DD

| Feature / Scope | File | Total | Done | Status |
|-----------------|------|-------|------|--------|
| [Feature Name] | [feature-name.md](feature-name.md) | 0 | 0 | Planned |
| Cross-cutting | [cross-cutting.md](cross-cutting.md) | 0 | 0 | Planned |
| **Total** | | **0** | **0** | **0%** |

## Discovered (needs triage)

See [discovered.md](discovered.md) for new findings not yet assigned to a feature.
```

---

## Feature File Template

Each feature file follows this structure. Categories within each feature are ordered by severity — fix crashes before warnings:

```markdown
# Feature: [Image Loading Pipeline]

> **Context**: Brief description of why this feature needs refactoring.
> What's broken, what the user impact is, what triggered this work.
> **Created**: YYYY-MM-DD | **Status**: In Progress

---

## Critical Safety (Crashes & Hangs)
- [ ] **C1: [Title]**
  - **Location**: `File.swift:line`
  - **Severity**: 🔴 Critical
  - **Problem**: ...
  - **Fix**: ...

## Data Race Safety (Sendable & Isolation)
- [ ] **D1: [Title]**
  - ...

## Actor Correctness (Reentrancy & Isolation)
- [ ] **A1: [Title]**
  - ...

## Compiler Warnings (Strict Concurrency)
- [ ] **W1: [Title]**
  - ...

## Best Practices & Performance
- [ ] **P1: [Title]**
  - ...
```

Not every feature needs all categories — include only those with actual findings. The category ordering is fixed: **Critical Safety → Data Race Safety → Actor Correctness → Compiler Warnings → Best Practices**.

---

## Cross-cutting File Template

```markdown
# Cross-cutting Concerns

> Work that spans multiple features or is infrastructure-level.

- [ ] **X1: Enable SWIFT_STRICT_CONCURRENCY=complete in CI**
  - **Location**: `Project.xcodeproj` / CI config
  - **Severity**: 🟡 High
  - **Problem**: No automated concurrency warning detection. Issues found manually in production.
  - **Fix**: Set SWIFT_STRICT_CONCURRENCY=complete, add to CI build step.

- [ ] **X2: Enable TSan + LIBDISPATCH_COOPERATIVE_POOL_STRICT in CI**
  - ...

- [ ] **X3: Audit all @unchecked Sendable usage**
  - ...

- [ ] **X4: Migrate leaf modules to strict concurrency (bottom-up)**
  - ...
```

---

## Discovered File Template

```markdown
# Discovered During Refactoring

> New issues found while working on other tasks.
> Add here immediately — do NOT fix in-flight.
> Triage regularly: move items to the appropriate feature file or cross-cutting.md.

- [ ] **D1: [Title]**
  - **Found during**: [which task / feature]
  - **Location**: `File.swift:line`
  - **Severity**: 🟡 High
  - **Problem**: ...
  - **Fix**: ...
  - **Assign to**: [feature file name or cross-cutting]
```

---

## Step-by-Step Protocol

### Step 1: Create the refactoring/ directory

On first analysis of a codebase, propose creating the `refactoring/` directory. Scan the codebase and create:

1. `README.md` with the overview table
2. One feature file per feature/flow that needs work
3. `cross-cutting.md` for infrastructure tasks
4. `discovered.md` (empty template)

### Step 2: Plan one PR

Before writing any code, define the scope. Each PR targets tasks within **one feature file**:

```markdown
## Next PR Scope

**Feature file**: checkout-flow.md
**PR Title**: Fix continuation leak in PaymentService
**Files to change**: PaymentService.swift
**Max lines changed**: ~50
**Risk**: Low (isolated fix, add missing resume)

### Changes:
1. Add `continuation.resume(throwing: error)` on the error path

### Verification:
- [ ] Simulate payment timeout — no hang
- [ ] Cancel payment mid-flight — task completes
- [ ] TSan clean

### NOT in this PR:
- Actor extraction for TokenManager (different category)
- Sendable conformance for PaymentResult (different task)
```

### Step 3: Execute the PR

Apply ONLY the changes listed in the PR scope. If you discover new issues:

- **DO NOT fix them now**
- Add them to `refactoring/discovered.md` with a full description
- Continue with the original scope

### Step 4: Update the plan

After each PR:

1. Mark the task done in the feature file: `- [ ]` → `- [x]`
2. Update the `README.md` progress table
3. Triage `discovered.md` — move items to the right feature file
4. If a feature is fully done, mark it Completed in `README.md`

---

## PR Size Guidelines

| Change Type | Max Lines |
| --- | --- |
| Continuation fix (add missing resume) | 50 |
| Sendable conformance annotation | 100 |
| Actor extraction from class | 200 |
| Module-level strict concurrency migration | 300 |
| @unchecked Sendable → actor/Mutex conversion | 150 |
| AsyncStream lifecycle fix (finish + onTermination) | 100 |
| TaskGroup throttling addition | 100 |
| @MainActor annotation propagation | 150 |
| TSan + LIBDISPATCH_COOPERATIVE_POOL_STRICT CI setup | 50 |

## Rules

- New findings go to `discovered.md` with full description, NOT into current PR
- Mark tasks `- [x]` immediately after completing
- Update `README.md` progress table after every PR
- Triage `discovered.md` at least weekly — move items to feature files
- **Never**: expand PR scope mid-implementation, skip plan update, fix "one more thing" in unrelated area
- **Concurrency-specific**: never combine an isolation change with a Sendable conformance change in the same PR — if the combined change introduces a data race, you can't tell which change caused it

## PR Verification Checklist

Before submitting each PR, verify:

```markdown
- [ ] Changes match the defined scope — no extras
- [ ] No new warnings introduced (build with SWIFT_STRICT_CONCURRENCY=complete)
- [ ] All existing tests pass
- [ ] Thread Sanitizer enabled and passing (no new race reports)
- [ ] No cooperative pool blocking — no semaphore.wait, Thread.sleep in async context
- [ ] Every continuation resumes on every code path
- [ ] Actor state re-checked after every await
- [ ] AsyncStream finish() called in onTermination
- [ ] No new @unchecked Sendable without documented justification
- [ ] Feature file updated — task marked `[x]`
- [ ] `README.md` progress table current
- [ ] New discoveries logged in `discovered.md` (not fixed in this PR)
```

## When to Deviate

**Acceptable**: critical production crash (fix now, add to plan retroactively), blocker dependency discovered (reorder), team re-prioritisation, feature larger than expected (subdivide it).

**Never**: expanding PR scope mid-implementation, skipping the plan update step, fixing "just one more thing" in an unrelated area.
