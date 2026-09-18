# OperationQueue & AsyncOperation

## How to Use This Reference

Read this when creating Operation subclasses, building dependency graphs, implementing cancellation, or throttling concurrent work with OperationQueue.

---

## When to Use OperationQueue vs GCD

| Need | Use |
|------|-----|
| Fire-and-forget async work | GCD (`queue.async`) |
| Dependency graphs (A before B before C) | OperationQueue |
| Cancellation of in-flight work | OperationQueue |
| Throttling (`maxConcurrentOperationCount`) | OperationQueue |
| Pause / resume all work | OperationQueue (`isSuspended`) |
| Progress reporting | OperationQueue (KVO on `progress`) |
| Simple synchronization | GCD (serial queue, barrier) |

---

## AsyncOperation Base Class

The default `Operation` lifecycle marks work as finished when `main()` returns. For async operations (network, disk I/O), you must manually manage `isExecuting` and `isFinished` with KVO notifications and thread-safe state.

```swift
class AsyncOperation: Operation {
    private let lock = NSLock()
    private var _isExecuting = false
    private var _isFinished = false

    override var isAsynchronous: Bool { true }

    override var isExecuting: Bool {
        get { lock.withLock { _isExecuting } }
        set {
            willChangeValue(forKey: "isExecuting")
            lock.withLock { _isExecuting = newValue }
            didChangeValue(forKey: "isExecuting")
        }
    }

    override var isFinished: Bool {
        get { lock.withLock { _isFinished } }
        set {
            willChangeValue(forKey: "isFinished")
            lock.withLock { _isFinished = newValue }
            didChangeValue(forKey: "isFinished")
        }
    }

    override func start() {
        guard !isCancelled else {
            finish()  // MUST still set isFinished!
            return
        }
        isExecuting = true
        main()
    }

    func finish() {
        isExecuting = false
        isFinished = true
    }
}
```

### Common AsyncOperation Mistakes

**Missing KVO notifications:** Queue never detects state change. Operations stall forever.

```swift
// BAD — no willChangeValue/didChangeValue
override var isFinished: Bool {
    get { _isFinished }
    set { _isFinished = newValue }  // Queue doesn't know!
}
```

**Not calling finish():** Operation never completes. Queue waits forever, blocking dependencies.

**Not checking isCancelled in start():** Operation runs even after cancellation, wasting resources.

**Not thread-safe state:** Multiple threads read/write `_isExecuting`/`_isFinished` without synchronization — data race.

**Calling super.start():** The default `start()` calls `main()` synchronously and marks as finished. For async operations, you override `start()` entirely — never call `super.start()`.

---

## Concrete Example: DownloadOperation

```swift
final class DownloadOperation: AsyncOperation {
    let url: URL
    var downloadedData: Data?
    var downloadError: Error?
    private var task: URLSessionDataTask?

    init(url: URL) {
        self.url = url
        super.init()
    }

    override func main() {
        task = URLSession.shared.dataTask(with: url) { [weak self] data, _, error in
            guard let self else { return }
            self.downloadedData = data
            self.downloadError = error
            self.finish()  // MUST call finish on every path
        }
        task?.resume()
    }

    override func cancel() {
        task?.cancel()
        super.cancel()
    }
}
```

---

## Dependency Graphs

```swift
let download = DownloadOperation(url: imageURL)
let resize = ResizeOperation()
let cache = CacheOperation()

// resize waits for download; cache waits for resize
resize.addDependency(download)
cache.addDependency(resize)

// Dependencies work ACROSS queues
let networkQueue = OperationQueue()
let processingQueue = OperationQueue()

networkQueue.addOperation(download)
processingQueue.addOperations([resize, cache], waitUntilFinished: false)
```

### Passing Data Between Operations

Use a protocol-based approach for type-safe data flow:

```swift
protocol DataProducer {
    var producedData: Data? { get }
}

extension DownloadOperation: DataProducer {
    var producedData: Data? { downloadedData }
}

final class ResizeOperation: AsyncOperation {
    var inputData: Data?

    override func main() {
        // Pull data from dependency
        let data = inputData ?? dependencies
            .compactMap { $0 as? DataProducer }
            .first?.producedData

        guard let data else {
            finish()
            return
        }
        // Process...
        finish()
    }
}
```

---

## Cancellation

Cancellation is **cooperative** — setting `isCancelled = true` doesn't stop execution. You must check it.

```swift
override func main() {
    guard !isCancelled else { finish(); return }

    let data = downloadSync()

    guard !isCancelled else { finish(); return }  // Check again after I/O

    process(data)
    finish()
}
```

**`completionBlock` fires regardless of cancellation.** If you need to distinguish, check `isCancelled` inside the completion:

```swift
operation.completionBlock = {
    if operation.isCancelled {
        print("Was cancelled")
    } else {
        print("Completed successfully")
    }
}
```

---

## QoS Promotion

OperationQueue automatically promotes QoS when a high-priority operation depends on a low-priority one:

```swift
let lowPriority = BlockOperation { fetchMetadata() }
lowPriority.qualityOfService = .background

let highPriority = BlockOperation { displayResults() }
highPriority.qualityOfService = .userInitiated
highPriority.addDependency(lowPriority)

// lowPriority is automatically promoted to .userInitiated
// because highPriority depends on it
```

---

## Throttling

```swift
let queue = OperationQueue()
queue.name = "com.app.imageDownload"
queue.maxConcurrentOperationCount = 4  // Never more than 4 simultaneous

// Dynamic throttling based on conditions
if isOnCellular {
    queue.maxConcurrentOperationCount = 2
} else {
    queue.maxConcurrentOperationCount = 6
}
```

---

## Pause / Resume

```swift
let queue = OperationQueue()

// Pause — no new operations start (in-flight continue)
queue.isSuspended = true

// Resume — queued operations begin executing
queue.isSuspended = false
```

**Note:** `isSuspended` only prevents NEW operations from starting. Already-running operations continue to completion.

---

## Serial OperationQueue as Lock Alternative

A serial OperationQueue can serve as a mutex-like construct with built-in cancellation and dependency support:

```swift
let serialQueue = OperationQueue()
serialQueue.maxConcurrentOperationCount = 1
serialQueue.name = "com.app.serialAccess"

// Each block executes one at a time, in order
serialQueue.addOperation { accessSharedResource() }
```

This is heavier than `NSLock` but useful when you also need cancellation, dependencies, or pause/resume.

---

## waitUntilAllOperationsAreFinished

```swift
// WARNING: This blocks the calling thread!
queue.waitUntilAllOperationsAreFinished()
```

**Never call on main thread** — causes deadlock if any operation dispatches to main. Use KVO on `operationCount` or add a completion operation as a dependency instead:

```swift
let completion = BlockOperation { print("All done") }
for op in operations {
    completion.addDependency(op)
}
queue.addOperations(operations + [completion], waitUntilFinished: false)
```
