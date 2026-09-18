# Debugging Concurrency Issues

## How to Use This Reference

Read this when diagnosing data races, verifying thread correctness, profiling concurrent code with Instruments, or writing tests for concurrent behavior.

---

## Thread Sanitizer (TSan)

The most important concurrency debugging tool. Detects data races at runtime with ~2-5x slowdown.

### Enabling in Xcode

1. Edit Scheme → Run → Diagnostics → check "Thread Sanitizer"
2. Or: Edit Scheme → Test → Diagnostics → check "Thread Sanitizer"

### CI Integration

```yaml
# xcodebuild with TSan
xcodebuild test \
  -scheme MyApp \
  -destination 'platform=iOS Simulator,name=iPhone 16' \
  -enableThreadSanitizer YES
```

**Add TSan to your CI test pipeline.** Data races are intermittent — they may not reproduce locally but will eventually appear in CI with enough test runs.

### Reading TSan Output

```
WARNING: ThreadSanitizer: data race (pid=12345)
  Write of size 8 at 0x7ffeefbff5a0 by thread T2:
    #0 MyApp.ImageCache.store(_:for:) ImageCache.swift:42
    #1 MyApp.NetworkManager.handleResponse(_:) NetworkManager.swift:87

  Previous read of size 8 at 0x7ffeefbff5a0 by main thread:
    #0 MyApp.ImageCache.image(for:) ImageCache.swift:28
    #1 MyApp.ProductCell.configure(with:) ProductCell.swift:15
```

**How to read:**

1. Two threads accessed the same memory location
2. At least one was a write
3. No synchronization between them
4. Stack traces show exactly where — `ImageCache.store` (write on T2) vs `ImageCache.image(for:)` (read on main)
5. **Fix:** Add `NSLock` or serial queue to protect `ImageCache`'s internal storage

### TSan Limitations

- Only detects races that actually execute during the test run
- ~2-5x runtime slowdown, ~5-10x memory overhead
- Cannot be used with Address Sanitizer simultaneously
- Only works on simulators (not devices)

---

## dispatchPrecondition

Assert queue context at compile-time-checkable boundaries. **Removed in release builds** — zero production cost.

```swift
func updateUI() {
    dispatchPrecondition(condition: .onQueue(.main))
    tableView.reloadData()
}

func processInBackground() {
    dispatchPrecondition(condition: .notOnQueue(.main))
    heavyComputation()
}

func accessCache() {
    dispatchPrecondition(condition: .onQueue(cacheQueue))
    return cache[key]
}
```

### Three Predicates

| Predicate | Use When |
|-----------|----------|
| `.onQueue(.main)` | UI update methods |
| `.notOnQueue(.main)` | Expensive computation that must not block UI |
| `.onQueue(specificQueue)` | Methods that must only be called from a specific queue |

**Best practice:** Add `dispatchPrecondition` at every **API boundary** — the top of every public or internal method that has a queue requirement. This ensures violations are caught at the call site (during development) rather than buried in a hard-to-reproduce crash. Since the assertion is stripped in release builds, there is zero production overhead.

---

## Instruments Profiling

### Time Profiler

Shows where CPU time is spent across threads.

**What to look for:**

- Single thread consuming 100% CPU while others are idle → serial bottleneck
- Many threads each with tiny time slices → thread explosion / context switching
- Main thread blocked for >16ms → UI jank

### System Trace

Shows thread scheduling, context switches, and blocking.

**What to look for:**

- Threads blocked on locks/semaphores for long periods → contention
- Thread creation bursts → thread explosion
- Priority inversions (marked explicitly in System Trace)

### os_signpost for Custom Profiling

```swift
import os.signpost

let log = OSLog(subsystem: "com.app", category: "Networking")

func fetchData(url: URL) {
    let signpostID = OSSignpostID(log: log)

    os_signpost(.begin, log: log, name: "FetchData", signpostID: signpostID,
                "URL: %{public}@", url.absoluteString)

    URLSession.shared.dataTask(with: url) { data, _, error in
        os_signpost(.end, log: log, name: "FetchData", signpostID: signpostID,
                    "Size: %d bytes", data?.count ?? 0)
    }.resume()
}
```

Visible in Instruments → os_signpost instrument. Shows duration, frequency, and overlapping operations.

---

## Testing Concurrent Code

### XCTestExpectation for Async Operations

```swift
func testConcurrentAccess() {
    let cache = ThreadSafeCache<String, Int>()
    let expectation = expectation(description: "All writes complete")
    expectation.expectedFulfillmentCount = 100

    for i in 0..<100 {
        DispatchQueue.global().async {
            cache.setValue(i, for: "key-\(i)")
            expectation.fulfill()
        }
    }

    wait(for: [expectation], timeout: 5.0)
    XCTAssertEqual(cache.count, 100)
}
```

### Stress Testing with concurrentPerform

```swift
func testThreadSafety_stressTest() {
    let cache = ThreadSafeCache<String, Int>()

    // Hammer from multiple threads simultaneously
    DispatchQueue.concurrentPerform(iterations: 10_000) { i in
        if i % 2 == 0 {
            cache.setValue(i, for: "key-\(i)")
        } else {
            _ = cache.value(for: "key-\(i - 1)")
        }
    }

    // If no crash and TSan is clean, the synchronization is correct
}
```

### Injectable Queue for Testing

```swift
final class DataProcessor {
    private let queue: DispatchQueue

    // Default: real queue. Tests: inject a known queue.
    init(queue: DispatchQueue = DispatchQueue(label: "com.app.processor")) {
        self.queue = queue
    }

    func process(_ data: Data, completion: @escaping (Result<Output, Error>) -> Void) {
        queue.async {
            let result = self.transform(data)
            DispatchQueue.main.async { completion(result) }
        }
    }
}

// In tests — use a serial queue you control
func testProcess() {
    let testQueue = DispatchQueue(label: "test.processor")
    let processor = DataProcessor(queue: testQueue)
    // ...
}
```

### Memory Leak Detection with Busy Assertion

```swift
func testNoRetainCycle() {
    var sut: HeartbeatMonitor? = HeartbeatMonitor()
    weak var weakSUT = sut

    sut?.start(interval: 1.0)
    sut?.stop()
    sut = nil

    // If timer handler captured self strongly, weakSUT won't be nil
    XCTAssertNil(weakSUT, "HeartbeatMonitor leaked — check [weak self] in timer handler")
}
```

---

## Environment Variables

Useful debugging environment variables (set in Xcode scheme → Run → Arguments → Environment Variables):

| Variable | Value | Effect |
|----------|-------|--------|
| `LIBDISPATCH_COOPERATIVE_POOL_STRICT` | `1` | Detect blocking in cooperative thread pool |
| `OBJC_DEBUG_MISSING_POOLS` | `YES` | Warn about missing autorelease pools |

---

## Logging Best Practices

```swift
import os

let logger = Logger(subsystem: "com.app", category: "concurrency")

func accessSharedResource() {
    logger.debug("Accessing resource on thread: \(Thread.current)")
    logger.info("Queue label: \(String(cString: __dispatch_queue_get_label(nil)))")
}
```

Use `os.Logger` (iOS 14+) or `os_log` for structured logging. Avoid `print()` in production — it's not thread-safe and has no filtering.
