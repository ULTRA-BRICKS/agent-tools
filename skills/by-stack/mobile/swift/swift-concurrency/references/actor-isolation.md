# Actor & Isolation Deep Dive

## How to Use This Reference

Read this when choosing isolation strategies, debugging actor-related compiler errors, understanding Task inheritance semantics, or deciding between actors, `@MainActor`, and `Mutex`. Each rule addresses a misconception that has caused production bugs.

---

## Task.init Inherits Actor Isolation 🔴

`Task { }` uses `@_inheritActorContext` — a Task created inside a `@MainActor` class runs its body **on the main thread**. This is the most common Swift Concurrency misconception.

```swift
// BUG -- Task body runs on MainActor, blocks UI
@MainActor final class ViewModel {
    func processImages() {
        Task {
            // This runs on MainActor, NOT on a background thread
            let result = heavyImageProcessing(images) // Blocks UI
            self.result = result
        }
    }
}

// FIX (Swift 6.2+) -- @concurrent guarantees background execution
@MainActor final class ViewModel {
    func processImages() {
        Task {
            let result = await Self.process(images) // Guaranteed off MainActor
            self.result = result // Back on MainActor
        }
    }

    @concurrent static func process(_ images: [UIImage]) async -> ProcessedResult {
        // @concurrent: always runs on the cooperative pool, never on caller's actor.
        // Parameters must be Sendable (crossing isolation boundary).
        heavyImageProcessing(images)
    }
}

// FIX (Swift 6.0/6.1) -- nonisolated static func runs on cooperative pool (pre-6.2 behavior)
@MainActor final class ViewModel {
    func processImages() {
        Task {
            let result = await Self.process(images)
            self.result = result
        }
    }

    nonisolated static func process(_ images: [UIImage]) async -> ProcessedResult {
        // In Swift 6.0/6.1: runs on cooperative pool.
        // WARNING: In Swift 6.2 with NonisolatedNonsendingByDefault, this inherits
        // caller's actor (MainActor), blocking the UI. Use @concurrent instead.
        heavyImageProcessing(images)
    }
}
```

**Key:** `Task.detached { }` breaks inheritance but also strips priority, task-locals, and cancellation propagation. Prefer `nonisolated` functions.

---

## Isolation Is Compile-Time, Not Runtime 🟠

A function's isolation is determined by its **definition**, not by how or where it's called. A function called from `@MainActor` code is NOT `@MainActor`-isolated unless its definition says so. The compiler infers isolation through the chain: method → type → protocol.

```swift
// The compiler knows this is MainActor at the definition site
@MainActor func updateUI() { /* ... */ }

// This is NOT MainActor just because it's called from MainActor context
func computeResult() -> Int { /* ... */ } // nonisolated by default

@MainActor func doWork() {
    updateUI()          // OK -- same isolation
    let x = computeResult() // OK -- nonisolated can be called from anywhere
}
```

When confused about isolation, ask: "What does the compiler know at the **definition** site?"

---

## deinit Is Always nonisolated 🔴

Even for `@MainActor` types, `deinit` runs on **any thread** when the last reference is released. You cannot safely access actor-isolated properties or call UIKit methods in `deinit`.

```swift
// WARNING -- deinit runs on arbitrary thread
@MainActor final class ViewModel: ObservableObject {
    var timer: Timer?
    var observation: NSKeyValueObservation?

    deinit {
        timer?.invalidate()       // May crash -- Timer is MainActor
        observation?.invalidate() // May crash -- KVO thread safety
    }
}

// FIX -- explicit cleanup before deallocation
@MainActor final class ViewModel: ObservableObject {
    var timer: Timer?
    var observation: NSKeyValueObservation?

    func cleanup() {
        timer?.invalidate(); timer = nil
        observation?.invalidate(); observation = nil
    }
    // Call cleanup() from .onDisappear, coordinator, or parent
}
```

**Swift 6.1+:** `isolated deinit` allows running on the actor's executor. For older targets, use explicit cleanup.

---

## Never Split Isolation Across a Single Type 🟠

Don't apply `@MainActor` to individual properties while leaving the type nonisolated. If the instance is created off `@MainActor`, those properties become permanently inaccessible without async hopping.

```swift
// ANTI-PATTERN -- mixed isolation on a single type
class DataManager {
    @MainActor var displayName: String = ""  // MainActor-isolated
    var data: [Item] = []                     // nonisolated

    func process() {
        // Cannot access displayName here without await
        // Cannot easily reason about thread safety
    }
}

// FIX -- isolate the entire type
@MainActor class DataManager {
    var displayName: String = ""
    var data: [Item] = []

    func process() {
        // All properties accessible, isolation is clear
    }
}
```

**Rule:** Apply global actor isolation to the **entire type** or not at all.

---

## Property Wrapper Isolation Inference Removed in Swift 6 🟠

Swift 6 strict concurrency mode removed the rule where `@StateObject` / `@ObservedObject` / `@Published` property wrappers caused the enclosing type to be inferred as `@MainActor`. Code relying on this implicit inference silently loses isolation when migrating to Swift 6.

```swift
// Swift 5 -- implicitly @MainActor because of @StateObject
// Swift 6 -- NOT @MainActor, isolation lost silently
struct ContentView: View {
    @StateObject var viewModel = ContentViewModel()
    // ...
}

// FIX -- explicit @MainActor annotation
@MainActor
struct ContentView: View {
    @StateObject var viewModel = ContentViewModel()
    // ...
}
```

**Action:** After migrating to Swift 6, search for all types using `@StateObject`, `@ObservedObject`, and `@Published` — add explicit `@MainActor` where needed.

---

## MainActor.assumeIsolated for Legacy Callbacks 🟢

For delegate callbacks or framework methods the compiler doesn't know run on the main thread, use `MainActor.assumeIsolated { }`. It crashes at runtime if the assumption is wrong — this is intentional safety.

```swift
// PROBLEM -- compiler doesn't know this runs on main thread
extension ViewController: SomeFrameworkDelegate {
    func frameworkDidComplete(_ result: Result) {
        // Cannot access @MainActor properties without async
        // But we know this delegate is always called on main thread
    }
}

// FIX -- assumeIsolated bridges the knowledge gap
extension ViewController: SomeFrameworkDelegate {
    nonisolated func frameworkDidComplete(_ result: Result) {
        MainActor.assumeIsolated {
            self.label.text = "Done" // Safe -- crashes if not actually on main
        }
    }
}
```

**Caveat:** Non-Sendable data cannot cross the boundary into/out of `assumeIsolated`. Prefer `@MainActor` annotation on the delegate class when possible.

---

## Actors Are Advanced Tools, Not Queue Replacements 🟢

"There is a correlation between people having a bad time with Swift Concurrency and using actors." — Matt Massicotte

**Actors are appropriate when:**
- You have genuinely shared mutable state accessed from multiple isolation domains
- State management involves suspension points (network calls, I/O)
- You need the compiler to enforce isolation

**Actors are NOT appropriate when:**
- The type has no mutable instance properties (use `nonisolated` functions)
- All access is from a single isolation domain (use `@MainActor` or no isolation)
- You need synchronous access (use `Mutex` or `NSLock`)
- You're replacing a serial `DispatchQueue` with no real shared state

```
Decision:
+-- Does the type have shared mutable state?
|   +-- NO  -> Do NOT use actor. Use nonisolated struct/class.
|   +-- YES -> Is all access from UI/MainActor?
|       +-- YES -> @MainActor class (simpler, no await overhead)
|       +-- NO  -> Do operations involve suspension (await)?
|           +-- YES -> actor (correct choice)
|           +-- NO  -> Mutex<State> or NSLock (synchronous, faster)
```

---

## MainActor.run Is Almost Never the Right Solution 🟢

Prefer static `@MainActor` annotations over dynamic `MainActor.run {}`. The dynamic version is runtime synchronization — the compiler cannot verify correctness at all call sites.

```swift
// ANTI-PATTERN -- runtime hop, no compile-time safety
func fetchAndDisplay() async {
    let data = await fetchData()
    await MainActor.run {
        self.label.text = String(data: data, encoding: .utf8)
    }
}

// FIX -- static annotation, compiler-verified
@MainActor func display(_ data: Data) {
    label.text = String(data: data, encoding: .utf8)
}

func fetchAndDisplay() async {
    let data = await fetchData()
    await display(data) // Compiler enforces MainActor at every call site
}
```

**Exception:** `MainActor.run` is acceptable for one-shot hops in truly generic async contexts where you cannot add `@MainActor` to the function signature (e.g., generic middleware).

---

## Isolation Cheat Sheet

| Scenario | Recommended Isolation |
|----------|----------------------|
| SwiftUI View | `@MainActor` (explicit in Swift 6) |
| ViewModel with UI state | `@MainActor class` |
| Shared cache / in-memory store | `actor` |
| Synchronous short critical section | `Mutex<State>` (iOS 18+) or `NSLock` |
| Stateless utility functions | `nonisolated` (default) |
| Network layer / repository | `nonisolated` + `Sendable` structs |
| Database wrapper | `@ModelActor` (SwiftData) or custom actor |
| Singleton with mutable state | `actor` or `@MainActor` (if UI-only) |
| Protocol conformance | Match the protocol's isolation requirement |
