# Data Races & Memory Safety

## How to Use This Reference

Read this when diagnosing Thread Sanitizer reports, fixing value type races, managing memory in concurrent contexts, or ensuring main thread compliance for UI updates.

---

## Swift Value Type COW Races

Swift value types (Array, Dictionary, String, Data) use Copy-on-Write. COW is NOT thread-safe — concurrent writes to the same buffer can corrupt memory.

```swift
// DATA RACE — concurrent mutation of Array's COW buffer
var results = [String]()
DispatchQueue.concurrentPerform(iterations: 100) { i in
    results.append("Result \(i)")  // COW buffer corruption
}
```

From swift.org: a program concurrently appending to an Array "may print '91', '94', or even crash."

### Fixes

```swift
// FIX 1: Serial queue for collection
let queue = DispatchQueue(label: "com.app.results")
var results = [String]()
DispatchQueue.concurrentPerform(iterations: 100) { i in
    let result = "Result \(i)"
    queue.sync { results.append(result) }  // sync — results available after loop
}

// FIX 2: Per-index assignment (safe ONLY with these preconditions:
//   1. Array is pre-allocated at fixed size — no append/remove/resize
//   2. Each index is accessed by exactly one iteration — disjoint writes
//   3. No concurrent reads from other threads during concurrentPerform)
var results = Array<String?>(repeating: nil, count: 100)
DispatchQueue.concurrentPerform(iterations: 100) { i in
    results[i] = "Result \(i)"  // Safe — fixed size, disjoint indices
}

// FIX 3: NSLock
let lock = NSLock()
var results = [String]()
DispatchQueue.concurrentPerform(iterations: 100) { i in
    let result = "Result \(i)"
    lock.withLock { results.append(result) }
}
```

---

## Real-World COW Race Fixes

### Alamofire (os_unfair_lock memory corruption)

Alamofire stored `os_unfair_lock` as a Swift property — Swift moved it in memory, corrupting the lock. Fixed by switching to `NSLock` and later `OSAllocatedUnfairLock`.

### Kingfisher (Dictionary race in image cache)

Kingfisher's image cache used a concurrent queue with barriers for Dictionary access. Under high load, a barrier write could overlap with a sync read on a different thread. Fixed by using `NSLock` for simpler, correct synchronization.

### SDWebImage (concurrent access to download tasks)

SDWebImage's download manager had races on the `URLSessionTask` dictionary. Multiple callbacks from URLSession could access the task map simultaneously. Fixed with `NSLock` protecting all dictionary access.

**Pattern:** All three libraries converged on `NSLock` for simplicity and correctness.

---

## Memory Management Rules for GCD

### One-Shot Callbacks

```swift
// SAFE — closure executes once, then released
DispatchQueue.global().async { [weak self] in
    // [weak self] is optional here (one-shot, no retain cycle)
    // but [weak self] is recommended if self might be deallocated
    self?.processData()
}
```

### Repeating / Stored Closures

```swift
// MUST use [weak self] — retain cycle
timer.setEventHandler { [weak self] in  // Repeating!
    self?.tick()
}

// MUST use [weak self] — stored closure
cancellable = publisher.sink { [weak self] value in
    self?.handle(value)
}
```

**Rule:** One-shot `async` can capture `self` strongly (it's released after execution). Repeating timers, stored closures, and notification observers MUST use `[weak self]`.

---

## Autoreleasepool for Batch Processing

When processing many items in a loop, Objective-C objects accumulate in the autorelease pool:

```swift
// BAD — memory spikes during processing
for item in largeDataset {
    let image = UIImage(data: item.data)  // Autoreleased
    processImage(image)
}

// GOOD — drain pool periodically
for item in largeDataset {
    autoreleasepool {
        let image = UIImage(data: item.data)
        processImage(image)
    }  // Pool drained here — memory stays flat
}
```

Also set `.workItem` autorelease frequency on queues that do batch work (see `queue-creation.md`).

---

## Main Thread Violations

UI updates MUST happen on the main thread. Violations cause:

- Purple runtime warnings in Xcode ("Publishing changes from background threads")
- Visual glitches (stale data, flicker)
- Undefined behavior (rare crashes in UIKit/AppKit)

### Detection

```swift
// Add at the start of any UI-updating method
func updateUI() {
    dispatchPrecondition(condition: .onQueue(.main))
    tableView.reloadData()
}
```

### Common Patterns

```swift
// Network callback → main thread
URLSession.shared.dataTask(with: url) { data, _, _ in
    // URLSession callbacks are on a background queue
    DispatchQueue.main.async {
        self.items = parseItems(data!)
        self.tableView.reloadData()
    }
}.resume()

// Combine: ensure UI updates on main
viewModel.$items
    .receive(on: DispatchQueue.main)  // Switch to main before UI update
    .sink { [weak self] items in
        self?.tableView.reloadData()
    }
    .store(in: &cancellables)
```

---

## Core Data Thread Safety

Core Data contexts are NOT thread-safe. Every access must happen on the context's queue.

```swift
// SAFE — perform block runs on context's queue
context.perform {
    let request = NSFetchRequest<User>(entityName: "User")
    let users = try? context.fetch(request)
    // Process users...
}

// SAFE — synchronous version (blocks calling thread)
context.performAndWait {
    let count = try? context.count(for: request)
}

// UNSAFE — accessing context from arbitrary queue
DispatchQueue.global().async {
    let users = try? context.fetch(request)  // DATA RACE
}
```

**Rules:**

- Always use `context.perform {}` or `context.performAndWait {}`
- Never pass `NSManagedObject` instances across threads — pass `NSManagedObjectID` instead
- Use `newBackgroundContext()` for background work, keep `viewContext` for main thread

---

## Combine Threading

Combine publishers can fire on any thread unless explicitly controlled:

```swift
// URLSession.DataTaskPublisher fires on URLSession's delegate queue (background)
URLSession.shared.dataTaskPublisher(for: url)
    .map(\.data)
    .decode(type: [Item].self, decoder: JSONDecoder())
    .receive(on: DispatchQueue.main)  // MUST switch to main before UI binding
    .sink(
        receiveCompletion: { _ in },
        receiveValue: { [weak self] items in
            self?.items = items  // Safe — on main thread
        }
    )
    .store(in: &cancellables)
```

**Rule:** Always add `.receive(on: DispatchQueue.main)` before any sink that updates UI or `@Published` properties observed by SwiftUI.
