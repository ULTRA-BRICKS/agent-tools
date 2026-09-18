# Deadlock Patterns & Prevention

## How to Use This Reference

When reviewing concurrent code, check for these 5 deadlock patterns. Every deadlock follows a circular-wait pattern. For every violation found, add it to the feature's `refactoring/` plan with severity 🔴 Critical.

## Severity

All deadlocks are 🔴 Critical — they cause app freezes or Watchdog kills with no recovery.

---

## Pattern 1: sync on Main from Main

The single most common iOS deadlock. Main queue is serial — if you're already on it, `sync` waits forever.

```swift
// DEADLOCK — main.sync called from main thread
DispatchQueue.main.sync {
    updateUI()  // Never executes
}
```

**Why it deadlocks:** Main queue is serial. `sync` blocks current thread (main) waiting for the block to execute on main queue. But main queue can't execute — it's blocked by `sync`.

```swift
// FIX 1: Use async (preferred — eliminates the deadlock entirely)
DispatchQueue.main.async {
    updateUI()
}

// FIX 2: Check if already on main (use with caution — see caveat below)
if Thread.isMainThread {
    updateUI()
} else {
    DispatchQueue.main.async { updateUI() }
}
// Caveat: Thread.isMainThread checks the thread, not the queue.
// In rare cases (e.g., performAndWait) you can be on the main thread
// but not on the main queue. Prefer FIX 1 when possible.

// DETECTION: dispatchPrecondition (does NOT fix deadlocks — catches them in debug)
func updateUI() {
    dispatchPrecondition(condition: .onQueue(.main))
    // Crashes in debug if called from wrong queue.
    // Stripped in release builds — zero production protection.
}
```

---

## Pattern 2: Nested sync on Same Serial Queue

```swift
let queue = DispatchQueue(label: "com.app.serial")

queue.sync {
    // Already on this serial queue
    queue.sync {  // DEADLOCK
        doWork()
    }
}
```

**Why it deadlocks:** Outer `sync` is running on the serial queue. Inner `sync` waits for queue to be free. Queue won't be free until outer block finishes. Circular wait.

**Fix:** Never nest `sync` calls on the same queue. Restructure to use a single level, or use a lock instead.

---

## Pattern 3: Target Queue sync Cycle

```swift
let parent = DispatchQueue(label: "com.app.parent")
let child = DispatchQueue(label: "com.app.child", target: parent)

parent.sync {
    child.sync {  // DEADLOCK
        // child targets parent, so this is nested sync on parent
    }
}
```

**Why it deadlocks:** `child` targets `parent`, so child's work actually runs on parent. This is equivalent to Pattern 2 — nested sync on the same underlying queue.

**Fix:** Never `sync` dispatch to a queue whose target chain includes the current queue. Use `async` instead, or restructure the target hierarchy.

---

## Pattern 4: ABBA Lock Ordering

```swift
let queueA = DispatchQueue(label: "com.app.a")
let queueB = DispatchQueue(label: "com.app.b")

// Thread 1
queueA.sync {
    queueB.sync { /* work */ }  // Waits for B
}

// Thread 2 (simultaneously)
queueB.sync {
    queueA.sync { /* work */ }  // Waits for A — DEADLOCK
}
```

**Why it deadlocks:** Thread 1 holds A, wants B. Thread 2 holds B, wants A. Neither can proceed.

**Fix:** Always acquire locks/queues in the same global order. If you need both A and B, always acquire A first, then B. Document the ordering.

---

## Pattern 5: DispatchGroup / Semaphore Wait on Main

```swift
let group = DispatchGroup()
group.enter()
someAsyncWork {
    group.leave()
}
group.wait()  // DEADLOCK if on main thread and callback needs main

// Same problem with semaphore
let semaphore = DispatchSemaphore(value: 0)
fetchData { result in
    // If this callback dispatches to main...
    semaphore.signal()
}
semaphore.wait()  // Blocks main — callback can't dispatch to main
```

**Fix:** Never `wait()` on main thread. Use `group.notify` or completion handlers instead:

```swift
let group = DispatchGroup()
group.enter()
someAsyncWork {
    group.leave()
}
group.notify(queue: .main) {
    // Safe — non-blocking
    updateUI()
}
```

---

## Prevention: dispatchPrecondition

Add precondition checks at API boundaries to catch queue violations in debug:

```swift
func updateUI() {
    dispatchPrecondition(condition: .onQueue(.main))
    // ...
}

func processData() {
    dispatchPrecondition(condition: .notOnQueue(.main))
    // ...
}

func accessCache() {
    dispatchPrecondition(condition: .onQueue(cacheQueue))
    // ...
}
```

Three predicates:

| Predicate | Meaning |
|-----------|---------|
| `.onQueue(q)` | Assert currently executing on queue `q` |
| `.notOnQueue(q)` | Assert NOT on queue `q` |
| `.onQueue(.main)` | Assert on main thread/queue |

**These are removed in release builds** — zero runtime cost in production.

---

## Detection Checklist

When reviewing code, search for these patterns:

- [ ] `DispatchQueue.main.sync` — is caller guaranteed to be off main?
- [ ] Any `.sync` call — could the current queue be the same queue (or its target)?
- [ ] Two or more queues acquired via `.sync` — is the order always consistent?
- [ ] `group.wait()` or `semaphore.wait()` — is this on main thread?
- [ ] Target queue hierarchy — any cycles?
- [ ] `dispatchPrecondition` at public API boundaries?
