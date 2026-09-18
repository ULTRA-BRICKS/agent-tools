# Enterprise Concurrency Migration

## How to Use This Reference

Read this when migrating a codebase to `SWIFT_STRICT_CONCURRENCY=complete` or Swift 6 language mode, planning a multi-module migration, dealing with third-party SDK Sendable blockers, or converting global mutable state to concurrency-safe patterns.

---

## Migrate Bottom-Up by Module Dependency Graph 🟢

Enabling strict concurrency on a high-level module instantly generates **thousands of warnings** that cascade from upstream modules. The only viable approach: topological sort of module dependencies, then migrate leaf modules first.

```text
Migration Order (bottom-up):
1. Foundation extensions / utilities (no internal dependencies)
2. Networking / API layer
3. Domain models / business logic
4. Persistence layer
5. Feature modules
6. App target (last — depends on everything)
```

**Strategy per module:**
1. Enable `SWIFT_STRICT_CONCURRENCY=targeted` — shows warnings only for code that uses concurrency
2. Fix all `targeted` warnings
3. Upgrade to `SWIFT_STRICT_CONCURRENCY=complete` — shows warnings for all code
4. Fix all `complete` warnings
5. Enable Swift 6 language mode: set `SWIFT_VERSION = 6` in Xcode build settings (or `swift-language-version: .version("6")` in Package.swift). Note: `SWIFT_UPCOMING_FEATURE_FLAGS = StrictConcurrency` is the opt-in flag for Swift 5 mode — it is NOT needed once `SWIFT_VERSION = 6` because strict concurrency is the default.

**Real-world scale:** Telefonica (1M+ LOC, 24 modules) confirmed this is the only viable approach. Apple's WWDC24 session 10169 recommends the same.

---

## Never Use @unchecked Sendable as a Migration Shortcut 🟠

`@unchecked Sendable` provides **zero runtime thread-safety guarantees** — it merely silences the compiler. At scale, these silent escape hatches accumulate into the same data races Swift 6 is designed to prevent.

| Instead of @unchecked Sendable | Use |
|-------------------------------|-----|
| Class with internal mutable state | `actor` or `Mutex<State>` wrapper |
| Third-party type not yet Sendable | `@preconcurrency import` (temporary) |
| Value being transferred once | `sending` parameter (SE-0430) |
| Immutable reference type | Verify all properties are `let` + Sendable; file compiler bug if needed |
| Protocol requirement mismatch | Redesign protocol with `async` or `sending` |

**Airbnb's Swift Style Guide explicitly prohibits** `@unchecked Sendable` as a crutch. Treat every usage as tech debt requiring documented justification and a removal plan.

---

## Plan for Third-Party SDK Blockers 🟢

Many widely-used SDKs lack Sendable conformance. One non-compliant SDK can block migration of entire modules.

**Strategies (in priority order):**

1. **Check for updates** — SDK may have added Sendable in a recent release
2. **`@preconcurrency import`** — suppresses warnings for that module's types (temporary)
3. **Actor wrapper** — isolate SDK interactions behind a dedicated actor
4. **Protocol abstraction** — define your own Sendable protocol, adapt SDK types
5. **Contact vendor** — request Sendable conformance timeline

```swift
// Actor wrapper for non-Sendable SDK
actor AnalyticsActor {
    private let tracker: ThirdPartyTracker // non-Sendable, owned by actor

    init() { tracker = ThirdPartyTracker() }

    func track(event: String, params: [String: String]) {
        tracker.log(event, parameters: params) // Safe -- isolated
    }
}
```

**Track blockers** in `refactoring/cross-cutting.md` with SDK name, current version, Sendable status, and expected update timeline.

---

## Global Mutable Singletons Are the #1 Warning Source 🟠

Swift 6 flags every mutable `static var` or global `var`. Enterprise apps with hundreds of singletons generate thousands of warnings.

**Fix priority order:**

| Pattern | Fix | Example |
|---------|-----|---------|
| `static var` that is actually constant | `static let` | `static let shared = Manager()` |
| Singleton with UI-only access | `@MainActor static let` | `@MainActor static let shared = UIManager()` |
| Singleton with multi-domain access | Convert to `actor` | `actor ConfigStore { static let shared = ConfigStore() }` |
| Singleton with synchronous access needs | `Mutex<State>` | `static let shared = Mutex(initialState)` |
| Configuration set once at launch | `nonisolated(unsafe)` + documented | `nonisolated(unsafe) static var config: Config!` |

```swift
// BEFORE -- global mutable state, Swift 6 error
class AppState {
    static var shared = AppState()
    var isLoggedIn = false  // Data race potential
}

// AFTER -- actor isolation
actor AppState {
    static let shared = AppState()
    private(set) var isLoggedIn = false
    func setLoggedIn(_ value: Bool) { isLoggedIn = value }
}
```

**`nonisolated(unsafe)`** is the last resort — only for values genuinely set once before any concurrent access (app launch configuration). Always document the safety invariant.

---

## Undocumented Framework Threading Requirements 🔴

Apple framework delegates sometimes have undocumented main-thread requirements. Auto-generated async versions of delegate methods can crash when the framework calls back on a background thread.

**Known problematic patterns:**

| Framework | Issue | Fix |
|-----------|-------|-----|
| `UNUserNotificationCenterDelegate` | Async delegate methods may run off main | Add `@MainActor` to delegate class |
| `UIApplication.setAlternateIconName` | Completion handler called on wrong thread | Use continuation with `@MainActor` resume |
| `URLSession.getAllTasks` | Auto-async version has thread mismatch | Use completion-handler version with continuation |
| Core Motion callbacks | May arrive on internal queue | Dispatch to expected queue before processing |

```swift
// CRASH -- auto-generated async delegate may run off main thread
class NotificationDelegate: NSObject, UNUserNotificationCenterDelegate {
    // Swift compiler generates async version that may not run on main thread
    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                didReceive response: UNNotificationResponse) async {
        // May crash if accessing UI state
    }
}

// FIX -- explicit @MainActor on delegate class
@MainActor
class NotificationDelegate: NSObject, UNUserNotificationCenterDelegate {
    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                didReceive response: UNNotificationResponse) async {
        // Safe -- @MainActor ensures main thread
    }
}
```

**General rule:** For any Apple framework delegate, prefer `@MainActor` annotation on the delegate class unless you have specific evidence it's called from a known background queue.

---

## Actor Hopping Overhead — Batch Your Calls 🟡

Each MainActor ↔ background actor call requires a thread context switch. In a loop, this overhead is massive — loading 100 items from a database actor with individual calls can take 10x longer than a single batch call.

```swift
// SLOW -- 100 actor hops (one per item)
@MainActor func loadItems() async {
    for id in itemIDs {
        let item = await databaseActor.fetchItem(id) // Hop to actor and back
        items.append(item)
    }
}

// FAST -- single actor hop for entire batch
@MainActor func loadItems() async {
    let fetchedItems = await databaseActor.fetchItems(itemIDs) // One hop
    items = fetchedItems
}

// In the actor:
actor DatabaseActor {
    func fetchItems(_ ids: [ItemID]) -> [Item] {
        ids.compactMap { cache[$0] } // All work in single isolation context
    }
}
```

**Measurement:** Use Instruments → Swift Concurrency template to visualize actor hop frequency. Look for "ping-pong" patterns between MainActor and custom actors.

**When to use Mutex instead of actor:** If all your actor operations are synchronous (no `await` inside the actor methods), `Mutex<State>` eliminates hopping entirely since access is synchronous.

---

## Migration Checklist

Before migrating a module to strict concurrency:

- [ ] All downstream (leaf) dependencies already migrated
- [ ] Third-party SDK Sendable status audited; blockers wrapped in actors
- [ ] Global mutable state cataloged with fix strategy for each
- [ ] `SWIFT_STRICT_CONCURRENCY=targeted` passes with zero warnings
- [ ] `SWIFT_STRICT_CONCURRENCY=complete` warnings triaged and prioritized
- [ ] No `@unchecked Sendable` without documented justification
- [ ] Actor hopping patterns reviewed for batch optimization opportunities
- [ ] Thread Sanitizer enabled in CI for the migrated module
- [ ] Test suite covers concurrent access paths (not just serial happy-path)
