# Thread Explosion Prevention

## How to Use This Reference

Read this when diagnosing performance issues, seeing high thread counts in Instruments, or reviewing code that dispatches many concurrent blocks. Thread explosion is the #1 GCD performance killer.

---

## What Is Thread Explosion

GCD creates new threads when existing ones are blocked. If you dispatch 200 blocks to `DispatchQueue.global()` and each blocks on I/O or a semaphore, GCD can create up to ~512 threads per process (kernel limit). Each thread costs ~512KB stack + scheduling overhead.

**Limits:**

- ~64 threads per workqueue (per QoS)
- ~512 threads per process (kernel limit)
- Each thread: ~512KB stack memory
- 512 threads × 512KB = ~256MB just for stacks

---

## The DispatchQueue.global() Danger

```swift
// THREAD EXPLOSION — each iteration can spawn a thread
for url in urls {  // 200 URLs
    DispatchQueue.global().async {
        let data = try! Data(contentsOf: url)  // Blocks on I/O
        process(data)
    }
}
```

Every blocked thread causes GCD to create a new one. 200 blocking I/O operations = potentially 200 threads.

```swift
// FIX 1: OperationQueue with maxConcurrentOperationCount
let queue = OperationQueue()
queue.maxConcurrentOperationCount = 4
for url in urls {
    queue.addOperation {
        let data = try! Data(contentsOf: url)
        process(data)
    }
}

// FIX 2: DispatchSemaphore for rate-limiting
// Wait BEFORE dispatching — throttles the producer, not the workers
let semaphore = DispatchSemaphore(value: 4)
for url in urls {
    semaphore.wait()  // Blocks producer thread until a slot opens
    DispatchQueue.global().async {
        defer { semaphore.signal() }
        let data = try! Data(contentsOf: url)
        process(data)
    }
}

// FIX 3: Serial queue (simplest, if parallelism not needed)
let processingQueue = DispatchQueue(label: "com.app.processing")
for url in urls {
    processingQueue.async {
        let data = try! Data(contentsOf: url)
        process(data)
    }
}
```

---

## WWDC 2017-706 Rule

Apple's recommendation: **Use a fixed number of serial queue hierarchies equal to the number of subsystems in your app.** Typically 3-4:

1. **Networking** subsystem queue
2. **Persistence** subsystem queue (Core Data, file I/O)
3. **UI support** queue (image processing, layout computation)
4. **Media** queue (if applicable — audio, video processing)

Use target queue hierarchies to funnel work (see `queue-creation.md`).

**Low Power Mode warning:** iOS may **halt `.background` QoS work entirely** when Low Power Mode is active. Any work queued with `.background` — either on a named queue or via `DispatchQueue.global(.background)` — can stop executing. If the work is user-observable or required for app correctness, use `.utility` or higher.

---

## Priority Inversion

When a high-priority task waits for a low-priority task, and a medium-priority task preempts the low-priority one:

```
High priority:    [blocked waiting for Low]
Medium priority:  [running — preempts Low]
Low priority:     [blocked by Medium — can't finish work High needs]
```

**GCD resolves priority inversion automatically** for:

- `DispatchQueue.sync` — GCD temporarily boosts the target queue's QoS
- `DispatchGroup.wait` — GCD boosts work inside the group
- `OperationQueue` with dependencies — QoS propagation

**GCD does NOT resolve priority inversion for:**

- `DispatchSemaphore` — no priority donation, which is why semaphore should NOT be used as a mutex
- `os_unfair_lock` — provides priority donation (one reason it's faster than semaphore for mutual exclusion)
- `NSLock` — provides priority donation via pthread_mutex

---

## Thread Starvation with Semaphores in Swift Concurrency

**Critical:** Never use `DispatchSemaphore.wait()` in an async context:

```swift
// DEADLOCK RISK — blocks cooperative thread pool
func fetchData() async -> Data {
    let semaphore = DispatchSemaphore(value: 0)
    var result: Data!

    URLSession.shared.dataTask(with: url) { data, _, _ in
        result = data
        semaphore.signal()
    }.resume()

    semaphore.wait()  // Blocks a cooperative thread — can starve the pool
    return result
}
```

The Swift Concurrency cooperative thread pool has a limited number of threads (typically equal to CPU cores). Blocking one with `semaphore.wait()` can starve other tasks, and if all threads are blocked, the entire async system deadlocks.

**Fix:** Use `withCheckedContinuation` to bridge callback-based APIs:

```swift
func fetchData() async -> Data {
    await withCheckedContinuation { continuation in
        URLSession.shared.dataTask(with: url) { data, _, _ in
            continuation.resume(returning: data!)
        }.resume()
    }
}
```

---

## Throttling Strategies

### OperationQueue (Preferred)

```swift
let downloadQueue = OperationQueue()
downloadQueue.name = "com.app.downloads"
downloadQueue.maxConcurrentOperationCount = 4  // Hard limit
downloadQueue.qualityOfService = .utility
```

### concurrentPerform (CPU-bound batch work)

```swift
// Automatically limits to optimal thread count
DispatchQueue.concurrentPerform(iterations: items.count) { i in
    processItem(items[i])  // Must be non-blocking!
}
```

**Rules for `concurrentPerform`:**

- Work inside MUST be non-blocking (no I/O, no semaphore.wait)
- Runs synchronously — blocks until all iterations complete
- Automatically uses optimal thread count (usually == CPU cores)
- Perfect for CPU-bound parallel processing

### Target Queue Hierarchy (Architectural)

Funnel multiple subsystem queues through a limited set of root queues (see `queue-creation.md`). This prevents thread explosion at the architecture level.

---

## Detection

Signs of thread explosion in Instruments:

- **System Trace:** Thread count spikes (>20 threads for your process)
- **Time Profiler:** Many threads with tiny time slices (context switching overhead)
- **Thread list:** Many threads named `com.apple.root.default-qos` (unnamed global queue work)
- **Runtime:** App becomes sluggish despite low CPU usage (threads fighting for scheduling)

Quick check in Xcode debugger: `Thread.current` shows thread number — if you see thread numbers > 30, investigate.
