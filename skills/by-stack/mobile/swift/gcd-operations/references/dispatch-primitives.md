# Dispatch Primitives

## How to Use This Reference

Read this when working with DispatchGroup, DispatchWorkItem, DispatchSemaphore, DispatchSource timers, or DispatchIO. Each section covers correct usage patterns, common mistakes, and production-safe examples.

---

## DispatchGroup

Tracks completion of multiple async tasks. The most common concurrency coordination primitive.

### Basic Pattern

```swift
let group = DispatchGroup()

group.enter()
fetchUsers { users in
    defer { group.leave() }  // ALWAYS defer leave()
    self.users = users
}

group.enter()
fetchProducts { products in
    defer { group.leave() }
    self.products = products
}

group.notify(queue: .main) {
    self.updateUI()  // Both fetches complete
}
```

### Critical Rules

1. **Every `enter()` must have exactly one `leave()`** — missing leave = notify never fires; extra leave = `EXC_BAD_INSTRUCTION` crash
2. **Always use `defer { group.leave() }`** inside the async callback, right after enter logic, to guarantee balance on every code path (success, error, early return)
3. **Never use `group.wait()` on main thread** — use `group.notify` instead (see `deadlocks.md`, Pattern 5)

### Timeout Pattern

```swift
let result = group.wait(timeout: .now() + 10)
switch result {
case .success:
    print("All tasks completed")
case .timedOut:
    print("Timed out — some tasks still pending")
}
```

---

## DispatchWorkItem

Cancelable, chainable unit of work.

### Basic Usage

```swift
let item = DispatchWorkItem {
    processData()
}

DispatchQueue.global().async(execute: item)

// Cancel before execution (cooperative — does not interrupt running work)
item.cancel()
```

### Checking Cancellation in Long-Running Work

```swift
var item: DispatchWorkItem!
item = DispatchWorkItem { [weak item] in  // weak to avoid retain cycle
    for i in 0..<10_000 {
        guard item?.isCancelled != true else { return }  // Cooperative check
        processChunk(i)
    }
}
```

### QoS Enforcement Flag

Use `.enforceQoS` to prevent priority inversion when dispatching high-priority work items to lower-priority queues:

```swift
let urgentItem = DispatchWorkItem(qos: .userInteractive, flags: [.enforceQoS]) {
    renderFrame()
}
backgroundQueue.async(execute: urgentItem)
// Queue's QoS is temporarily raised to .userInteractive for this item
```

### Debounce Pattern

```swift
final class SearchDebouncer {
    private var pendingItem: DispatchWorkItem?

    func debounce(delay: TimeInterval, action: @escaping () -> Void) {
        pendingItem?.cancel()  // Cancel previous pending search
        let item = DispatchWorkItem(block: action)
        pendingItem = item
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: item)
    }
}

// Usage
let debouncer = SearchDebouncer()
func searchTextChanged(_ text: String) {
    debouncer.debounce(delay: 0.3) {
        performSearch(text)
    }
}
```

### Chaining with notify

```swift
let processingQueue = DispatchQueue(label: "com.app.processing")
let download = DispatchWorkItem { downloadFile() }
let parse = DispatchWorkItem { parseFile() }

processingQueue.async(execute: download)
download.notify(queue: processingQueue, execute: parse)
parse.notify(queue: .main) {
    updateUI()
}
```

---

## DispatchSemaphore

**Use ONLY for rate-limiting, NEVER as a mutex.** Semaphores have no priority donation — using them for mutual exclusion causes priority inversion.

### Rate-Limiting Pattern (Correct Usage)

```swift
let maxConcurrent = DispatchSemaphore(value: 4)

for url in urls {
    DispatchQueue.global().async {
        maxConcurrent.wait()    // Blocks until a slot opens
        defer { maxConcurrent.signal() }
        downloadAndProcess(url)
    }
}
```

### What NOT to Use Semaphore For

```swift
// BAD — semaphore as a mutex (no priority donation)
let mutex = DispatchSemaphore(value: 1)
mutex.wait()
sharedState.modify()
mutex.signal()
// Use NSLock instead — it has priority donation

// BAD — semaphore.wait() to make async synchronous
let sem = DispatchSemaphore(value: 0)
fetchData { result in
    // ...
    sem.signal()
}
sem.wait()  // Blocks thread, risks deadlock on main
// Use completion handler or async/await instead
```

---

## DispatchSource Timers

System-efficient timers with automatic coalescing.

### Complete Lifecycle Pattern

```swift
final class HeartbeatMonitor {
    private var timer: DispatchSourceTimer?
    private let queue = DispatchQueue(label: "com.app.heartbeat")

    func start(interval: TimeInterval) {
        stop()  // Cancel any existing timer

        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(
            deadline: .now() + interval,
            repeating: interval,
            leeway: .milliseconds(100)  // Allow coalescing for battery
        )
        timer.setEventHandler { [weak self] in  // MUST be weak for repeating
            self?.sendHeartbeat()
        }
        self.timer = timer
        timer.activate()  // Was resume() before iOS 10
    }

    func stop() {
        timer?.cancel()
        timer = nil
    }

    deinit {
        stop()
    }
}
```

### Critical Rules

1. **`[weak self]` in repeating timer handlers** — strong reference creates retain cycle (timer → closure → self → timer)
2. **Cancel before dealloc** — deallocating a suspended timer crashes (`EXC_BAD_INSTRUCTION`)
3. **Resume/activate before first use** — timers start suspended
4. **Cancel is idempotent** — safe to call multiple times
5. **After cancel, cannot be reused** — create a new timer instead
6. **Leeway** — always set leeway for battery efficiency; system coalesces timers within the leeway window

### Suspend / Resume Lifecycle

```swift
// Suspend (pause without destroying)
timer.suspend()

// Resume (must resume before dealloc!)
timer.resume()

// WARNING: Unbalanced suspend/resume crashes
// Each suspend must be matched by a resume
// Deallocating a suspended source = crash
```

---

## DispatchIO

Channel-based I/O for reading/writing files without blocking threads. **No async/await equivalent exists** — DispatchIO is still the best option for non-blocking file operations.

### Reading a File

```swift
let channel = DispatchIO(
    type: .stream,
    path: filePath,
    oflag: O_RDONLY,
    mode: 0,
    queue: DispatchQueue.global(),
    cleanupHandler: { error in
        if error != 0 { print("Cleanup error: \(error)") }
    }
)

var allData = Data()
channel.read(
    offset: 0,
    length: Int.max,
    queue: DispatchQueue.global()
) { done, data, error in
    if let data = data, !data.isEmpty {
        allData.append(contentsOf: data)
    }
    if done {
        processFile(allData)
    }
}
```

### When to Use

- Large file reads/writes where blocking a thread is unacceptable
- High-throughput I/O (log files, data export)
- Streaming data from disk

### When NOT to Use

- Small config files — just use `Data(contentsOf:)` on a background queue
- Network I/O — use URLSession instead
