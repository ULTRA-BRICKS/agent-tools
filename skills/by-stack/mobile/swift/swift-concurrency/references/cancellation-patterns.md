# Cancellation Patterns

Cancellation in Swift concurrency is **cooperative**. Setting the cancelled flag does nothing unless running code checks it.

## How Cancellation Propagates

- **Structured concurrency**: Cancelling a parent task cancels all children
- **Task groups**: Cancelling a group cancels all child tasks
- **Unstructured tasks**: `Task {}` and `Task.detached {}` must be cancelled explicitly via stored handle
- **SwiftUI .task**: Cancels automatically when view disappears (prefer over `onAppear` + `Task`)

## Checking for Cancellation

Use inside long-running or looping async work:

```swift
// In throwing contexts (preferred)
func processAll(_ items: [Item]) async throws {
    for item in items {
        try Task.checkCancellation()  // Throws CancellationError if cancelled
        try await process(item)
    }
}

// In non-throwing contexts
func processAll(_ items: [Item]) async {
    for item in items {
        guard !Task.isCancelled else { return }
        await process(item)
    }
}
```

**Important**: CPU-bound loops with no `await` will never see cancellation unless you check explicitly.

## withTaskCancellationHandler

Bridges Swift cancellation to legacy APIs with their own cancel mechanism:

```swift
func observe() async throws -> [Change] {
    let operation = CKQueryOperation(query: query)

    return try await withTaskCancellationHandler {
        try await performOperation(operation)
    } onCancel: {
        operation.cancel()  // Called immediately when task is cancelled
    }
}
```

**Note**: `onCancel` fires immediately on any thread, even while async body is suspended.

**Critical constraint:** The `onCancel` closure signature is `@Sendable () -> Void` — it is **synchronous** and cannot call `async` functions. It may execute on a different thread from the async body. Any shared mutable state between the handler and body requires a `Mutex` or atomic operation — actors are not accessible from a synchronous closure.

## Common Cancellation Bugs

### Bug: Catching and ignoring CancellationError

```swift
// BAD: Shows error alert for normal lifecycle event
do {
    try await loadData()
} catch {
    showAlert(error.localizedDescription)  // "The operation was cancelled" 😬
}

// GOOD: Filter out CancellationError
do {
    try await loadData()
} catch is CancellationError {
    // Normal - view disappeared or task cancelled. Do nothing.
} catch {
    showAlert(error.localizedDescription)
}
```

### Bug: Forgetting to cancel stored tasks

```swift
// BAD: Task keeps running after object is done with it
class ViewModel {
    var loadTask: Task<Void, Never>?

    func load() {
        loadTask = Task { await fetchData() }
    }
}

// GOOD: Cancel previous task and on teardown
class ViewModel {
    var loadTask: Task<Void, Never>?

    func load() {
        loadTask?.cancel()  // Cancel previous
        loadTask = Task { await fetchData() }
    }

    deinit {
        loadTask?.cancel()
    }
}
```

### Bug: No cancellation checks in CPU-bound work

```swift
// BAD: Tight loop runs to completion even if cancelled
func processImages(_ images: [UIImage]) async -> [UIImage] {
    images.map { processHeavy($0) }  // No suspension points, no cancellation check
}

// GOOD: Check periodically
func processImages(_ images: [UIImage]) async throws -> [UIImage] {
    var results: [UIImage] = []
    for image in images {
        try Task.checkCancellation()  // Check before each heavy operation
        results.append(processHeavy(image))
    }
    return results
}
```

## SwiftUI .task Cancellation

```swift
struct ProfileView: View {
    @State private var profile: Profile?
    @State private var error: Error?

    var body: some View {
        content
            .task {
                do {
                    profile = try await api.loadProfile()
                } catch is CancellationError {
                    // View disappeared - normal, do nothing
                } catch {
                    self.error = error
                }
            }
    }
}
```

## Timeout Pattern

Wrap work with a timeout to prevent infinite hangs:

```swift
func fetchWithTimeout<T>(_ work: @escaping () async throws -> T, timeout: Duration) async throws -> T {
    try await withThrowingTaskGroup(of: T.self) { group in
        group.addTask { try await work() }
        group.addTask {
            try await Task.sleep(for: timeout)
            throw TimeoutError()
        }

        let result = try await group.next()!
        group.cancelAll()  // Cancel the other task
        return result
    }
}
```

## Cancellation Checklist

- [ ] Long-running loops check `Task.checkCancellation()` or `Task.isCancelled`
- [ ] Stored `Task` handles are cancelled in `deinit` or cleanup method
- [ ] `CancellationError` is caught and ignored (not shown to users)
- [ ] Legacy APIs with cancel methods use `withTaskCancellationHandler`
- [ ] SwiftUI views use `.task` modifier (auto-cancellation) over `onAppear` + `Task`
