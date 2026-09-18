# Test Suite Refactoring & Migration Workflow

## How to Use This Reference

Read this when planning a migration from legacy XCTest to Swift Testing, improving an existing test suite incrementally, or setting up a test debt tracking plan.

---

## Why Phased Migration Matters

A "migrate all tests at once" PR is:

- **Unreviewable**: 2000+ line diff of mechanical transforms
- **Risky**: One bad import or missing `@MainActor` breaks the entire suite silently
- **Scope-creeping**: Tempts combining test changes with production refactoring

Phased migration: one test file per PR, <=200 lines of test changes, no production code changes in the same PR.

---

## XCTest -> Swift Testing: Complete Assertion Mapping

### Assertion transforms

| Step | XCTest | Swift Testing |
|------|--------|--------------|
| 1 | `import XCTest` | `import Testing` |
| 2 | `final class FooTests: XCTestCase` | `@Suite("Foo") struct FooTests` |
| 3 | `func testSomething()` | `@Test("does something") func something()` |
| 4 | `setUp()` override | `init()` (or stored property with inline init) |
| 5 | `tearDown()` override | `deinit` (class) or `defer` blocks |
| 6 | `setUpWithError() throws` | `init() throws` |
| 7 | `addTeardownBlock` | No direct equivalent; use `defer` or `deinit` |
| 8 | `XCTAssertEqual(a, b)` | `#expect(a == b)` |
| 9 | `XCTAssertNotEqual(a, b)` | `#expect(a != b)` |
| 10 | `XCTAssertNil(x)` | `#expect(x == nil)` |
| 11 | `XCTAssertNotNil(x)` | `#expect(x != nil)` or `try #require(x)` |
| 12 | `XCTAssertTrue(x)` | `#expect(x)` |
| 13 | `XCTAssertFalse(x)` | `#expect(!x)` |
| 14 | `XCTAssertGreaterThan(a, b)` | `#expect(a > b)` |
| 15 | `XCTAssertLessThanOrEqual(a, b)` | `#expect(a <= b)` |
| 16 | `try XCTUnwrap(opt)` | `try #require(opt)` |
| 17 | `XCTAssertThrowsError(try f())` | `#expect(throws: Type.self) { try f() }` |
| 18 | `XCTAssertNoThrow(try f())` | `#expect(throws: Never.self) { try f() }` |
| 19 | `XCTFail("msg")` | `Issue.record("msg")` |
| 20 | `XCTExpectedFailure` | `withKnownIssue` |
| 21 | `XCTSkip` / `XCTSkipIf` | `.disabled()` / `.enabled(if:)` traits |
| 22 | `continueAfterFailure = false` | Use `#require` for must-pass assertions |
| 23 | `guard case ... else { XCTFail }` | `guard case ... else { Issue.record(...); return }` |
| 24 | `@testable import` | Keep (still needed for `internal` access) |

### AI anti-pattern warning

`#expect(a, b)` -- WRONG, `#expect` takes a single Bool expression, not two arguments.

```swift
// WRONG
guard let val = optional else { Issue.record("nil"); return }

// CORRECT
let val = try #require(optional)
```

### XCTAssertEqual with accuracy

NO direct Swift Testing equivalent. Use `isApproximatelyEqual()` from swift-numerics or `abs(a - b) <= tolerance`.

### Lifecycle mapping

| XCTest | Swift Testing |
|--------|--------------|
| `setUp/tearDown` | `init/deinit` (class) or just `init` (struct) |
| `setUpWithError() throws` | `init() throws` |
| `addTeardownBlock` | No equivalent; use `defer` or class `deinit` |
| `XCTestExpectation + fulfill() + wait` | `confirmation()` |
| `expectedFulfillmentCount` | `confirmation(expectedCount:)` |
| `isInverted = true` | `confirmation(expectedCount: 0)` |

### No-equivalent features (must stay XCTest)

- `measure {}` -- NO Swift Testing equivalent
- `XCTContext.runActivity` -- NO equivalent (use nested `@Suite` for organization)
- `XCTAttachment` -- NO equivalent

---

## Coexistence Rules

Both frameworks CAN coexist in the same target and same file.

Do NOT mix frameworks within a single test function.

### DANGEROUS mixing patterns

- `XCTAssert` inside `@Test` function SILENTLY PASSES
- `#expect` inside `XCTestCase` method is SILENTLY IGNORED
- Third-party libraries using `XCTAssert` under the hood (assertMacroExpansion, assertSnapshot) silently do nothing when called from `@Test`

### Double-run hazard

If you keep the `test` prefix AND the method is inside `XCTestCase`, the test runs TWICE.

---

## Swift Testing Instance Freshness

In XCTest, the same class instance is reused across tests. In Swift Testing, **every `@Test` function gets a fresh struct instance**.

```swift
// Swift Testing -- fresh struct per test
struct CartTests {
    var sut = Cart()    // Initialized fresh for every @Test function

    // For complex setup, use init():
    init() {
        sut = CartViewModel(repository: MockRepo())
    }
}
```

---

## Migration Plan Structure

Create `refactoring/test-migration.md`:

```markdown
# Test Suite Migration: XCTest -> Swift Testing

## Phase 1: Foundation (Before any migration)
- [ ] **F1: Add missing tests for untested ViewModels**
- [ ] **F2: Fix all C1/H1 anti-patterns from audit**

## Phase 2: Migration (One file per PR)
- [ ] **M1: Migrate ItemListViewModelTests.swift**
- [ ] **M2: Migrate CartViewModelTests.swift**

## Phase 3: Enhancement (After migration)
- [ ] **E1: Add parameterized tests for PriceFormatter**
- [ ] **E2: Add test tags for critical path**
- [ ] **E3: Set up Test Plan with TSan enabled**
```

---

## PR Sizing

| Change type | Max lines |
|-------------|-----------|
| Migrate one test file | 200 |
| Add missing test coverage | 150 |
| Fix anti-patterns (C1/H1) | 100 |
| Add parameterized tests | 100 |
| Test infrastructure setup | 150 |
| Test Plan setup | 50 |

**Never mix production code changes with test suite changes in the same PR.**

---

## Pre-Migration Checklist

```text
[] Target is Xcode 16+ in CI
[] Swift Testing can coexist with XCTest in same target
[] All tests in the file currently pass
[] No wait(for:) deadlock (fix first)
[] No static shared state (fix first)
[] @MainActor present on ViewModel
```

---

## Post-Migration Verification

```text
[] All tests pass
[] No XCTAssert* remaining in migrated file
[] No `func test*()` remaining (all replaced with @Test)
[] @MainActor present on @Suite type if SUT is @MainActor
[] init() used for setup instead of setUp() override
[] No parallel execution issues
[] PR scope is ONLY this test file
```

---

## Common Migration Mistakes

### Missing @MainActor after class -> struct

```swift
// WRONG: @MainActor is missing
struct VMTests { ... }

// CORRECT
@MainActor struct VMTests { ... }
```

### Leaving setUp()/tearDown()

```swift
// WRONG -- setUp is not called in Swift Testing
struct VMTests {
    override func setUp() { ... }  // COMPILE ERROR

// CORRECT
struct VMTests {
    init() { sut = ViewModel(repository: MockRepo()) }
}
```

### XCTFail -> Issue.record

```swift
// WRONG -- XCTFail undefined after removing import XCTest
guard case .loaded(let items) = sut.state else {
    return XCTFail("Wrong state")
}

// CORRECT
guard case .loaded(let items) = sut.state else {
    Issue.record("Expected .loaded state, got \(sut.state)")
    return
}
```

---

## Tracking Progress

```markdown
# Test Suite Refactoring Progress

| File | Tests | Migrated | Anti-patterns Fixed | Status |
|------|-------|----------|---------------------|--------|
| ItemListViewModelTests | 8 | 8 | 2 | Done |
| CartViewModelTests | 5 | 0 | 0 | Planned |
| **Total** | 13 | 8 | 2 | 62% |
```
