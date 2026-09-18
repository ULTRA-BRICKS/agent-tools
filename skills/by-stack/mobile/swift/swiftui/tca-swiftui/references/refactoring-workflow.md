# Refactoring Workflow — `refactoring/` Directory Protocol

> Shared workflow across all iOS architecture skills. Adapted for TCA-specific concerns.

## Directory Structure

When first encountering a TCA codebase that needs modernization:

```
refactoring/
├── README.md               # Progress dashboard
├── feature-list.md         # All features and their migration status
├── discovered.md           # New findings during refactoring (with full descriptions)
└── features/
    ├── login.md            # Per-feature migration plan
    ├── home-feed.md
    └── settings.md
```

## README.md Progress Table

```markdown
# TCA Modernization Progress

| Feature | Phase | Status | PR | Notes |
|---------|-------|--------|----|-------|
| Login | @ObservableState migration | ✅ Done | #142 | |
| Home Feed | God reducer decomposition | 🔄 In Progress | — | Split into 3 child features |
| Settings | Not started | ⏳ | — | Depends on Login migration |
```

## Per-Feature Plan Template

```markdown
# Feature: Home Feed

## Current State
- [ ] Uses WithViewStore
- [ ] God reducer (800+ lines)
- [ ] Environment-based dependencies
- [ ] No tests

## Migration Plan

### Phase 1: Critical (Safety)
- [ ] Replace empty enum cancel IDs with enum-with-cases
- [ ] Fix captured @ObservableState in effects

### Phase 2: @ObservableState Migration
- [ ] Add @ObservableState to State
- [ ] Remove WithViewStore in view
- [ ] Replace @PresentationState with @Presents

### Phase 3: Decomposition
- [ ] Extract FeedItemFeature child reducer
- [ ] Extract FeedFilterFeature child reducer
- [ ] Add delegate actions for child→parent communication

### Phase 4: Dependencies
- [ ] Convert protocol dependencies to @DependencyClient
- [ ] Move live implementations to separate module

### Phase 5: Testing
- [ ] Add exhaustive tests for each child reducer
- [ ] Add non-exhaustive integration test for feed flow
```

## PR Sizing Rules

- **Maximum 200 lines changed per PR** (excluding generated code)
- **Single concern per PR** — one migration step, one decomposition, one dependency conversion
- **Never mix refactoring with feature work** — separate PRs

## Phase Ordering

1. **Critical safety fixes** — cancel ID bugs, effect capture issues
2. **@ObservableState migration** — feature by feature, bottom-up
3. **God reducer decomposition** — extract children, add delegate actions
4. **Dependency modernization** — protocols → @DependencyClient
5. **Test coverage** — exhaustive unit + non-exhaustive integration

## Discovered Issues

During any phase, new issues go to `refactoring/discovered.md` with **full descriptions** — NOT into the current PR:

```markdown
## Discovered Issues

### [2024-01-15] Settings screen uses NavigationStack inside NavigationStack
- **Location:** SettingsView.swift:45
- **Severity:** 🟡 High
- **Problem:** Nested NavigationStack causes unpredictable back button behavior
- **Fix:** Remove inner NavigationStack, use parent's navigation path
- **Blocked by:** Settings feature migration (Phase 2)
```
