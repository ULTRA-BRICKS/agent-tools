# Effects — Side Effects, Cancellation & Long-Running Operations

## Effect API Reference

```swift
return .none                          // No effect
return .send(.someAction)             // Emit one action synchronously
return .run { send in ... }           // Async work, send zero or more actions
return .merge(effect1, effect2)       // Run in parallel
return .concatenate(effect1, effect2) // Run sequentially
return .cancel(id: CancelID.timer)    // Cancel in-flight effect
```

## .run Pattern

```swift
return .run { [count = state.count] send in
    let fact = try await numberFact(count)
    await send(.factResponse(fact))
} catch: { error, send in
    await send(.factFailed(error))
}
```

**Critical:** Capture state values BEFORE the `.run` closure. You cannot access `state` inside `.run`.

### Fire-and-Forget (no action sent back)

```swift
return .run { _ in
    await analyticsClient.track("button_tapped")
}
```

### Capturing Dependencies

```swift
return .run { [apiClient] send in
    let result = try await apiClient.fetch()
    await send(.fetched(result))
}
```

## Cancellation

### Cancel IDs — Always Use Enum with Cases

```swift
// ✅ Correct
enum CancelID { case request, timer, websocket }
return .run { ... }.cancellable(id: CancelID.request)
return .cancel(id: CancelID.request)

// ❌ NEVER — empty enum types pruned in release builds
enum RequestID {}
```

### Cancel-in-Flight (Debounce/Search)

```swift
return .run { send in
    let results = try await searchClient.search(query)
    await send(.searchResults(results))
}
.cancellable(id: CancelID.search, cancelInFlight: true)
```

### Cross-Scope Cancellation Limitation

`.cancel(id:)` does NOT work across different reducer scopes. Effects are compartmentalized per reducer instance.

```swift
// ✅ To cancel child effects: nil out child state (ifLet auto-cancels)
case .closeTapped:
    state.child = nil  // ifLet automatically cancels all child effects
    return .none
```

### Partial Cancellation with withTaskCancellation

```swift
return .run { send in
    await withTaskCancellation(id: CancelID.timer) {
        for await _ in clock.timer(interval: .seconds(1)) {
            await send(.timerTick)
        }
    }
    // Code here runs even if timer is cancelled
}
```

## Long-Running Effects

### Timer

```swift
@Dependency(\.continuousClock) var clock

case .task:
    return .run { send in
        for await _ in self.clock.timer(interval: .seconds(1)) {
            await send(.timerTick)
        }
    }
    .cancellable(id: CancelID.timer)
```

### WebSocket / Event Stream

```swift
case .connectWebSocket:
    return .run { send in
        let stream = try await webSocketClient.connect(url)
        for try await message in stream {
            await send(.messageReceived(message))
        }
    } catch: { error, send in
        await send(.connectionFailed(error))
    }
    .cancellable(id: CancelID.websocket)
```

### Effect Lifecycle with Navigation

When `@Presents`/`ifLet`/`forEach` is used, effects from child features are automatically cancelled when the child is dismissed. No manual cancellation needed.

If an effect needs to survive child dismissal, fire it from the parent, not the child.

## Effect Anti-Patterns

### Chaining .send in .concatenate

```swift
// ❌ .send completes INSTANTLY — doesn't wait for reducer
return .concatenate(
    .send(.firstAction),
    .send(.secondAction)  // Fires immediately
)
```

### Long-running effect in .concatenate

```swift
// ❌ AsyncStream never completes → second effect never runs
return .concatenate(
    .run { send in for await value in neverEndingStream { ... } },
    .run { send in await send(.willNeverExecute) }
)
```

### Forgotten cancellation without ifLet

```swift
// ❌ If NOT using ifLet, child effects keep running
case .closeTapped:
    state.child = nil
    return .none

// ✅ Explicit cancel (or use ifLet for automatic cancellation)
case .closeTapped:
    state.child = nil
    return .cancel(id: CancelID.childEffect)
```

## Effect Decision Tree

```
What kind of work?
├── Synchronous state mutation only? → return .none
├── Need to send another action? → Use helper function, NOT .send
│   (Exception: delegate actions → return .send(.delegate(...)))
├── Async network call? → .run { send in ... }
├── Multiple independent async tasks? → .merge(effect1, effect2)
├── Sequential async tasks? → .concatenate (or single .run block)
├── Long-running stream? → .run { for await } + .cancellable(id:)
├── Cancel previous request? → .cancellable(id:, cancelInFlight: true)
├── Cancel from elsewhere? → .cancel(id:) (same scope only!)
└── Fire-and-forget? → .run { _ in ... }
```
