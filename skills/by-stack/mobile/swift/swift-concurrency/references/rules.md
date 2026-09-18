# Swift Concurrency — Rules Quick Reference

## Do's — Always Follow

1. **Enable strict concurrency checking** — `SWIFT_STRICT_CONCURRENCY=complete` in build settings. Warnings now become errors in Swift 6 mode. (`references/compiler-flags-ci.md`)
2. **Resume every continuation exactly once on every code path** — use `withCheckedThrowingContinuation` (not Unsafe). Missing resume = permanent hang. Double resume = `EXC_BREAKPOINT`. (`references/crash-patterns.md`)
3. **Use actors for shared mutable state, @MainActor for UI state** — actors provide compile-time isolation. Never access actor-isolated state without `await` or `assumeIsolated`. (`references/actor-isolation.md`)
4. **Prefer Mutex for synchronous short critical sections** — actors force async access. `Mutex<State>` (Swift 6+, iOS 18+) or `NSLock` allows synchronous lock without suspension. (`references/advanced-patterns.md`)
5. **Use withDiscardingTaskGroup for fire-and-forget child tasks** — regular TaskGroup accumulates child task results in memory until iterated. Discarding variant releases immediately. (`references/asyncstream-memory.md`)
6. **Always call continuation.finish() on AsyncStream** — use `onTermination` handler to detect consumer cancellation. Missing `finish()` leaks the stream and all captured resources. (`references/asyncstream-memory.md`)
7. **Inject Clock protocol for time-dependent code** — `ContinuousClock` in production, `ImmediateClock` or custom `TestClock` in tests. Never use `Task.sleep(nanoseconds:)` directly. (`references/testing-debugging.md`)
8. **Mark public types with explicit Sendable conformance** — the compiler does not infer Sendable for public types even if all stored properties are Sendable. (`references/sendable-transfer.md`)

## Don'ts — Critical Anti-Patterns

> Severity: 🔴 crash/data loss, 🟠 data race/corruption, 🟡 hang/deadlock, 🟢 best practice violation

### Never: Block the cooperative thread pool 🟡

```swift
// DEADLOCK -- cooperative pool has only CPU-core-count threads (4-10 on iPhones)
func fetchSync() async -> Data {
    let semaphore = DispatchSemaphore(value: 0)
    URLSession.shared.dataTask(with: url) { data, _, _ in
        result = data; semaphore.signal()
    }.resume()
    semaphore.wait() // Blocks cooperative thread; pool starves under load
}

// FIX: Use continuation
func fetch() async throws -> Data {
    try await withCheckedThrowingContinuation { continuation in
        URLSession.shared.dataTask(with: url) { data, _, error in
            if let error { continuation.resume(throwing: error); return }
            continuation.resume(returning: data!)
        }.resume()
    }
}
```

### Never: Use @unchecked Sendable to silence compiler 🟠

```swift
// RUNTIME CRASH -- compiler trusts you, data race at runtime
final class TokenStore: @unchecked Sendable {
    var token: String = "" // No synchronization
}

// FIX: Use actor for async access or Mutex for sync access
actor TokenStore {
    private var token: String = ""
    func update(_ newToken: String) { token = newToken }
    func current() -> String { token }
}
```

### Never: Assume state unchanged after await in actor 🟠

```swift
// BUG -- actor reentrancy: state changes between check and use
actor Cache {
    var store: [URL: Data] = [:]
    func data(for url: URL) async -> Data {
        if store[url] == nil {
            store[url] = await download(url) // Duplicate downloads
        }
        return store[url]!
    }
}

// FIX: Re-check after await, coalesce in-flight requests
actor Cache {
    var store: [URL: Data] = [:]
    private var inFlight: [URL: Task<Data, Error>] = [:]
    func data(for url: URL) async throws -> Data {
        if let cached = store[url] { return cached }
        if let task = inFlight[url] { return try await task.value }
        let task = Task { try await download(url) }
        inFlight[url] = task
        defer { inFlight[url] = nil }
        let data = try await task.value
        store[url] = data
        return data
    }
}
```

### Never: Access @MainActor state from deinit 🔴

```swift
// WARNING -- deinit is always nonisolated, runs on any thread
@MainActor final class ViewModel {
    var timer: Timer?
    deinit { timer?.invalidate() } // Compiler warning in Swift 6
}

// FIX: Explicit cleanup method called from .onDisappear or coordinator
@MainActor final class ViewModel {
    var timer: Timer?
    func cleanup() { timer?.invalidate(); timer = nil }
}
```

### Never: Use MainActor.run {} for isolation 🟢

```swift
// ANTI-PATTERN -- runtime hop, compiler cannot verify isolation
func update() async {
    await MainActor.run { label.text = "Done" }
}

// FIX: Static @MainActor annotation -- compiler-verified
@MainActor func update() {
    label.text = "Done"
}
```
