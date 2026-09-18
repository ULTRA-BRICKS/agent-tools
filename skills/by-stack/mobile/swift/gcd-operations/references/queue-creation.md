# Queue Creation & Configuration

## How to Use This Reference

Read this when creating new dispatch queues, selecting QoS classes, setting up target queue hierarchies, or reviewing existing queue architecture. Every queue decision affects thread pool behavior, priority, and debuggability.

---

## Serial vs Concurrent Queues

```swift
// Serial — tasks execute one at a time, in FIFO order
let serial = DispatchQueue(label: "com.app.cache")

// Concurrent — tasks can execute in parallel
let concurrent = DispatchQueue(label: "com.app.processing", attributes: .concurrent)
```

**Default is serial.** Use serial unless you have a measured need for concurrency. Serial queues are simpler, deadlock-resistant, and sufficient for most coordination.

---

## QoS (Quality of Service) Classes

| QoS | Purpose | Priority | Example |
|-----|---------|----------|---------|
| `.userInteractive` | Immediate UI response | Highest | Animation, event handling |
| `.userInitiated` | User waiting for result | High | Opening document, search |
| `.default` | Not specified | Medium | General work |
| `.utility` | Long-running with progress | Low | Download, import |
| `.background` | User not aware | Lowest | Backup, prefetch, indexing |
| `.unspecified` | Opt out of QoS | System decides | Legacy code bridging |

**Rules:**

- Never use `.background` for user-visible work — iOS aggressively throttles it, and **in Low Power Mode iOS may halt `.background` work entirely**
- Never use `.userInteractive` for anything that takes > 16ms
- When in doubt, use `.userInitiated` for user-triggered work
- `.utility` is correct for network downloads with progress bars

---

## Avoid DispatchQueue.global()

`DispatchQueue.global()` is a shared resource across your entire app and all frameworks. Problems:

1. **No label** — crashes show `com.apple.root.default-qos`, useless for debugging
2. **No control** — you can't set `maxConcurrentOperationCount`, can't pause, can't cancel
3. **Thread explosion** — every `global().async` can spawn a new thread (see `thread-explosion.md`)
4. **No barriers** — barriers on global queues are silently ignored (see `thread-safety.md`)

```swift
// BAD — scattered throughout codebase
DispatchQueue.global().async { /* network */ }
DispatchQueue.global().async { /* parsing */ }
DispatchQueue.global().async { /* image resize */ }

// GOOD — named subsystem queues
enum QueueSubsystem {
    static let networking = DispatchQueue(label: "com.app.networking")
    static let parsing = DispatchQueue(label: "com.app.parsing")
    static let imageProcessing = DispatchQueue(
        label: "com.app.image",
        qos: .utility,
        attributes: .concurrent
    )
}
```

---

## Target Queue Hierarchies (WWDC 2017-706)

Target queues funnel work from multiple sources through a single serial bottleneck, preventing thread explosion while preserving logical separation.

```swift
// Subsystem root — all work funnels through here
let subsystemQueue = DispatchQueue(label: "com.app.subsystem")

// Module queues target the subsystem
let networkQueue = DispatchQueue(
    label: "com.app.subsystem.network",
    target: subsystemQueue
)
let parsingQueue = DispatchQueue(
    label: "com.app.subsystem.parsing",
    target: subsystemQueue
)

// networkQueue and parsingQueue run on subsystemQueue's thread
// No extra threads, no explosion, logical separation preserved
```

**Rules:**

- 3-4 subsystem root queues for a typical app (networking, UI-support, persistence, media)
- Never create a target queue cycle (A targets B, B targets A) — deadlock
- Never `sync` dispatch to a queue whose target chain includes the current queue — deadlock
- Target queues inherit QoS from the highest-priority enqueued work item

---

## Queue Labeling

Every queue MUST have a reverse-DNS label with subsystem and purpose:

```swift
// GOOD — identifiable in crash logs, Instruments, debugger
DispatchQueue(label: "com.myapp.imageCache.write")
DispatchQueue(label: "com.myapp.networking.upload")

// BAD — useless in debugging
DispatchQueue(label: "queue1")
DispatchQueue(label: "myQueue")
DispatchQueue(label: "")  // Anonymous — worst case
```

In crash logs and Thread Sanitizer output, the label is the ONLY way to identify which queue is involved.

---

## Initially Inactive Queues

Use `.initiallyInactive` when you need to configure a queue before it starts processing:

```swift
let queue = DispatchQueue(
    label: "com.app.batchProcessor",
    attributes: [.concurrent, .initiallyInactive]
)

// Enqueue work while queue is inactive — nothing executes yet
for item in batch {
    queue.async { process(item) }
}

// Start processing all at once
queue.activate()
```

**Rules:**

- Once activated, cannot be deactivated
- Useful for batch setup, dependency configuration, or deferred start
- A suspended queue that is deallocated before being resumed will crash — `.initiallyInactive` avoids this by requiring explicit `activate()`

---

## Autorelease Frequency

For queues that create many temporary Objective-C objects:

```swift
// Drain autorelease pool after each work item
let queue = DispatchQueue(
    label: "com.app.parsing",
    autoreleaseFrequency: .workItem
)
```

| Frequency | Behavior | When |
|-----------|----------|------|
| `.inherit` | Inherit from target queue (default) | Most cases |
| `.workItem` | Drain pool after each block | Parsing, image processing, batch work |
| `.never` | Never auto-drain | Only for manual `autoreleasepool` control |

Use `.workItem` for queues that process many items in a loop to prevent memory buildup.
