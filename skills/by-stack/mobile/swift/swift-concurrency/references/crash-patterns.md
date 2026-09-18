# Production Crash Patterns

## How to Use This Reference

When diagnosing crash reports, Watchdog kills, or hangs in concurrent code, match the crash signature to a pattern below. Every pattern includes the crash mechanism, a broken code example, and the fix. For every violation found in a code review, add it to the feature's `refactoring/` plan with the listed severity.

---

## Pattern 1: Continuation Never Resumed 🟡

A `withCheckedContinuation` that is never resumed suspends the calling task **forever**. The checked variant logs `SWIFT TASK CONTINUATION MISUSE: leaked its continuation!` but does not crash — this is a **silent hang**. The most common cause is completion-handler code paths that `return` early without calling the callback.

```swift
// BUG -- error path never resumes continuation; caller hangs forever
func fetchUser(id: String) async throws -> User {
    try await withCheckedThrowingContinuation { continuation in
        legacyFetch(id: id) { result in
            switch result {
            case .success(let user):
                continuation.resume(returning: user)
            case .failure:
                break // BUG: continuation leaked
            }
        }
    }
}

// FIX -- resume with Result on every path
func fetchUser(id: String) async throws -> User {
    try await withCheckedThrowingContinuation { continuation in
        legacyFetch(id: id) { result in
            continuation.resume(with: result) // Handles both cases
        }
    }
}
```

**Detection:** Search for `withCheckedContinuation` and `withCheckedThrowingContinuation`. Verify every branch inside the closure calls `resume`. Pay special attention to `guard`, early `return`, and `catch` blocks.

---

## Pattern 2: Continuation Double-Resume 🔴

`CheckedContinuation.resume()` called twice triggers an immediate `EXC_BREAKPOINT (SIGTRAP)` with message `SWIFT TASK CONTINUATION MISUSE`. `UnsafeContinuation` causes undefined behavior instead — silent corruption. Third-party libraries (SDWebImage, Auth0, RxSwift bridging) sometimes call completion handlers more than once.

```swift
// CRASH -- third-party SDK calls completion twice on retry
func loadImage(url: URL) async -> UIImage? {
    await withCheckedContinuation { continuation in
        thirdPartyLoad(url: url) { image in
            continuation.resume(returning: image) // Called twice on retry = crash
        }
    }
}

// FIX -- guard with a flag (use lock for thread safety)
func loadImage(url: URL) async -> UIImage? {
    await withCheckedContinuation { continuation in
        let resumed = NSLock()
        var didResume = false
        thirdPartyLoad(url: url) { image in
            resumed.lock()
            defer { resumed.unlock() }
            guard !didResume else { return }
            didResume = true
            continuation.resume(returning: image)
        }
    }
}
```

**Rules:**
- Always use `withChecked*` unless profiling proves the check is a bottleneck
- `withUnsafe*` double-resume is undefined behavior — no crash, no warning, silent corruption

---

## Pattern 3: Cooperative Pool Deadlock 🟡

`DispatchSemaphore.wait()`, `DispatchGroup.wait()`, `DispatchQueue.sync`, `NSCondition`, `pthread_cond_wait`, `Thread.sleep()`, and synchronous file I/O inside any `async` function or Task will eventually deadlock. The cooperative thread pool is limited to **CPU core count** (4–10 threads on iPhones). Blocking even one thread while waiting on async work that needs the same pool is guaranteed to deadlock under load.

```swift
// DEADLOCK -- blocks cooperative thread, pool starves
func process() async -> Data {
    let semaphore = DispatchSemaphore(value: 0)
    Task { fetchData(); semaphore.signal() }
    semaphore.wait() // Blocks cooperative thread
    return data
}

// FIX -- use continuation to bridge callback
func process() async -> Data {
    await withCheckedContinuation { continuation in
        fetchDataWithCallback { data in
            continuation.resume(returning: data)
        }
    }
}
```

**Test exposure:** Set environment variable `LIBDISPATCH_COOPERATIVE_POOL_STRICT=1` in your test scheme. This forces 1 cooperative thread per priority, exposing deadlocks that only appear under load.

---

## Pattern 4: TaskGroup Memory Explosion 🟡

Spawning more child tasks than CPU cores when each task calls a framework that internally blocks (Vision's `VNImageRequestHandler.perform()`, some Core ML inference calls) deadlocks the cooperative pool. Even without blocking, unbounded `addTask` accumulates completed results in memory until iterated.

```swift
// OOM -- 10,000 tasks queued, results accumulate in memory
await withTaskGroup(of: ProcessedItem.self) { group in
    for item in largeDataset {
        group.addTask { await process(item) }
    }
    for await result in group { collect(result) }
}

// FIX -- throttle with sliding window (use withThrowingTaskGroup if process() can throw)
await withTaskGroup(of: ProcessedItem.self) { group in
    let limit = ProcessInfo.processInfo.activeProcessorCount
    for (i, item) in largeDataset.enumerated() {
        if i >= limit { await group.next().map { collect($0) } } // Wait for a slot
        group.addTask { await process(item) }
    }
    for await result in group { collect(result) }
}
```

---

## Pattern 5: Opaque Framework Blocking Calls 🟡

Apple's own frameworks sometimes internally block. GCD will not spawn new threads to rescue cooperative threads blocked inside closed-source framework calls.

| Framework | Blocking API | Workaround |
|-----------|-------------|------------|
| Vision | `VNImageRequestHandler.perform()` | Wrap in `DispatchQueue.global()` + continuation |
| Core ML | `MLModel.prediction(from:)` (some models) | Dispatch to dedicated queue |
| FileManager | `contentsOfDirectory(atPath:)` on network volumes | Use `DispatchIO` or background queue |
| Data | `Data(contentsOf: fileURL)` | Use `FileHandle` with async reads |

```swift
// FIX: Wrap suspect framework call in a dedicated queue
func performVisionRequest(_ request: VNRequest, on image: CGImage) async throws -> [VNObservation] {
    try await withCheckedThrowingContinuation { continuation in
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                let handler = VNImageRequestHandler(cgImage: image)
                try handler.perform([request])
                continuation.resume(returning: request.results ?? [])
            } catch {
                continuation.resume(throwing: error)
            }
        }
    }
}
```

---

## Pattern 6: Actor Reentrancy State Corruption 🟠

Actors guarantee mutual exclusion only for **synchronous** code segments. When an actor method hits `await`, other calls interleave. The classic pattern: check a condition, await an operation, act on the condition — but it changed during the suspension.

```swift
// BUG -- two concurrent calls both pass the check
actor WalletActor {
    var balance: Decimal = 100
    func withdraw(_ amount: Decimal) async throws {
        guard balance >= amount else { throw InsufficientFunds() }
        let approved = await paymentGateway.authorize(amount) // Reentrancy window
        balance -= amount // Both calls deduct — balance goes negative
    }
}

// FIX -- coalesce in-flight operations, re-check after await
actor WalletActor {
    var balance: Decimal = 100
    private var pendingWithdrawals: Decimal = 0
    func withdraw(_ amount: Decimal) async throws {
        let available = balance - pendingWithdrawals
        guard available >= amount else { throw InsufficientFunds() }
        pendingWithdrawals += amount
        defer { pendingWithdrawals -= amount }
        let approved = await paymentGateway.authorize(amount)
        guard approved else { throw PaymentDeclined() }
        balance -= amount
    }
}
```

---

## Pattern 7: MainActor Watchdog Kill 🔴

The MainActor IS the main thread. Synchronous work exceeding ~5 seconds at launch or ~10 seconds during normal operation triggers a `SIGKILL` from the iOS watchdog (exception code `0x8badf00d`). Core Data fetches, JSON parsing, and image decoding on `@MainActor` types are the usual culprits.

```swift
// WATCHDOG KILL -- heavy work on MainActor
@MainActor final class ViewModel {
    func loadData() async {
        let data = try! Data(contentsOf: largeFileURL) // Sync I/O on main thread
        let items = try! JSONDecoder().decode([Item].self, from: data) // CPU-bound on main
        self.items = items
    }
}

// FIX -- move heavy work off MainActor with @concurrent static method
@MainActor final class ViewModel {
    func loadData() async throws {
        let items = try await Self.parseItems(from: largeFileURL)
        self.items = items // Back on MainActor for UI update
    }

    // @concurrent ensures this runs on the cooperative pool, not the caller's actor.
    // In Swift <6.2, use `nonisolated static func` instead of @concurrent.
    @concurrent static func parseItems(from url: URL) async throws -> [Item] {
        let (data, _) = try await URLSession.shared.data(from: url)
        return try JSONDecoder().decode([Item].self, from: data)
    }
}
```

---

## Pattern 8: SwiftData / Core Data Threading Violations 🔴

`ModelContext` and managed objects accessed across isolation domains cause sporadic `EXC_BAD_ACCESS (code=1)` crashes that vanish under Address Sanitizer — nearly impossible to debug.

**Rule:** Create a dedicated `ModelContext` per actor. Never pass `ModelContext` or model objects across isolation boundaries.

```swift
// CRASH -- ModelContext created on MainActor, accessed from background
@MainActor let context = ModelContext(container)
Task.detached {
    let items = try context.fetch(FetchDescriptor<Item>()) // Wrong isolation
}

// FIX -- @ModelActor for background context
@ModelActor actor BackgroundPersistence {
    func fetchItems() throws -> [Item] {
        try modelContext.fetch(FetchDescriptor<Item>()) // Own context, own isolation
    }
}
```

---

## Pattern 9: Release-Only Actor Crashes 🔴

Some actor-isolated array access patterns crash with `EXC_BAD_ACCESS` only in `-O` (optimized) builds, never in debug. Custom future/promise implementations using actor-isolated arrays of continuations are especially vulnerable — the optimizer can reorder or eliminate intermediate copies.

**Mitigation:**
- Prefer `AsyncStream`, `AsyncChannel` (swift-async-algorithms) over hand-rolled actor-based futures
- Run CI tests with both debug AND release configurations
- Enable TSan and ASan in separate CI runs (they cannot run simultaneously)
- Use `LIBDISPATCH_COOPERATIVE_POOL_STRICT=1` in test schemes

---

## Detection Checklist

When reviewing code for crash-level concurrency bugs:

- [ ] Every `withChecked*Continuation` resumes on **every** code path (success, failure, early return, cancellation)
- [ ] No `DispatchSemaphore.wait()`, `DispatchGroup.wait()`, `Thread.sleep()` in any async context
- [ ] TaskGroup child task count bounded for large datasets
- [ ] No opaque framework calls (Vision, CoreML) directly in async context without queue wrapping
- [ ] Actor methods re-check state after every `await`
- [ ] No synchronous heavy work (>100ms) on `@MainActor`
- [ ] `ModelContext` not shared across isolation domains
- [ ] CI runs include release configuration tests
