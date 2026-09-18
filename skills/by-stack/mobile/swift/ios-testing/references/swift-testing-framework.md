# Swift Testing Framework Reference

## How to Use This Reference

Read this when writing new tests with `@Test`/`@Suite`, using `#expect`/`#require`, writing parameterized tests, working with test tags, handling known failures, or setting up Test Plans. Swift Testing requires Xcode 16+ but supports iOS 13+ deployment targets.

---

## Core Syntax: XCTest -> Swift Testing at a Glance

| XCTest | Swift Testing |
|--------|--------------|
| `class FooTests: XCTestCase` | `struct FooTests` (or `@Suite struct`) |
| `func testSomething()` | `@Test func something()` |
| `XCTAssertEqual(a, b)` | `#expect(a == b)` |
| `XCTAssertNotEqual(a, b)` | `#expect(a != b)` |
| `XCTAssertNil(x)` | `#expect(x == nil)` |
| `XCTAssertNotNil(x)` | `#expect(x != nil)` or `try #require(x)` |
| `XCTAssertTrue(x)` | `#expect(x)` |
| `XCTAssertFalse(x)` | `#expect(!x)` |
| `XCTAssertGreaterThan(a, b)` | `#expect(a > b)` |
| `XCTAssertLessThanOrEqual(a, b)` | `#expect(a <= b)` |
| `XCTAssertThrowsError(try f())` | `#expect(throws: SomeError.self) { try f() }` |
| `XCTAssertNoThrow(try f())` | `#expect(throws: Never.self) { try f() }` |
| `XCTUnwrap(optional)` | `try #require(optional)` |
| `XCTFail("msg")` | `Issue.record("msg")` |
| `XCTExpectedFailure` | `withKnownIssue` |
| `XCTSkip` / `XCTSkipIf` | `.disabled()` / `.enabled(if:)` traits |
| `setUp()` | Stored properties with inline init (fresh per test) |
| `tearDown()` | `deinit` or `addTeardownBlock` |
| `XCTAssertEqual(a, b, accuracy:)` | No direct equivalent (see below) |

### No accuracy equivalent

```swift
// DOES NOT EXIST
#expect(a == b, accuracy: 0.01)

// CORRECT -- use swift-numerics or manual tolerance
import Numerics
#expect(Float(1.0).isApproximatelyEqual(to: 1.0001, absoluteTolerance: 0.01))
// or
#expect(abs((0.1 + 0.2) - 0.3) < 0.0001)
```

---

## @Test Macro

`@Test` turns any function into a test. Always use `@Test` attribute -- Swift Testing ignores `test` prefix naming convention.

```swift
import Testing

struct CartTests {

    // WRONG -- no @Test attribute, Swift Testing ignores it
    func testAddItem() { #expect(true) }

    // CORRECT
    @Test("adding an item increments item count")
    func addItem() {
        var cart = Cart()
        cart.add(.sample())
        #expect(cart.items.count == 1)
    }
}
```

**Test instances are fresh per `@Test` function** -- Swift Testing creates a new instance of the struct for every test. This gives free isolation (unlike XCTestCase where the same instance is reused).

---

## @Suite Macro

The `@Suite` macro is IMPLICIT -- any type containing `@Test` functions is a suite. `@Suite` is only REQUIRED for display names or traits.

```swift
@Suite("CartViewModel")
@MainActor
struct CartViewModelTests {

    let mockRepo = MockCartRepository()
    var sut: CartViewModel

    init() {
        sut = CartViewModel(repository: mockRepo)
    }

    @Test("shows empty state on first load")
    func initialState() {
        #expect(sut.items.isEmpty)
    }

    @Test("loads items from repository")
    func loadItems() async throws {
        mockRepo.stubbedItems = [.sample(), .sample()]
        await sut.load()
        #expect(sut.items.count == 2)
    }
}
```

`Test.current` gives runtime access to current test metadata. Useful for custom logging but never use it for test logic flow control.

---

## Struct vs Class @Suite Lifecycle

### Default to struct

```swift
struct ParserTests {
    let sut: Parser
    init() { sut = Parser() } // runs before EACH test
}
```

### Use class only when deinit needed

```swift
@Suite final class DatabaseTests {
    let db: TestDatabase
    init() throws { db = try TestDatabase() }
    deinit { db.cleanup() }
}
```

### Fresh instance per test

A fresh suite instance is created for EACH test function. Never rely on state carrying over between tests.

```swift
// WRONG -- count is 0 in testB, NOT 1
struct CounterTests {
    var count = 0
    @Test func testA() { count += 1 }
    @Test func testB() { #expect(count == 1) } // FAILS
}
```

### Class must be final

If using a class as a test suite, it MUST be `final`. Non-final classes are not supported.

**Decision tree -- struct vs class:**
```
Need deinit for cleanup?
+-- NO  -> struct (preferred)
+-- YES -> Need @MainActor isolation?
    +-- YES -> @MainActor final class
    +-- NO  -> final class
```

---

## #expect and #require

### #expect -- non-failing assertion

Logs a failure but the test continues. Use for most assertions.

```swift
#expect(user.name == "Alice")
#expect(items.count > 0)
#expect(response.statusCode == 200)
```

**CRITICAL: Put expressions INSIDE #expect()** -- never pre-evaluate to a Bool variable.

```swift
// WRONG -- failure message says "didReturnFive" with no context
let didReturnFive = result == 5
#expect(didReturnFive) // Output: "Expectation failed: didReturnFive"

// CORRECT -- macro captures both sides
#expect(result == 5) // Output: "Expectation failed: (result -> 3) == 5"
```

WHY: `#expect` is a syntax-aware macro that inspects the expression tree. Pre-evaluating defeats its diagnostic power.

### #require -- failing assertion (throws)

Always mark test functions `throws` when using `#require`; always prefix `#require` with `try`.

```swift
// WRONG -- won't compile
@Test func unwrap() { let user = #require(fetchUser()) }

// CORRECT
@Test func unwrap() throws { let user = try #require(fetchUser()) }
```

Use `#expect` for most assertions; reserve `#require` only for preconditions where continuing is nonsensical.

```swift
// WRONG -- overusing #require hides multiple failures
try #require(a == 1)
try #require(b == 2) // never reached if first fails

// CORRECT
let obj = try #require(fetchObject()) // precondition
#expect(obj.a == 1) // continues on failure
#expect(obj.b == 2) // still runs
```

Prefer `try #expect(expr)` over `#expect(try expr)` -- the latter can produce confusing errors in some contexts.

### Testing throws

```swift
// Expect ANY error
#expect(throws: (any Error).self) { try riskyOperation() }

// Expect a specific error TYPE
#expect(throws: NetworkError.self) { try fetch() }

// Expect a specific error VALUE (Equatable)
#expect(throws: NetworkError.timeout) { try fetch() }

// Expect NO error
#expect(throws: Never.self) { try safeCall() }
```

In Swift 6.1+, `#expect(throws:)` RETURNS the thrown error for further inspection:

```swift
let error = #expect(throws: ValidationError.self) { try validate(input) }
if case .tooShort(let min) = error { #expect(min == 8) }
```

### Floating-point comparison

```swift
// WRONG
#expect(0.1 + 0.2 == 0.3) // fails!

// CORRECT
#expect(abs((0.1 + 0.2) - 0.3) < 0.0001)
```

### CustomTestStringConvertible

`CustomTestStringConvertible` conformance improves failure messages. Add it in TEST target via retroactive conformance.

---

## All Trait Types

### .disabled

Use `.disabled("reason")` with a descriptive string -- never leave reason empty.

### .enabled(if:)

Use for feature-flag or environment-gated tests.

### @available for OS version gating

Use `@available()` (NOT `.enabled(if:)`) for OS version gating.

```swift
// WRONG -- runtime check
@Test func newStuff() { guard #available(iOS 18, *) else { return } }

// CORRECT -- test runner knows it's skipped
@available(iOS 18, *) @Test func newStuff() { }
```

### .bug

Associate tests with issue trackers:

```swift
@Test(.bug("https://github.com/org/repo/issues/42", "Crash on nil input"))
func nilInputHandling() { }
```

### .timeLimit

Use `.timeLimit(.minutes(N))` as safety net. The SHORTER duration wins when applied at both suite and test level.

### .tags

Define tags in a central extension on `Tag` -- never as freestanding constants.

```swift
// WRONG
@Tag let critical: Tag // won't be recognized

// CORRECT
extension Tag {
    @Tag static var critical: Self
    @Tag static var networking: Self
}

@Test(.tags(.critical, .networking)) func apiCall() { }
```

---

## Parameterized Tests

### zip() for paired arguments

Use `zip()` for paired input -> expected mappings. Without `zip`, two collections produce a Cartesian product.

```swift
// WRONG -- 3x3 = 9 combos
@Test(arguments: [Flavor.vanilla, .pistachio, .chocolate], [false, true, false])
func nutContent(flavor: Flavor, expected: Bool) { }

// CORRECT -- exactly 3 paired test cases
@Test(arguments: zip([Flavor.vanilla, .pistachio, .chocolate], [false, true, false]))
func nutContent(flavor: Flavor, expected: Bool) {
    #expect(flavor.containsNuts == expected)
}
```

### Sendable + CustomTestStringConvertible

Parameterized test arguments must conform to `Sendable` (and `CustomTestStringConvertible` for readable output).

### No for...in loops

Never use a `for...in` loop inside a test body when parameterized tests are available.

```swift
// WRONG -- single test; failure doesn't identify which value
@Test func allCases() { for val in [1, 2, 3] { #expect(isValid(val)) } }

// CORRECT -- each value is independent, parallel, re-runnable
@Test(arguments: [1, 2, 3]) func eachCase(val: Int) { #expect(isValid(val)) }
```

### Limits

- At most 2 argument collections. For more complex inputs, use an array of tuples or a custom struct.
- `@Test` cannot be applied inside generic structs.

---

## confirmation() Semantics

### Does NOT suspend and wait

`confirmation()` does NOT suspend and wait like `XCTestExpectation`. It checks the count when its closure RETURNS.

```swift
// WRONG -- closure returns immediately; confirm() never called; ALWAYS FAILS
@Test func completionHandler() async {
    await confirmation("callback") { confirm in
        legacyAPI.fetch { result in confirm() } // called AFTER closure returns
    }
}

// CORRECT -- use withCheckedContinuation to bridge completion handlers
@Test func completionHandler() async throws {
    let data = try await withCheckedThrowingContinuation { cont in
        legacyAPI.fetch { result in cont.resume(with: result) }
    }
    #expect(!data.isEmpty)
}
```

### Assert something NEVER happens

Use `expectedCount: 0`:

```swift
@Test func logoutDoesNotSync() async {
    await confirmation("sync triggered", expectedCount: 0) { confirm in
        sut.onSync = { confirm() } // if called -> test FAILS
        sut.logout()
    }
}
```

### Ranges (Swift 6.1+)

In Swift 6.1+, `confirmation` accepts ranges (e.g., `expectedCount: 5...10`).

**Decision tree -- confirmation vs withCheckedContinuation:**
```
Does the API have an await point inside the closure?
+-- YES -> confirmation { confirm in ... await sut.start() ... }
+-- NO (completion handler, fire-and-forget) -> withCheckedContinuation
```

---

## Parallel Execution

Tests run in parallel by default (in-process, via Swift Concurrency). These shared resources WILL break:
- Singletons
- UserDefaults.standard
- File system
- Core Data / SwiftData
- Static/global mutable variables
- Keychain
- URLProtocol global registration

### .serialized

Use `.serialized` on a Suite as a TEMPORARY measure for thread-unsafe tests. Long-term fix: eliminate shared state.

```swift
@Suite(.serialized) struct LegacyDatabaseTests { }
```

**GOTCHA:** `.serialized` on a `@Test` only affects parameterized test cases WITHIN that test -- NOT other tests in the same suite. Apply `.serialized` to the `@Suite`.

### XCTest difference

XCTest ran parallel tests in SEPARATE PROCESSES. Swift Testing runs in ONE PROCESS. Code that "worked" in parallel XCTest may break in parallel Swift Testing.

---

## @MainActor Interaction

XCTest runs synchronous tests on @MainActor by default. Swift Testing does NOT -- tests run on arbitrary executors.

```swift
// WRONG
@Test func updateUI() async { let vm = ViewModel() } // potential violation

// CORRECT
@MainActor @Test func updateUI() async { let vm = ViewModel() }
```

You can annotate an entire Suite with `@MainActor`, but tests still run in PARALLEL. Add `.serialized` if order matters.

`confirmation()` and `withKnownIssue()` accept an `isolation:` parameter for running their closure on a specific actor.

---

## withKnownIssue Semantics

`withKnownIssue` marks expected failures -- the test STILL RUNS but failures don't fail the suite.

When the bug is FIXED, `withKnownIssue` reports an UNEXPECTED SUCCESS -- alerting you to remove it. Superior to `.disabled()`.

Use `isIntermittent: true` for flaky issues that only sometimes fail.

`withKnownIssue` supports `when:` and `matching:` closures for conditional and filtered known issues.

**Decision tree -- withKnownIssue vs .disabled:**
```
Is the bug tracked and you want to know when it's fixed?
+-- YES -> withKnownIssue("reason") // test runs, alerts on unexpected pass
+-- NO  -> .disabled("reason")       // test is skipped entirely
```

---

## Combine + AsyncStream Testing in Swift Testing

### No built-in Combine support

Bridge publishers to async/await using `.values`:

```swift
@Test func publisherEmitsValues() async {
    let subject = PassthroughSubject<Int, Never>()
    Task { subject.send(1); subject.send(completion: .finished) }
    var collected: [Int] = []
    for await value in subject.values { collected.append(value) }
    #expect(collected == [1])
}
```

### Controlled streams

Always inject controlled streams in tests -- never test against real timers or network-dependent streams.

```swift
@Test func streamConsumption() async {
    let (stream, continuation) = AsyncStream<Int>.makeStream()
    continuation.yield(1); continuation.yield(2); continuation.finish()
    var collected: [Int] = []
    for await value in stream { collected.append(value) }
    #expect(collected == [1, 2])
}
```

### Infinite stream safety

Use `prefix(_:)` to prevent tests from hanging on infinite streams.

### Scheduler limitations

`confirmation()` does NOT work reliably with publishers that use `debounce`, `throttle`, `delay`, or receive on different schedulers.

---

## Known Limitations

These features require XCTest -- Swift Testing has NO support:
- UI Testing (XCUIApplication)
- Performance testing (measure {})
- XCTActivity
- Attachments
- Objective-C tests
- XCTSkip (use traits instead)

Swift 6.2 introduces `@concurrent` attribute for tests that explicitly need to run OFF the main actor when default MainActor isolation is enabled.

---

## Coexistence with XCTest

Swift Testing and XCTest can live in the same test target and same file.

Do NOT mix frameworks within a single test function.

### DANGEROUS: XCTAssert inside @Test

`XCTAssert` inside `@Test` function SILENTLY PASSES -- no XCTestCase context to report to. Failures are swallowed silently.

```swift
// DANGEROUS
@Test func brokenTest() {
    XCTAssertEqual(1, 2) // SILENTLY PASSES
}
```

### DANGEROUS: #expect inside XCTestCase

`#expect` inside `XCTestCase` method is SILENTLY IGNORED -- no Swift Testing context. Failures are dropped.

```swift
// DANGEROUS
class OldTests: XCTestCase {
    func testBroken() { #expect(1 == 2) } // test PASSES
}
```

### Third-party library danger

This is especially dangerous with 3rd-party libraries (swift-syntax `assertMacroExpansion`, SnapshotTesting `assertSnapshot`) that use XCTAssert under the hood -- calling from `@Test` means assertions silently do nothing.

### Double-run hazard

If you keep the `test` prefix AND the method is inside a class inheriting `XCTestCase`, the test runs TWICE -- once by XCTest, once by Swift Testing.

Drop the `test` prefix for Swift Testing. Use display names instead:

```swift
@Test("User creation succeeds with valid input") func userCreation() { }
```

---

## Test Plans (Xcode 16+)

Test Plans (`.xctestplan` files) replace scheme test settings. They let you run the same tests with different configurations.

**Key settings to configure per Test Plan:**
- Thread Sanitizer (detect data races)
- Code Coverage (per-configuration)
- Test Repetitions (run flaky tests N times to confirm reliability)
- Localization (run suite in multiple languages)
