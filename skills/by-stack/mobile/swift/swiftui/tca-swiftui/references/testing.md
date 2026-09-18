# Testing — TestStore, Exhaustive/Non-Exhaustive & Best Practices

## Exhaustive TestStore (Default)

Exhaustive testing requires asserting every state mutation and receiving every action from effects:

```swift
@MainActor
func testAddItem() async {
    let store = TestStore(initialState: Feature.State()) {
        Feature()
    } withDependencies: {
        $0.apiClient.fetch = { _ in mockData }
    }

    await store.send(.fetchButtonTapped) {
        $0.isLoading = true
    }
    await store.receive(\.dataLoaded) {
        $0.isLoading = false
        $0.data = mockData
    }
}
```

**Requirements:**
1. Assert every state mutation for each sent action
2. `receive` every action that effects send back
3. All effects must complete before test ends
4. All accessed dependencies must be overridden

## Non-Exhaustive Testing (Integration Tests)

For multi-feature integration tests where asserting every mutation is impractical:

```swift
let store = TestStore(initialState: App.State()) { App() }
store.exhaustivity = .off  // or .off(showSkippedAssertions: true)

await store.send(.login(.submitButtonTapped))
await store.receive(\.login.delegate.didLogin) {
    $0.selectedTab = .activity  // Only assert what matters
}
```

## Case Key Path Syntax (TCA 1.4+)

```swift
// ❌ Old verbose
await store.receive(.child(.presented(.response(.success("Hello")))))

// ✅ Modern key path
await store.receive(\.child.response.success)

// With payload (TCA 1.9+):
await store.receive(\.child.response.success, "Hello")
```

## Time-Dependent Tests

### TestClock (precise control)

```swift
let clock = TestClock()
store.dependencies.continuousClock = clock

await store.send(.startTimer)
await clock.advance(by: .seconds(1))
await store.receive(\.timerTick) { $0.count = 1 }
await clock.advance(by: .seconds(1))
await store.receive(\.timerTick) { $0.count = 2 }
```

### ImmediateClock (fast CI)

```swift
store.dependencies.continuousClock = ImmediateClock()
```

Use ImmediateClock when you don't need time control — significantly faster in CI.

## Cancelling Long-Running Effects in Tests

```swift
let task = await store.send(.startTimer)
await clock.advance(by: .seconds(2))
await store.receive(\.timerTick) { $0.count = 1 }
await store.receive(\.timerTick) { $0.count = 2 }
await task.cancel()  // MUST cancel or test hangs
```

## Per-Feature Test Checklist

```
[ ] Each user action → correct state mutation
[ ] Each effect → success response handling
[ ] Each effect → failure/error response handling
[ ] Long-running effects → properly cancelled on teardown
[ ] Delegate actions → parent receives and handles correctly
[ ] Navigation state → push/pop/present/dismiss work correctly
[ ] Unimplemented dependencies → only expected endpoints called
[ ] Bindings → @BindingState changes propagate correctly
```

## Testing Pyramid for TCA

1. **Unit tests (exhaustive)** — individual reducer actions, state mutations, effect outputs
2. **Integration tests (non-exhaustive)** — multi-feature flows, delegate action chains
3. **Snapshot tests** — use regular `Store` (not `TestStore`) with controlled state
4. **DON'T snapshot-test with TestStore**

## Testing Strategy

| Level | Store Type | Exhaustivity | Use For |
|-------|-----------|-------------|---------|
| Unit | TestStore | Exhaustive (default) | Single reducer, focused logic |
| Integration | TestStore | `.off` | Multi-feature flows |
| Snapshot | Store (regular) | N/A | UI verification |

## Common Testing Mistakes

1. **Missing `@MainActor`** on test methods — causes unexpected behavior
2. **Forgetting `await task.cancel()`** for long-running effects — test hangs
3. **Not overriding dependencies** — unimplemented endpoints fire XCTFail
4. **Reconstructing StackState** from scratch in tests — breaks element IDs. Use mutation helpers instead.
5. **Integration test with exhaustive mode** — becomes 100+ lines for simple flows. Use `.off`.
