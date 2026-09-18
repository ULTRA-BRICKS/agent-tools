# Memory Management & Retain Cycles

Preventing memory leaks and retain cycles in Swift Concurrency code.

## Core Problem

Tasks capture variables like closures. Strong captures + long-lived tasks = memory leaks.

```swift
Task {
    self.doWork()  // ⚠️ Strong capture of self
}
```

## Retain Cycle Pattern

**Cycle**: Task holds `self`, `self` holds task → neither released.

```swift
// BAD: Retain cycle - deinit never called
@MainActor final class UpdateService {
    private var pollingTask: Task<Void, Never>?

    func startPolling() {
        pollingTask = Task {
            while true {
                self.poll()  // Strong capture
                try? await Task.sleep(for: .seconds(5))
            }
        }
    }

    deinit { pollingTask?.cancel() }  // Never called!
}
```

## Breaking Retain Cycles

### Pattern 1: Weak self + loop exit condition

```swift
func startPolling() {
    pollingTask = Task { [weak self] in
        while let self = self {  // Exits when self deallocates
            await self.poll()
            try? await Task.sleep(for: .seconds(5))
        }
    }
}
```

### Pattern 2: Weak self + guard

```swift
func startMonitoring() {
    task = Task { [weak self] in
        for await event in eventStream {
            guard let self = self else { return }
            self.handle(event)
        }
    }
}
```

## One-Way Retention

Task retains `self`, but `self` doesn't retain task. Object stays alive until task completes.

```swift
// OK for short-lived tasks
func saveData() {
    Task {
        await database.save(self.data)  // Strong capture OK - completes quickly
    }
}
```

**When acceptable**: Short tasks that complete quickly.
**When problematic**: Long-running or indefinite tasks.

## Async Sequences and Retention

Infinite sequences are especially dangerous:

```swift
// BAD: Sequence never ends, self never deallocates
func startObserving() {
    task = Task {
        for await _ in NotificationCenter.default.notifications(named: .didBecomeActive) {
            isActive = true  // Strong capture, infinite sequence
        }
    }
}

// GOOD: Weak self + guard exits when self deallocates
func startObserving() {
    task = Task { [weak self] in
        for await _ in NotificationCenter.default.notifications(named: .didBecomeActive) {
            guard let self = self else { return }
            self.isActive = true
        }
    }
}
```

## isolated deinit (Swift 6.2+)

Clean up actor-isolated state in deinit:

```swift
@MainActor final class ViewModel {
    private var task: Task<Void, Never>?

    isolated deinit {
        task?.cancel()
    }
}
```

**Limitation**: Won't break retain cycles (deinit never called if cycle exists).

## Decision Tree

```
Task captures self?
├─ Task completes quickly?
│  └─ Strong capture OK
│
├─ Long-running or infinite?
│  ├─ Can use weak self? → Use [weak self] + while let/guard let
│  ├─ Need manual control? → Store task, cancel explicitly
│  └─ Async sequence? → [weak self] + guard
│
└─ Self owns task?
   ├─ Yes → HIGH RISK of retain cycle
   └─ No → Lower risk, but check lifetime
```

## Detection Strategies

### Add deinit logging

```swift
deinit {
    print("✅ \(type(of: self)) deallocated")
}
```

If deinit never prints → likely retain cycle.

### Unit test pattern

```swift
func testViewModelDeallocates() async {
    var viewModel: ViewModel? = ViewModel()
    weak var weakViewModel = viewModel

    viewModel?.startWork()
    viewModel = nil

    try? await Task.sleep(for: .milliseconds(100))

    XCTAssertNil(weakViewModel, "ViewModel should be deallocated")
}
```

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Strong capture in `while true` | `[weak self]` + `while let self` |
| Strong capture in async sequence | `[weak self]` + `guard let self else return` |
| Not canceling stored tasks | Add `deinit { task?.cancel() }` |
| Assuming deinit breaks cycles | Deinit never called if cycle exists |

## Best Practices

1. **Default to weak self** for long-running tasks
2. **Use guard let self** in async sequences
3. **Cancel tasks explicitly** when possible
4. **Add deinit logging** during development
5. **Test object deallocation** in unit tests
6. **Use Memory Graph** to verify no cycles
7. **Prefer cancellation** over weak self when possible
