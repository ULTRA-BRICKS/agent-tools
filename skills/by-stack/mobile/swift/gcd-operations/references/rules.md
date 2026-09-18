# GCD & OperationQueue — Rules Quick Reference

## Do's — Always Follow

1. **Label every queue with reverse DNS** — labels appear in crash reports, Instruments, and Xcode debugger. `DispatchQueue(label: "com.company.app.subsystem")`.
2. **Use serial queues by default** — they provide mutual exclusion and ordering. Concurrent queues are an optimization, not a default.
3. **Limit the app to 3-4 well-defined queue subsystems** — per Apple WWDC 2017-706. Use target queue hierarchies to funnel work. Never scatter `DispatchQueue.global()` calls throughout the codebase.
4. **Balance every `group.enter()` with `defer { group.leave() }`** — missing leave means `notify()` never fires. Extra leave crashes with `EXC_BAD_INSTRUCTION`.
5. **Use `dispatchPrecondition` at API boundaries** — `.onQueue(.main)` before UI updates, `.notOnQueue(serialQueue)` before sync dispatch. Debug-only — removed in release builds, zero production cost.
6. **Use `[weak self]` in repeating timers and stored closures** — one-shot `async` closures are safe (execute and release), but repeating `DispatchSourceTimer` handlers and closures stored on properties create retain cycles.
7. **Always call `endBackgroundTask`** — in both the expiration handler AND the success path. Failing to end is the #1 cause of background task problems (Apple DTS).
8. **Run all CI tests with Thread Sanitizer enabled** — catches data races, access races, and use-after-free at runtime.

## Don'ts — Critical Anti-Patterns

### Never: `sync` on main queue from main thread

```swift
// CRASH -- libdispatch detects and traps
DispatchQueue.main.sync { print("never") }

// FIX: Always use async
DispatchQueue.main.async { print("works") }
```

### Never: Nested `sync` on same serial queue

```swift
// DEADLOCK -- outer block occupies queue, inner sync waits forever
serialQueue.async {
    serialQueue.sync { /* ... */ } // Circular wait
}
```

### Never: Barrier on global queue

```swift
// SILENTLY IGNORED -- no warning, no error, no exclusion
DispatchQueue.global().async(flags: .barrier) { /* not exclusive */ }

// FIX: Custom concurrent queue
let rwQueue = DispatchQueue(label: "com.app.rwlock", attributes: .concurrent)
rwQueue.async(flags: .barrier) { /* exclusive write */ }
```

### Never: `DispatchSemaphore.wait()` in async context

```swift
// DEADLOCK -- blocks cooperative thread pool thread
func fetch() async {
    let semaphore = DispatchSemaphore(value: 0)
    Task { semaphore.signal() }
    semaphore.wait() // Pool can be as small as 1 thread
}
```

### Never: `.background` QoS for user-visible work

```swift
// FREEZES in Low Power Mode -- system may halt .background entirely
DispatchQueue.global(qos: .background).async {
    downloadUserRequestedFile() // User is waiting!
}

// FIX: .userInitiated for user-triggered work
DispatchQueue.global(qos: .userInitiated).async { /* ... */ }
```
