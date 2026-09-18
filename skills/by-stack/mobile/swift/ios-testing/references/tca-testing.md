# TCA (Composable Architecture) Testing

## How to Use This Reference

Read this when testing features built with Point-Free's Composable Architecture (TCA), using `TestStore`, testing effects and dependencies, working with `TestClock`, testing navigation (tree-based and stack-based), or migrating TCA tests to Swift Testing.

---

## TestStore Fundamentals

TestStore requires `async` test functions -- every `send()` and `receive()` must be `await`ed.

```swift
// WRONG -- missing async/await
func testCounter() {
    let store = TestStore(initialState: Feature.State()) { Feature() }
    store.send(.incrementButtonTapped) { $0.count = 1 }
}

// CORRECT
@MainActor func testCounter() async {
    let store = TestStore(initialState: Feature.State()) { Feature() }
    await store.send(.incrementButtonTapped) { $0.count = 1 }
}
```

**Critical:** `@MainActor` is REQUIRED on test functions that use TestStore.

---

## Exhaustive State Assertions

Exhaustive mode (default): EVERY state change MUST be asserted in the trailing closure. Omit any changed field and the test FAILS with a diff.

The `send()` trailing closure mutates PREVIOUS state to match CURRENT -- it is NOT an assertion block.

```swift
// WRONG -- common AI mistake
await store.send(.incrementButtonTapped) {
    XCTAssertEqual($0.count, 1) // WRONG -- not how TestStore works
}

// CORRECT -- mutate $0 to expected state
await store.send(.incrementButtonTapped) {
    $0.count = 1 // "I expect count to become 1"
}
```

`send()` with no state change -- omit trailing closure. Empty closure is noise.

---

## Receiving Effect Actions

Effects that send actions back MUST be asserted with `await store.receive()`.

```swift
await store.send(.fetchData) { $0.isLoading = true }
await store.receive(\.dataResponse.success) {
    $0.isLoading = false
    $0.data = expectedData
}
```

**Modern syntax (TCA 1.4+):** `receive()` uses CASE KEY PATH syntax, NOT full action value.

```swift
// WRONG -- old-style
await store.receive(.dataResponse(.success(data))) { }

// CORRECT -- modern case key path
await store.receive(\.dataResponse.success) { $0.data = data }
```

---

## Non-Exhaustive Mode

Set `store.exhaustivity = .off` for integration tests spanning multiple composed features.

Use `.off(showSkippedAssertions: true)` to see what you're skipping (grey info boxes in Xcode).

```swift
let store = TestStore(initialState: AppFeature.State()) { AppFeature() }
store.exhaustivity = .off(showSkippedAssertions: true)
```

---

## Dependencies

Override dependencies via `withDependencies` closure on TestStore construction.

```swift
let store = TestStore(initialState: Feature.State()) {
    Feature()
} withDependencies: {
    $0.apiClient.fetchUserData = { mockUserData }
    $0.continuousClock = ImmediateClock()
    $0.uuid = .incrementing
}
```

**Critical:** If you forget to override a dependency, TCA triggers test failure: "@Dependency(\.apiClient) has no test implementation."

---

## Clock Testing

Use `TestClock` + `advance(by:)` for debounce/timer logic. `ImmediateClock` fires immediately -- defeats purpose of testing timing.

```swift
let clock = TestClock()
let store = TestStore(initialState: Feature.State()) {
    Feature()
} withDependencies: {
    $0.continuousClock = clock
}

await store.send(.searchFieldChanged("c")) { $0.query = "c" }
await clock.advance(by: .milliseconds(500))
await store.receive(\.searchResponse.success) { $0.results = ["Result"] }
```

---

## Long-Running Effects

Long-living effects that outlive the test: cancel via `let task = await store.send(.onAppear)` then `await task.cancel()`, or use non-exhaustive mode.

`store.finish()` asserts all effects have completed. Call at end of test.

```swift
@MainActor @Test func onAppear_startsTimer() async {
    let clock = TestClock()
    let store = TestStore(initialState: Feature.State()) {
        Feature()
    } withDependencies: {
        $0.continuousClock = clock
    }

    let task = await store.send(.onAppear)
    await clock.advance(by: .seconds(1))
    await store.receive(\.timerTick) { $0.elapsed = 1 }
    await task.cancel()
}
```

---

## Navigation Testing

### Tree-based navigation

Testing `.presented` + `.dismiss` pattern:

```swift
await store.send(.addButtonTapped) {
    $0.destination = .addItem(AddItemFeature.State())
}
await store.send(\.destination.addItem.saveButtonTapped)
await store.send(\.destination.dismiss) { $0.destination = nil }
```

### Stack-based navigation

```swift
await store.send(.path(.push(id: 0, state: .detail(DetailFeature.State())))) {
    $0.path[id: 0] = .detail(DetailFeature.State())
}
```

---

## @Shared State

Initialize with `Shared(value)`. Mutated in effects, use `store.state.$count.assert`:

```swift
store.state.$count.assert { $0 = 1 }
```

---

## Deprecated APIs

`TaskResult` is DEPRECATED (TCA 1.4+). Use `Result<Value, Error>`.

---

## Swift Testing Compatibility

Supported since TCA >= 1.12. CRITICAL BUG in earlier versions: TestStore failures did NOT fail `@Test`. Ensure updated TCA.

---

## Common AI Mistakes Summary

| Mistake | Fix |
|---------|-----|
| Using `XCTAssertEqual` inside `send()`/`receive()` closures | Mutate `$0` instead |
| Forgetting `await` on `send()`/`receive()` | Always `await` |
| Forgetting `@MainActor` on test | Add `@MainActor` |
| Using `store.send()` for effect-generated actions | Use `store.receive()` |
| Old full-action syntax in `receive()` | Use case key path `\.action.case` |
| Not asserting all state changes in exhaustive mode | Assert every changed field |
| Forgetting to override dependencies | Override in `withDependencies` |
| Using `ImmediateClock` when testing debounce | Use `TestClock` |
