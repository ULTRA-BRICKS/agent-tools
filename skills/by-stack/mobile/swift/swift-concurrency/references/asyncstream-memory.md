# AsyncStream & Memory Management

## How to Use This Reference

Read this when creating or consuming `AsyncStream`, `AsyncSequence`, or `TaskGroup` in production code. Every pattern here addresses a memory leak, resource leak, or behavioral surprise that manifests only under sustained load.

---

## Infinite AsyncSequence Loops Leak Self 🔴

A `for await` loop over an infinite `AsyncSequence` (e.g., `NotificationCenter.default.notifications(named:)`) creates a strong reference to `self` for the loop's duration — forever. The Task that owns the loop must be explicitly cancelled in `deinit` or `.onDisappear`.

```swift
// LEAK -- Task keeps self alive forever
class NetworkMonitor {
    private var task: Task<Void, Never>?

    func startObserving() {
        task = Task {
            for await notification in NotificationCenter.default.notifications(named: .connectivityChanged) {
                self.handleChange(notification) // Strong capture, loop never ends
            }
        }
    }

    deinit {
        // Even if task is cancelled here, self is already retained by the loop
    }
}

// FIX -- weak self + explicit cancellation
class NetworkMonitor {
    private var task: Task<Void, Never>?

    func startObserving() {
        task = Task { [weak self] in
            for await notification in NotificationCenter.default.notifications(named: .connectivityChanged) {
                guard let self else { return } // Break loop if deallocated
                self.handleChange(notification)
            }
        }
    }

    func stopObserving() {
        task?.cancel()
        task = nil
    }

    deinit {
        task?.cancel()
    }
}
```

**SwiftUI:** Use `.task { }` modifier — it auto-cancels when the view disappears.

---

## Always Call continuation.finish() 🟡

Failing to call `finish()` on an `AsyncStream.Continuation` leaks the stream and all captured resources. The consumer's `for await` loop suspends forever waiting for more elements.

```swift
// LEAK -- producer never signals completion
func makeStream() -> AsyncStream<Event> {
    AsyncStream { continuation in
        eventSource.onEvent = { event in
            continuation.yield(event)
        }
        // BUG: No finish() when source completes or is cancelled
    }
}

// FIX -- finish on completion + onTermination for consumer cancellation
func makeStream() -> AsyncStream<Event> {
    AsyncStream { continuation in
        eventSource.onEvent = { event in
            continuation.yield(event)
        }
        eventSource.onComplete = {
            continuation.finish() // Signal end of stream
        }
        continuation.onTermination = { _ in
            eventSource.stop() // Clean up when consumer cancels
        }
    }
}
```

**Rule:** Every `AsyncStream` must have paired `finish()` (producer done) and `onTermination` (consumer cancelled) handlers.

---

## AsyncStream Has No Backpressure 🟠

The default `.unbounded` buffering policy allows producers to overwhelm consumers, causing unbounded memory growth. This manifests as gradual memory increase under high event rates.

| Policy | Behavior | Use When |
|--------|----------|----------|
| `.unbounded` | Buffer everything, no drops | Low-frequency events (notifications, lifecycle) |
| `.bufferingNewest(n)` | Keep latest N, drop oldest | Real-time data (sensor readings, stock prices) |
| `.bufferingOldest(n)` | Keep oldest N, drop newest | Audit logging where first events matter |

```swift
// MEMORY LEAK -- unbounded buffer with high-frequency producer
let stream = AsyncStream<SensorReading>(bufferingPolicy: .unbounded) { continuation in
    sensor.onReading = { reading in
        continuation.yield(reading) // 100 readings/sec, consumer processes 10/sec
    }
}

// FIX -- bounded buffer with newest-wins policy
let stream = AsyncStream<SensorReading>(bufferingPolicy: .bufferingNewest(10)) { continuation in
    sensor.onReading = { reading in
        continuation.yield(reading) // Drops oldest when buffer is full
    }
}
```

**For true backpressure:** Use `AsyncChannel` from swift-async-algorithms (where `send()` suspends until the consumer iterates) or `AsyncStream(unfolding:)` which naturally provides backpressure since the closure runs only when the consumer asks for the next value.

---

## Use withDiscardingTaskGroup for Long-Running Services 🟡

Regular `withTaskGroup` accumulates completed task results in memory until you call `next()`. For servers, observers, or connection acceptors that run indefinitely, this causes unbounded memory growth.

```swift
// MEMORY LEAK -- completed task results accumulate forever
try await withThrowingTaskGroup(of: Void.self) { group in
    for await connection in server.connections {
        group.addTask {
            await handleConnection(connection) // Result accumulates even though it's Void
        }
    }
    // Never reaches here -- connections is infinite
}

// FIX -- withDiscardingTaskGroup destroys tasks on completion
try await withThrowingDiscardingTaskGroup { group in
    for await connection in server.connections {
        group.addTask {
            await handleConnection(connection) // Result discarded immediately
        }
    }
}
```

**Bonus:** `withDiscardingTaskGroup` follows "one for all, all for one" error handling — a single child failure throws immediately and cancels all siblings. Regular `withThrowingTaskGroup` requires explicitly iterating `next()` to surface child errors.

**Availability:** Swift 5.9+ / Xcode 15+

---

## Task.detached Strips Priority, Task-Locals, and Cancellation 🟠

`Task.detached` doesn't just remove actor isolation — it also strips:
- **Priority inheritance** — the detached task gets `.medium` unless explicitly set
- **Task-local values** — logging context, distributed tracing IDs, request metadata all lost
- **Cancellation propagation** — parent cancellation does NOT cancel detached children

```swift
// LOST CONTEXT -- task-local tracing ID disappears
@TaskLocal static var requestID: String?

func handleRequest() async {
    Self.$requestID.withValue("req-123") {
        Task.detached {
            print(Self.requestID) // nil -- task-local stripped
            await processRequest() // Logs have no request correlation
        }
    }
}

// FIX -- use nonisolated function to escape actor while keeping context
func handleRequest() async {
    Self.$requestID.withValue("req-123") {
        Task {
            await doProcessing() // requestID is "req-123", priority inherited
        }
    }
}

nonisolated func doProcessing() async {
    // Runs off MainActor but keeps task-local values and cancellation
}
```

**When Task.detached IS appropriate:**
- You explicitly need to break from the parent's cancellation scope
- You need a fresh priority different from the parent's
- You're creating a truly independent background operation

---

## AsyncStream Creation Patterns

### Modern: `makeStream(of:)` (Swift 5.9+)

```swift
let (stream, continuation) = AsyncStream.makeStream(of: Event.self)

// Producer side
continuation.yield(event)
continuation.finish()

// Consumer side
for await event in stream { handle(event) }
```

### Legacy: Closure-based

```swift
let stream = AsyncStream<Event> { continuation in
    source.onEvent = { continuation.yield($0) }
    source.onDone = { continuation.finish() }
    continuation.onTermination = { _ in source.stop() }
}
```

| Approach | Advantage | Disadvantage |
|----------|-----------|--------------|
| `makeStream` | Continuation accessible outside closure; testable | Requires Swift 5.9+ |
| Closure-based | Available from Swift 5.5 | Continuation trapped in closure scope |
| `AsyncStream(unfolding:)` | Natural backpressure | Pull-based only, no push |

**For tests:** Always prefer `makeStream` — inject the continuation into production code, control timing from the test.
