# iOS Testing — Rules Quick Reference

## Do's — Always Follow

1. **One concept per test** — a test name should finish "When X, it Y". If `and` appears, split. (`references/swift-testing-framework.md`)
2. **Arrange -> Act -> Assert** — separate setup from action from verification with blank lines.
3. **Test through public interface only** — never call private methods directly. `@testable import` for `internal`, not `private`.
4. **Mock at protocol boundaries** — every injected dependency must be a protocol. (`references/xctest-patterns.md`)
5. **Mark `@MainActor` on test type when testing `@MainActor` ViewModels** — without this, Swift 6 compile error. (`references/async-testing.md`)
6. **Use `try #require()` for optionals** — prevents ambiguous nil crashes mid-test. (`references/swift-testing-framework.md`)
7. **Add memory leak detection in every ViewModel test** — `addTeardownBlock { [weak sut] in XCTAssertNil(sut) }`. (`references/enterprise-testing.md`)
8. **Use test data factories** — `Item.sample(name: "Draft")` is readable and stable. (`references/xctest-patterns.md`)
9. **Set `.timeLimit(.minutes(1))` on any async test** — leaked continuations hang suites. (`references/async-testing.md`)
10. **Write the failing test first when fixing a bug** — reproduces the bug, documents the fix, prevents regression.
11. **Put expressions INSIDE `#expect()`** — never pre-evaluate to a Bool variable. Macro needs the expression tree. (`references/swift-testing-framework.md`)
12. **Always `dropFirst()` when subscribing to `@Published`** — it emits current value immediately. Subscribe BEFORE triggering action. (`references/xctest-patterns.md`)
13. **Never mix assertion frameworks** — `XCTAssert` inside `@Test` silently passes. `#expect` inside `XCTestCase` silently ignored. (`references/anti-patterns.md`)

## Don'ts — Critical Anti-Patterns

### Never: Shared mutable state between tests

```swift
// BROKEN -- static state persists across tests
class CartTests: XCTestCase {
    static var cart = Cart()
}

// FIX -- each test gets a fresh instance
class CartTests: XCTestCase {
    private var sut: Cart!
    override func setUp() { sut = Cart() }
}
```

### Never: `wait(for:)` in async test contexts

```swift
// DEADLOCK
func testLoad() async {
    wait(for: [exp], timeout: 5) // Blocks thread

// FIX
    await fulfillment(of: [exp], timeout: 5)
}
```

### Never: XCTAssert inside @Test function

```swift
// SILENTLY PASSES -- no XCTestCase context
@Test func broken() {
    XCTAssertEqual(1, 2) // swallowed!
}

// FIX
@Test func correct() {
    #expect(1 == 2) // proper failure
}
```

### Never: Real Date(), UUID(), or random in tests

```swift
// FLAKY
let order = Order(createdAt: Date())

// FIX -- deterministic
let fixedDate = Date(timeIntervalSinceReferenceDate: 0)
let order = Order(createdAt: fixedDate, clock: MockClock(now: fixedDate))
```

### Never: Only testing the happy path

Every async method needs at minimum: success test, error test, edge-case test, cancellation test.
