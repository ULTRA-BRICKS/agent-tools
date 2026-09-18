# Swift Version Migration & 6.2 Changes

## How to Use This Reference

Read this when planning a Swift version migration (5.x → 6.0 → 6.2), preparing for behavioral changes in newer Swift versions, encountering unexpected main-thread execution in `nonisolated async` functions, or understanding the `@concurrent` annotation. Covers the two major migration paths that matter: **Swift 5.x → 6.0** (strict concurrency becomes enforced) and **Swift 6.0 → 6.2** (Approachable Concurrency changes runtime behavior silently).

---

## Migration Path: Swift 5.x → Swift 6.0

The biggest shift in Swift's history for concurrent code. Strict concurrency checking moves from opt-in warnings to enforced errors.

**What changes:**
- `SWIFT_STRICT_CONCURRENCY=complete` becomes the default — all Sendable violations are errors
- Global mutable state (`static var`, global `var`) requires isolation annotation
- Protocol conformances across isolation boundaries are checked
- `@Sendable` closures are enforced at all concurrency boundaries
- `sending` parameter (SE-0430) and region-based isolation (SE-0414) reduce false positives

**Recommended migration steps:**
1. Stay on Swift 5.x, enable `SWIFT_STRICT_CONCURRENCY=targeted` — fix warnings only in code using concurrency
2. Upgrade to `SWIFT_STRICT_CONCURRENCY=complete` — fix ALL warnings, including non-async code
3. Switch to Swift 6 language mode — warnings become errors

**Key Swift 6.0 features that help migration:**
- `sending` keyword eliminates many false Sendable requirements
- Region-based isolation tracks value ownership — freshly created values can cross boundaries
- `Mutex<State>` (iOS 18+) provides compiler-verified synchronous locking
- `isolated deinit` (Swift 6.1) allows cleanup on the actor's executor

---

## Migration Path: Swift 6.0 → Swift 6.2

Behavioral changes — code that compiled and ran correctly in Swift 6.0 may **silently change execution context** in Swift 6.2.

**What changes:**
- `nonisolated async` functions now run on the caller's actor (not the cooperative pool)
- `@preconcurrency` conformances crash at runtime (were warnings before)
- New opt-in: `defaultIsolation: MainActor` makes all code `@MainActor` by default
- `@concurrent` annotation explicitly opts into background execution
- `nonisolated` can be applied to types to block isolation inference

**Recommended migration steps:**
1. Enable `NonisolatedNonsendingByDefault` feature flag on one target
2. Run full test suite — watch for main-thread hangs (CPU work that used to run in background)
3. Add `@concurrent` to functions that need background execution
4. Test all `@preconcurrency` conformance call paths for runtime crashes
5. Roll out to remaining targets

---

## Swift 6.2 Behavioral Changes (Detail)

### nonisolated async Functions Now Run on Caller's Actor 🔴

**This is the most impactful Swift 6.2 change.** Previously (Swift 6.0/6.1), `nonisolated func doWork() async` always hopped to the global concurrent executor (cooperative pool). With `NonisolatedNonsendingByDefault` (SE-0461), it runs on the **caller's actor**.

A `nonisolated async` function called from `@MainActor` now stays on the main thread. Heavy CPU work that previously ran in the background will now silently block the UI.

```swift
// Swift 6.0/6.1 -- runs on cooperative pool (background)
// Swift 6.2    -- runs on CALLER'S actor (MainActor if called from MainActor)
nonisolated func processData(_ data: Data) async -> Result {
    // JSON decoding, image processing, etc.
    // In 6.2: this blocks the main thread if called from @MainActor!
}

@MainActor func handleTap() async {
    let result = await processData(payload) // 6.2: runs on main thread!
    display(result)
}
```

**Why this changed:** The old behavior required non-Sendable values to be transferred across isolation boundaries for every nonisolated async call. The new default (`nonisolated(nonsending)`) eliminates unnecessary hops and Sendable requirements — the function stays on the caller's executor, so no boundary crossing occurs.

**Impact checklist:**
- [ ] Audit all `nonisolated async` functions called from `@MainActor`
- [ ] Functions doing CPU-intensive work (>1ms) need `@concurrent`
- [ ] Functions doing I/O that's already async are fine (they suspend, not block)

---

### Use @concurrent for Explicit Background Execution 🟢

In Swift 6.2+, `@concurrent` replaces the old `nonisolated` behavior — it explicitly opts the function into running on the global concurrent executor (cooperative pool), regardless of the caller's isolation.

```swift
// BEFORE (Swift 6.0) -- nonisolated async always ran on background
nonisolated func decodeImage(_ data: Data) async -> UIImage { /* ... */ }

// AFTER (Swift 6.2) -- @concurrent explicitly requests background execution
@concurrent func decodeImage(_ data: Data) async -> UIImage {
    // Always runs on cooperative pool, even when called from @MainActor
    // Parameters must be Sendable (crossing isolation boundary)
}
```

**When to use @concurrent:**
- CPU-bound work: JSON decoding, image processing, cryptographic operations
- I/O wrapping: bridging synchronous I/O with continuations
- Any function that previously relied on nonisolated-means-background behavior

**When NOT to use @concurrent:**
- Functions that just forward calls to other async functions (unnecessary hop)
- Functions operating on non-Sendable types (won't compile — `@concurrent` requires Sendable parameters)
- Library code that should be flexible about execution context

---

### MainActor Is the Default Isolation in New Xcode 26 Projects 🟢

SE-0466 introduces `defaultIsolation: MainActor.self` at the module level. With this enabled, ALL unannotated functions and types are implicitly `@MainActor`.

```swift
// With -default-isolation MainActor:

struct SettingsView: View { /* implicitly @MainActor */ }
class DataManager { /* implicitly @MainActor */ }
func helper() { /* implicitly @MainActor */ }

// Opt out explicitly:
nonisolated class NetworkClient { /* NOT @MainActor */ }
nonisolated func compute() { /* NOT @MainActor */ }
```

**Guidelines:**
- Use `defaultIsolation: MainActor.self` for **app targets** — most app code is UI-connected
- Keep the default `nonisolated` for **library targets** — libraries should not assume caller context
- Imported module code is NOT affected by the importing module's default isolation
- Use `nonisolated` explicitly on types/functions that must run off the main thread

---

### @preconcurrency Conformances Now Crash at Runtime 🔴

SE-0423 added **runtime assertions** for actor isolation starting in **Swift 6.0**. `@preconcurrency` conformances are no longer just compile-time suppressions — if a `@MainActor` method satisfying a nonisolated protocol requirement is called from the wrong actor, the runtime checks isolation. Enforcement has been progressively stricter across versions: Swift 6.0 introduced runtime warnings/assertions, and Swift 6.2 escalates remaining cases to hard crashes.

```swift
protocol DataProvider {
    func fetchData() -> Data // nonisolated requirement
}

@MainActor class AppDataProvider: @preconcurrency DataProvider {
    func fetchData() -> Data {
        // @MainActor method satisfying nonisolated requirement
        // Swift 6.0: runtime assertion (may warn or crash depending on context)
        // Swift 6.1: expanded enforcement scope
        // Swift 6.2: all remaining cases become hard crashes
        return cachedData
    }
}

// This will crash in Swift 6.2:
Task.detached {
    let provider: DataProvider = AppDataProvider()
    let data = provider.fetchData() // Crash: called @MainActor method from wrong executor
}
```

**Workarounds:**
- `SWIFT_IS_CURRENT_EXECUTOR_LEGACY_MODE_OVERRIDE=nocrash` — downgrades crash to warning (temporary)
- Redesign the protocol to be actor-aware: `func fetchData() async -> Data`
- Move the implementation to a nonisolated method
- Use `nonisolated(unsafe)` only with documented thread-safety guarantees

**Action:** Test ALL `@preconcurrency` conformance call paths after migrating to Swift 6.2.

---

### nonisolated on Types Blocks Unwanted @MainActor Inference 🟢

SE-0449 lets you write `nonisolated struct S: SomeMainActorProtocol { ... }` to cut off global actor inference from protocol conformances. Previously, conforming to a `@MainActor` protocol made ALL members `@MainActor`, requiring individual `nonisolated` annotations.

```swift
// BEFORE -- conforming to @MainActor protocol infects entire type
@MainActor protocol ViewModelProtocol {
    func update()
}

struct MyViewModel: ViewModelProtocol {
    // ALL members are now @MainActor due to protocol inference
    func update() { /* @MainActor */ }
    func compute() { /* Also @MainActor -- unintentional! */ }
}

// AFTER -- nonisolated cuts off inference
nonisolated struct MyViewModel: ViewModelProtocol {
    func update() { /* nonisolated -- must be explicitly @MainActor if needed */ }
    func compute() { /* nonisolated -- as intended */ }
}
```

Also works on extensions: `nonisolated extension MyType { ... }`

---

## Swift Version Compatibility Table

| Feature | Swift 5.10 | Swift 6.0 | Swift 6.1 | Swift 6.2 |
|---------|-----------|-----------|-----------|-----------|
| Strict concurrency checking | Opt-in (complete) | Default (complete) | Default | Default |
| `sending` parameter | — | ✅ SE-0430 | ✅ | ✅ |
| Region-based isolation | — | ✅ SE-0414 | ✅ | ✅ |
| `nonisolated(nonsending)` default | — | — | — | ✅ SE-0461 |
| `@concurrent` annotation | — | — | — | ✅ SE-0461 |
| `defaultIsolation: MainActor` | — | — | — | ✅ SE-0466 |
| `nonisolated` on types | — | — | — | ✅ SE-0449 |
| Runtime isolation assertions | — | ✅ SE-0423 | Enhanced | Enforced |
| `isolated deinit` | — | — | ✅ | ✅ |
| `@preconcurrency` runtime crash | — | Assert (partial) | Assert (expanded) | Crash (all cases) |
| `Mutex` (Synchronization) | — | ✅ (iOS 18+) | ✅ | ✅ |

## Migration Checklist for Swift 6.2

- [ ] Enable `NonisolatedNonsendingByDefault` feature flag and test for main-thread hangs
- [ ] Add `@concurrent` to all CPU-intensive `nonisolated async` functions
- [ ] Test all `@preconcurrency` conformances for runtime crashes
- [ ] Decide on `defaultIsolation` for each target (MainActor for apps, nonisolated for libraries)
- [ ] Review `nonisolated` type annotations to block unwanted inference
- [ ] Run full test suite with new isolation behavior
