# Test Anti-Patterns Detection Checklist

## How to Use This Reference

Scan this file when auditing an existing test suite for quality issues, conducting a test code review, or preparing a test debt plan. Patterns are ordered by severity. The "grep signal" column gives a quick shell command to find candidates in a codebase.

---

## Severity Key

| Symbol | Meaning |
|--------|---------|
| C | Critical -- test can deadlock, crash, produce false positives, or give no signal at all |
| H | High -- tests are flaky, order-dependent, or covering nothing useful |
| M | Medium -- style violations that hurt readability or future maintainability |

---

## Critical Anti-Patterns

### C1: `wait(for:)` in async test context

**Signal:** `wait(for: [` inside `func test...() async` or `@Test func`
**Grep:** `grep -rn "wait(for:" --include="*.swift" Tests/`

**Problem:** Blocks the current thread waiting for the expectation to fulfill. If fulfillment needs the same thread (e.g., `@MainActor`), it deadlocks.

**Fix:** `await fulfillment(of: [exp], timeout: 5)`

---

### C2: Test with no assertions

**Signal:** A `func test*()` or `@Test func` body with no `XCTAssert*`, `#expect`, `#require`, or `Issue.record`

**Problem:** The test always passes. Completely worthless.

**Fix:** Add at minimum one assertion on the expected outcome.

---

### C3: Real network calls in unit tests

**Signal:** `URLSession.shared`, `URLRequest`, `AF.request` inside test files.
**Grep:** `grep -rn "URLSession\|URLRequest\|AF\.request" --include="*.swift" Tests/`

**Problem:** Tests fail when the server is down or slow. They can't run offline.

**Fix:** Mock the network layer at a protocol boundary.

---

### C4: Force-unwrap in test setup

**Signal:** `!` on optionals in `setUp`, `init`, or at the top of a test body.
**Grep:** `grep -n "= try!" --include="*.swift" Tests/`

**Fix:** Use `try XCTUnwrap()` (XCTest) or `try #require()` (Swift Testing).

---

### C5: XCTAssert inside @Test (framework mixing)

**Signal:** `XCTAssert*` calls inside `@Test` functions.
**Grep:** `grep -rn "XCTAssert\|XCTFail\|XCTUnwrap" --include="*.swift" Tests/ | grep -B5 "@Test"`

**Problem:** `XCTAssert` inside `@Test` function SILENTLY PASSES -- no XCTestCase context to report to. Failures are swallowed.

**Fix:** Use `#expect`, `#require`, or `Issue.record` in `@Test` functions.

---

### C6: #expect inside XCTestCase (framework mixing)

**Signal:** `#expect` calls inside `XCTestCase` subclass methods.

**Problem:** `#expect` inside `XCTestCase` method is SILENTLY IGNORED -- no Swift Testing context. Failures are dropped.

**Fix:** Use `XCTAssert*` in `XCTestCase` methods, `#expect` in `@Test` functions.

---

### C7: Third-party XCTAssert libraries in @Test

**Signal:** `assertMacroExpansion`, `assertSnapshot`, or other third-party assertion functions that use `XCTAssert` under the hood, called from `@Test` functions.

**Problem:** Assertions silently do nothing because there's no XCTestCase context.

**Fix:** Keep these tests as `XCTestCase` subclass methods until the library adds Swift Testing support.

---

## High Priority Anti-Patterns

### H1: Shared mutable state across tests

**Signal:** `static var` mutable properties in test files.
**Grep:** `grep -n "static var\|class var" --include="*Tests.swift" Tests/`

**Problem:** Test results depend on execution order.

**Fix:** Move all state to instance properties. Reinitialize in `setUp` or `init`.

---

### H2: Only happy-path tests

**Signal:** For any public method, only one test exists with no error simulation.

**Fix:** For every async method: success test, error test, edge-case test (minimum).

---

### H3: `Thread.sleep` or `Task.sleep` in tests

**Signal:** `Thread.sleep`, `Task.sleep`, `sleep(`, `usleep(`
**Grep:** `grep -n "Thread\.sleep\|Task\.sleep\|sleep(" --include="*Tests.swift" Tests/`

**Fix:** Use `await fulfillment(of:)`, `confirmation()`, or `withMainSerialExecutor`.

---

### H4: Testing private implementation details

**Problem:** Tests break on every refactor even when behavior is unchanged.

**Fix:** Test through the public interface.

---

### H5: Date() or UUID() in test data

**Grep:** `grep -n "Date()\|UUID()\|Date\.now\|\.random(" --include="*Tests.swift" Tests/`

**Fix:** Use fixed values in test data.

---

### H6: Missing @MainActor on test types for @MainActor SUT

**Grep:** `grep -B5 "XCTestCase\|@Suite\|struct.*Tests" --include="*Tests.swift" Tests/ | grep -v "@MainActor"`

**Fix:** Add `@MainActor` to the test class/struct declaration.

---

### H7: Double-run tests (XCTest + Swift Testing)

**Signal:** `test` prefix on methods inside `XCTestCase` subclass that also have `@Test`.

**Problem:** Test runs TWICE -- once by XCTest, once by Swift Testing.

**Fix:** Drop `test` prefix for Swift Testing methods. Use display names.

---

### H8: XCTestCase parallel execution assumption

**Signal:** Tests that "worked" in XCTest parallel mode but fail in Swift Testing parallel mode.

**Problem:** XCTest ran parallel tests in SEPARATE PROCESSES. Swift Testing runs in ONE PROCESS. Shared state now breaks.

**Fix:** Eliminate shared state or add `@Suite(.serialized)` temporarily.

---

## Medium Priority Anti-Patterns

### M1: Cryptic test names

**Fix:** `test_<method>_<condition>_<expectedResult>` or `@Test("does X when Y")`.

---

### M2: XCTFail with no message

**Grep:** `grep -n 'XCTFail()' --include="*Tests.swift" Tests/`

**Fix:** `XCTFail("Expected .loaded state, got \(sut.state)")`.

---

### M3: Mocks nested inside test files

**Fix:** Move to `Tests/Mocks/MockFooRepository.swift`.

---

### M4: No test data factory

**Fix:** `Item.sample()` extension with sensible defaults.

---

### M5: Empty `tearDown` / missing `sut = nil`

**Fix:** nil out properties and add `addTeardownBlock` for leak detection.

---

### M6: Missing #expect expression context (pre-evaluated Bool)

**Signal:** `let result = (expr); #expect(result)` -- pre-evaluated Bool variable passed to `#expect`.

**Problem:** `#expect` macro can't inspect the expression tree. Failure message is useless: just "Expectation failed" with no context about what was validated or why it failed.

**Important:** Flag this even when C6 is also present. If the code is later fixed to correct context, M6 will still be wrong.

**Example:**
```swift
// BAD: loses expression context
let isValid = sut.validate(amount: 10.0)
#expect(isValid)  // Failure: "Expectation failed" — useless

// GOOD: full expression gives meaningful output
#expect(sut.validate(amount: 10.0) == true)
// Failure: "sut.validate(amount: 10.0) == true → false == true"
```

**Fix:** Put the full expression inside `#expect()`: `#expect(sut.validate(amount: 10.0))`.

---

## Quick CI Grep Script

```bash
#!/bin/bash
echo "=== C1: wait(for:) in async tests ==="
grep -rn "wait(for:" --include="*.swift" Tests/ | grep -v "await fulfillment"

echo "=== C3: Real network in tests ==="
grep -rn "URLSession\.shared\|URLSession(configuration\|AF\.request" --include="*.swift" Tests/

echo "=== C5: XCTAssert in @Test functions ==="
grep -rn -A10 "@Test" --include="*.swift" Tests/ | grep "XCTAssert\|XCTFail"

echo "=== H1: Static mutable state in tests ==="
grep -rn "static var" --include="*Tests.swift" Tests/

echo "=== H3: sleep() in tests ==="
grep -rn "Thread\.sleep\|Task\.sleep\|sleep(" --include="*Tests.swift" Tests/

echo "=== H5: Date()/UUID() in test data ==="
grep -rn "Date()\|UUID()\|Date\.now" --include="*Tests.swift" Tests/

echo "=== M2: XCTFail() without message ==="
grep -n 'XCTFail()' --include="*Tests.swift" Tests/
```
