# Enterprise Refactoring & Tech-Debt Tracker

## Purpose

A `refactoring/` directory at the project root. One file per feature/flow, plus an overview `README.md`. GitHub checkbox syntax (`- [ ]` / `- [x]`). **Every task MUST carry a rich description** — Location, Severity, Problem, Fix — so anyone can pick it up months later.

## Why Not "Fix Everything at Once"

Single mega-PR that rewrites all concurrency: unreviewable (GCD bugs are subtle), unrevertable (one deadlock fix may mask another), blocks team, impossible to bisect regressions.

## Task Description Requirements

| Field | Example |
| --- | --- |
| **Title** | `Deadlock in NetworkManager sync dispatch` |
| **Location** | `NetworkManager.swift:47` |
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
- `cross-cutting.md` for infrastructure work (TSan CI, queue hierarchy, signposts)
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

Each feature file follows this structure. Categories within each feature are ordered by severity — fix crashes before style:

```markdown
# Feature: [Image Loading Pipeline]

> **Context**: Brief description of why this feature needs refactoring.
> What's broken, what the user impact is, what triggered this work.
> **Created**: YYYY-MM-DD | **Status**: In Progress

---

## Critical Safety (Deadlocks & Crashes)
- [ ] **C1: [Title]**
  - **Location**: `File.swift:line`
  - **Severity**: 🔴 Critical
  - **Problem**: ...
  - **Fix**: ...

## Thread Safety (Data Races & Lock Correctness)
- [ ] **T1: [Title]**
  - ...

## Queue Architecture (Consolidation & Labeling)
- [ ] **Q1: [Title]**
  - ...

## OperationQueue (KVO, Dependencies, Throttling)
- [ ] **O1: [Title]**
  - ...

## Monitoring & Performance
- [ ] **M1: [Title]**
  - ...
```

Not every feature needs all categories — include only those with actual findings. The category ordering is fixed: **Critical → Thread Safety → Queue Architecture → OperationQueue → Monitoring**.

---

## Cross-cutting File Template

```markdown
# Cross-cutting Concerns

> Work that spans multiple features or is infrastructure-level.

- [ ] **X1: Enable Thread Sanitizer in CI test scheme**
  - **Location**: `Project.xcodeproj` / CI config
  - **Severity**: 🟡 High
  - **Problem**: No automated data race detection. Races are found manually in production.
  - **Fix**: Add TSan-enabled test plan, enable in CI pipeline.

- [ ] **X2: Consolidate queue hierarchy (3 subsystems)**
  - ...

- [ ] **X3: Add os_signpost markers for concurrency profiling**
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
**PR Title**: Fix DispatchGroup imbalance in BatchUploadService
**Files to change**: BatchUploadService.swift
**Max lines changed**: ~50
**Risk**: Low (isolated fix, add defer)

### Changes:
1. Add `defer { group.leave() }` after each `group.enter()`

### Verification:
- [ ] Simulate network failure mid-batch — completion fires
- [ ] Retry 5 times rapidly — no crash
- [ ] TSan clean

### NOT in this PR:
- Queue consolidation (different category)
- Lock replacement (different task)
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

## Writing Good Task Descriptions

A task description must be understandable **6 months later** by someone with no prior context.

### Bad — Titles Without Context

```markdown
- [ ] **Fix deadlock in NetworkManager**
  - Something freezes when loading images. Probably a sync issue.
```

Six months later: "Which deadlock? What queue? What triggers it? Is this still relevant?"

### Good — Full Context

```markdown
- [ ] **C1: Deadlock in ImageCacheManager sync dispatch**
  - **Location**: `ImageCacheManager.swift:34`
  - **Severity**: 🔴 Critical
  - **Problem**: `getImage(for:)` calls `cacheQueue.sync` to read, but
    `downloadImage(for:)` also calls `cacheQueue.sync` to write. Both can be
    called from the same serial callback queue during batch loading, causing
    nested sync deadlock. Users report freezes on product listing after rapid scroll.
  - **Fix**: Change `cacheQueue` to concurrent. Keep sync reads, use
    `async(flags: .barrier)` for writes. Add `dispatchPrecondition`.
```

Six months later: crystal clear. File, line, exact symptom, exact fix.

---

## PR Size Guidelines

| Change Type | Max Lines |
| --- | --- |
| Deadlock fix (sync → async) | 50 |
| Lock replacement (semaphore → NSLock) | 100 |
| Queue consolidation (one subsystem) | 150 |
| Reader-writer barrier introduction | 100 |
| AsyncOperation base class + one concrete | 250 |
| Thread-safe collection wrapper | 150 |
| DispatchSource timer lifecycle fix | 100 |
| TSan CI integration | 50 |
| GCD → Swift Concurrency migration (one pattern) | 200 |

## Rules

- New findings go to `discovered.md` with full description, NOT into current PR
- Mark tasks `- [x]` immediately after completing
- Update `README.md` progress table after every PR
- Triage `discovered.md` at least weekly — move items to feature files
- **Never**: expand PR scope mid-implementation, skip plan update, fix "one more thing" in unrelated area
- **Concurrency-specific**: never combine a lock-type change with a queue restructure in the same PR — if the combined change introduces a deadlock, you can't tell which change caused it

## PR Verification Checklist

Before submitting each PR, verify:

```markdown
- [ ] Changes match the defined scope — no extras
- [ ] No new warnings introduced (build + analyze)
- [ ] All existing tests pass
- [ ] Thread Sanitizer enabled and passing (no new race reports)
- [ ] No deadlock risk — no sync on main, no nested sync, no ABBA
- [ ] Queue labels present on all new/changed queues
- [ ] Barriers on custom concurrent queues only, never global
- [ ] Every `group.enter()` has `defer { group.leave() }`
- [ ] `[weak self]` in repeating timer handlers and stored closures
- [ ] `dispatchPrecondition` at API boundaries for new/changed code
- [ ] Feature file updated — task marked `[x]`
- [ ] `README.md` progress table current
- [ ] New discoveries logged in `discovered.md` (not fixed in this PR)
```

## When to Deviate

**Acceptable**: critical production bug (fix now, add to plan retroactively), blocker dependency discovered (reorder), team re-prioritisation, feature larger than expected (subdivide it).

**Never**: expanding PR scope mid-implementation, skipping the plan update step, fixing "just one more thing" in an unrelated area.

---

## Concrete Example

Below is a realistic feature file for the `refactoring/` directory:

```markdown
# Feature: Image Loading Pipeline

> **Context**: Image loading uses scattered `DispatchQueue.global()` calls,
> a deadlocking sync cache read, and no cancellation. Users report app freezes
> on the product listing screen during rapid scrolling. Watchdog kills after 10s.
> **Created**: 2025-02-10 | **Status**: In Progress

---

## Critical Safety (Deadlocks & Crashes)

- [x] **C1: Deadlock in ImageCacheManager sync dispatch**
  - **Location**: `ImageCacheManager.swift:34`
  - **Severity**: 🔴 Critical
  - **Problem**: `getImage(for:)` calls `cacheQueue.sync` to read the cache, but
    `downloadImage(for:)` also calls `cacheQueue.sync` to write. Both methods can be
    called from the same serial callback queue during batch image loading, causing a
    nested sync deadlock. Users report freezing on product listing after rapid scroll.
  - **Fix**: Change `cacheQueue` from serial to custom concurrent. Keep sync for reads,
    use `async(flags: .barrier)` for writes. Add `dispatchPrecondition(.notOnQueue(cacheQueue))`
    at the public API boundary.
  - **PR**: #187 (merged 2025-02-10)
  - **Verification**: Rapid-scroll product listing 20 times — no freeze. TSan clean.

## Thread Safety (Data Races & Lock Correctness)

- [ ] **T1: Unprotected downloadTasks dictionary**
  - **Location**: `ImageDownloader.swift:12`
  - **Severity**: 🟡 High
  - **Problem**: `var downloadTasks: [URL: URLSessionTask]` is read/written from
    multiple queues without synchronization. TSan reports race on insert vs. lookup.
    In rare cases, two identical downloads start for the same URL.
  - **Fix**: Protect with `NSLock`. Lock around `downloadTasks[url]` check and insert.

## Queue Architecture (Consolidation & Labeling)

- [ ] **Q1: Replace 4 scattered global() calls with image subsystem queue**
  - **Location**: `ImageDownloader.swift:28,45`, `ImageCacheManager.swift:12`,
    `ImageResizer.swift:8`
  - **Severity**: 🟢 Medium
  - **Problem**: Four different files dispatch to `DispatchQueue.global()` with no
    coordination. No labels — crashes show only `com.apple.root.default-qos`.
  - **Fix**: Create `com.app.image` subsystem queue. All image-related work targets it.
    Add `maxConcurrentOperationCount = 4` for download operations.
```
