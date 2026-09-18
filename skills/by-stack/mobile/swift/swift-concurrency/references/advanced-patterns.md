# Advanced Concurrency Patterns

## How to Use This Reference

Read this when choosing between `Mutex` and actors, deciding on `async let` vs `TaskGroup`, implementing cancellation handlers, working with isolated parameters, or understanding SwiftUI `.task` modifier behavior. Each section addresses a decision point that affects correctness and performance.

---

## Mutex vs Actor Decision 🟢

Use `Mutex<T>` (Swift 6, `import Synchronization`, iOS 18+) when you need **synchronous** access. Use actors when operations involve suspension points or complex state management.

| Criterion | Mutex | Actor |
|-----------|-------|-------|
| Access pattern | Synchronous (no await) | Asynchronous (requires await) |
| Operation duration | Microseconds | Any duration |
| Suspension points inside | ❌ Not possible | ✅ Can await inside |
| Sendable verification | ✅ Compiler-verified | ✅ Compiler-verified |
| Priority inversion | Possible (mitigated by OS) | Handled by runtime |
| Performance | Faster (no context switch) | Slower (executor hop) |
| Reentrancy | Not applicable (synchronous) | ⚠️ Reentrant at await points |
| Availability | iOS 18+ / Swift 6 | iOS 13+ |
| Fallback | `NSLock` (any iOS) | — |

```swift
// Mutex -- synchronous cache with no suspension
import Synchronization

final class Cache: Sendable {
    private let storage = Mutex<[String: Data]>([:])

    func get(_ key: String) -> Data? {
        storage.withLock { $0[key] }
    }

    func set(_ key: String, value: Data) {
        storage.withLock { $0[key] = value }
    }
}

// Actor -- async operations inside
actor NetworkCache {
    private var storage: [URL: Data] = [:]
    private var inFlight: [URL: Task<Data, Error>] = [:]

    func data(for url: URL) async throws -> Data {
        if let cached = storage[url] { return cached }
        if let task = inFlight[url] { return try await task.value }
        let task = Task { try await URLSession.shared.data(from: url).0 }
        inFlight[url] = task
        defer { inFlight[url] = nil }
        let data = try await task.value
        storage[url] = data
        return data
    }
}
```

---

## async let vs TaskGroup Decision 🟢

| Criterion | `async let` | `TaskGroup` |
|-----------|------------|-------------|
| Task count | Fixed at compile time | Dynamic at runtime |
| Return types | Can differ (heterogeneous) | Must be same (homogeneous) |
| Error handling | Left-to-right tuple evaluation | First thrown, first caught |
| Non-awaited tasks | Cancelled then awaited at scope exit | Cancelled then awaited at scope exit |
| Throttling | Not built-in | Manual, but possible |
| Cancellation | Implicit at scope boundary | Manual via group.cancelAll() |

```swift
// async let -- fixed number of heterogeneous tasks
func loadDashboard() async throws -> Dashboard {
    async let user = fetchUser()
    async let orders = fetchOrders()
    async let recommendations = fetchRecommendations()
    return try await Dashboard(user: user, orders: orders, recs: recommendations)
}

// TaskGroup -- dynamic number of homogeneous tasks
func loadImages(urls: [URL]) async -> [UIImage] {
    await withTaskGroup(of: (Int, UIImage?).self) { group in
        for (i, url) in urls.enumerated() {
            group.addTask { (i, try? await loadImage(url)) }
        }
        var results = [UIImage?](repeating: nil, count: urls.count)
        for await (index, image) in group {
            results[index] = image
        }
        return results.compactMap { $0 }
    }
}
```

**Start with `async let`** (simplest). Move to `TaskGroup` for dynamic counts, completion-order processing, or throttling.

---

## Design Non-Sendable First 🟢

Non-Sendable types owned by an actor are locked to that isolation domain — and that's a feature. Entire networks of interconnected classes can work safely within one domain without Sendable gymnastics.

```swift
// PREFER -- non-Sendable class contained in actor
class DocumentParser { // NOT Sendable, NOT a problem
    var state: ParseState = .initial
    func parse(_ data: Data) -> Document { /* mutates state */ }
}

actor DocumentService {
    let parser = DocumentParser() // Owned by actor, isolation-safe

    func processDocument(_ data: Data) -> Document {
        parser.parse(data) // No Sendable needed — same isolation domain
    }
}
```

**Making a class Sendable is rarely the solution.** Design non-Sendable first, add Sendable only when the type genuinely needs to cross isolation boundaries.

---

## Isolated Parameters for Non-Sendable Async Methods 🟢

SE-0313 introduced `isolated` parameters for actor isolation inheritance. Combined with SE-0420's `#isolation` macro, non-Sendable types can participate in concurrency using `isolated (any Actor)? = #isolation`:

```swift
class MyProcessor { // non-Sendable
    private var state = 0

    func process(isolation: isolated (any Actor)? = #isolation) async -> Int {
        state += 1 // Safe: inherits caller's isolation
        return state
    }
}

// Usage from MainActor -- runs on MainActor
@MainActor func handle() async {
    let processor = MyProcessor()
    let result = await processor.process() // Inherits MainActor
}

// Usage from custom actor -- runs on that actor
actor Worker {
    let processor = MyProcessor()
    func doWork() async -> Int {
        await processor.process() // Inherits Worker isolation
    }
}
```

This pattern reduces the need for `@unchecked Sendable` significantly.

---

## @isolated(any) for Dynamic Closure Isolation 🟢

SE-0431 provides `@isolated(any) () async -> T` for function values that carry their actor isolation dynamically. Read isolation with `closure.isolation`.

```swift
// Closure carries its isolation context
func schedule(_ work: @isolated(any) @Sendable () async -> Void) {
    Task {
        await work() // Runs on whatever actor the closure was created on
    }
}

@MainActor func setupUI() {
    schedule {
        // This closure carries @MainActor isolation
        self.label.text = "Updated" // Safe -- runs on MainActor
    }
}
```

`Task.init` now uses this — tasks created with `@MainActor` closures enqueue synchronously on that actor, guaranteeing ordering.

---

## withTaskCancellationHandler Runs Concurrently 🟠

The cancellation handler fires **immediately** if the task is already cancelled when entered. It runs **concurrently** with the main body, not sequentially. Shared state between the handler and body must use locks or atomics — NOT actors.

```swift
// RACE CONDITION -- handler and body access shared state concurrently
actor DownloadManager {
    var downloads: [URL: URLSessionTask] = [:]

    func download(_ url: URL) async throws -> Data {
        try await withTaskCancellationHandler {
            // Body
            let (data, _) = try await URLSession.shared.data(from: url)
            return data
        } onCancel: {
            // Handler runs concurrently -- CANNOT access actor state
            // downloads[url]?.cancel() // Compiler error: actor-isolated
            // Also: URLSession.data(from:) already checks Task.isCancelled,
            // but if you need immediate cancellation, see fix below.
        }
    }
}

// FIX -- use Mutex to share a cancellable handle between body and handler
func download(_ url: URL) async throws -> Data {
    let taskRef = Mutex<URLSessionTask?>(nil)

    return try await withTaskCancellationHandler {
        try await withCheckedThrowingContinuation { continuation in
            let task = URLSession.shared.dataTask(with: url) { data, _, error in
                if let error { continuation.resume(throwing: error); return }
                continuation.resume(returning: data ?? Data())
            }
            taskRef.withLock { $0 = task }
            task.resume()
        }
    } onCancel: {
        taskRef.withLock { $0?.cancel() } // Mutex is safe in synchronous handler
    }
}
```

---

## Cancellation Propagation Rules 🟢

Cancellation propagates only **downward** through structured task trees. A child task being cancelled does NOT cancel the parent.

| Task Type | Cancels Children | Cancelled by Parent | Cancels Siblings |
|-----------|-----------------|--------------------|-----------------|
| `async let` | At scope exit | ✅ Yes | On error (implicit) |
| `TaskGroup` child | Via `cancelAll()` | ✅ Yes | Manual only |
| `DiscardingTaskGroup` child | On child error | ✅ Yes | On error (automatic) |
| `Task { }` (unstructured) | Via `task.cancel()` | ❌ No | N/A |
| `Task.detached { }` | Via `task.cancel()` | ❌ No | N/A |

**Checking cancellation:**
- `Task.isCancelled` — boolean check, no throw
- `try Task.checkCancellation()` — throws `CancellationError`
- Foundation APIs (`URLSession.data(from:)`) check automatically

---

## SwiftUI .task Modifier Inherits MainActor Isolation 🟡

SwiftUI's `.task` modifier inherits `@MainActor` from the `body` getter. Synchronous work inside `.task` runs on the main thread, causing UI hangs. Apple's WWDC23 "Analyze hangs with Instruments" explicitly flags this.

```swift
// HANG -- synchronous work in .task runs on MainActor
struct ContentView: View {
    var body: some View {
        Text("Hello")
            .task {
                let data = try! Data(contentsOf: largeFileURL) // Blocks main thread
                let items = try! JSONDecoder().decode([Item].self, from: data)
                self.items = items
            }
    }
}

// FIX -- call nonisolated async function from .task
struct ContentView: View {
    var body: some View {
        Text("Hello")
            .task {
                let items = await loadItems() // Runs off MainActor
                self.items = items // Back on MainActor for UI update
            }
    }
}

nonisolated func loadItems() async -> [Item] {
    // In Swift 6.2, add @concurrent if this is CPU-intensive
    let data = try! Data(contentsOf: largeFileURL)
    return try! JSONDecoder().decode([Item].self, from: data)
}
```

**Bonus:** `.task(id: value)` cancels and restarts when `value` changes — use for reactive data loading.

---

## Task-Local Values: Metadata Only 🟢

`@TaskLocal` values propagate to child tasks but NOT to detached tasks. They're designed for cross-cutting concerns like request IDs, not for dependency injection.

```swift
enum RequestContext {
    @TaskLocal static var requestID: String?
    @TaskLocal static var userID: String?
}

func handleRequest(id: String, user: String) async {
    RequestContext.$requestID.withValue(id) {
        RequestContext.$userID.withValue(user) {
            Task {
                print(RequestContext.requestID) // "req-123" ✅
                await processOrder()            // requestID propagated to children
            }
            Task.detached {
                print(RequestContext.requestID) // nil ❌ -- detached strips task-locals
            }
        }
    }
}
```

**Use for:** Request tracing, logging correlation, distributed tracing spans, feature flags per-request.
**Do NOT use for:** Dependency injection, configuration, database connections — these should be explicit parameters.

---

## Pattern Selection Cheat Sheet

| Need | Pattern |
|------|---------|
| Protect mutable state, sync access | `Mutex<State>` (iOS 18+) or `NSLock` |
| Protect mutable state, async operations | `actor` |
| UI state management | `@MainActor class` |
| 2-5 parallel fetches with different types | `async let` |
| N parallel operations, same type | `TaskGroup` |
| Long-running service with child tasks | `withDiscardingTaskGroup` |
| Fire-and-forget from sync code | `Task { }` |
| Bridge callback API to async | `withCheckedThrowingContinuation` |
| Cancel ongoing work when condition changes | `withTaskCancellationHandler` |
| Reactive data loading in SwiftUI | `.task(id: value)` |
| Time-dependent async logic | `Clock` protocol injection |
| Cross-cutting metadata (tracing) | `@TaskLocal` |
