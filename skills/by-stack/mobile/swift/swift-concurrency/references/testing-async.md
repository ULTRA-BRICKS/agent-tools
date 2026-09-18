# Testing Async Code

Patterns for writing deterministic tests for Swift Concurrency code.

## Swift Testing (Preferred)

Swift Testing supports async test functions natively:

```swift
@Test func userLoads() async throws {
    let user = try await UserService().load(id: "123")
    #expect(user.name == "Alice")
}
```

**Don't** wrap async work in `Task {}` or use expectations/semaphores in Swift Testing.

## Testing Actor State

Access actor properties through `await`, just like production code:

```swift
@Test func cachingWorks() async throws {
    let cache = ImageCache()
    let image = try await cache.image(for: testURL)
    let cached = try await cache.image(for: testURL)
    #expect(image == cached)
}
```

**Don't** add `nonisolated` accessors just for testing.

## Confirmation for Async Events

When testing that an async event fires (callback, notification, stream value):

```swift
@Test func notificationFires() async {
    await confirmation { confirmed in
        let task = Task {
            for await _ in NotificationCenter.default.notifications(named: .dataDidChange) {
                confirmed()
                break
            }
        }

        await Task.yield()  // Let listener start before posting
        NotificationCenter.default.post(name: .dataDidChange, object: nil)
        await task.value
    }
}
```

**Important**: All async work must complete before `confirmation()` closure returns.

## .serialized Trait (Common Misconception)

`.serialized` **only affects parameterized tests**. It tells Swift Testing to run argument cases sequentially, not in parallel.

```swift
// .serialized controls parameterized cases only
@Test(.serialized, arguments: ["alice", "bob", "charlie"])
func accountCreation(username: String) async throws {
    let account = try await AccountService().create(username: username)
    #expect(account.isActive)
}
```

Applying `.serialized` to non-parameterized tests does nothing.

## XCTest Async Patterns

### Modern: await fulfillment

```swift
func testAsyncOperation() async throws {
    let expectation = expectation(description: "data loaded")

    Task {
        await viewModel.load()
        expectation.fulfill()
    }

    await fulfillment(of: [expectation], timeout: 5)
    XCTAssertNotNil(viewModel.data)
}
```

### Deprecated: wait() unavailable in async

```swift
// ERROR: wait(...) is unavailable from asynchronous contexts
func testAsync() async {
    let exp = expectation(description: "done")
    wait(for: [exp])  // ❌ Unavailable
}

// FIX: Use await fulfillment
func testAsync() async {
    let exp = expectation(description: "done")
    await fulfillment(of: [exp])  // ✅
}
```

## Deterministic Testing

### Problem: Timing-based tests are flaky

```swift
// BAD: Depends on timing, may flake
@Test func debounceWorks() async throws {
    viewModel.search("query")
    try await Task.sleep(for: .milliseconds(350))  // Fragile
    #expect(viewModel.results.count > 0)
}
```

### Solution: Inject Clock protocol

```swift
// Production
actor Debouncer<C: Clock> where C.Duration == Duration {
    let clock: C
    let duration: Duration

    func debounce<T>(_ work: @escaping () async -> T) async -> T {
        try? await clock.sleep(for: duration)
        return await work()
    }
}

// Test with immediate clock
@Test func debounceWorks() async {
    let debouncer = Debouncer(clock: ImmediateClock(), duration: .milliseconds(300))
    let result = await debouncer.debounce { 42 }
    #expect(result == 42)  // Instant, deterministic
}
```

## withMainSerialExecutor

Forces all `@MainActor` work to serialize for deterministic testing:

```swift
@Test func viewModelUpdatesSequentially() async {
    await withMainSerialExecutor {
        let viewModel = ViewModel()
        await viewModel.loadData()
        #expect(viewModel.state == .loaded)
    }
}
```

## Testing Cancellation

```swift
@Test func cancelledTaskStops() async {
    let viewModel = ViewModel()
    let task = Task { await viewModel.startLongOperation() }

    try? await Task.sleep(for: .milliseconds(50))
    task.cancel()

    try? await Task.sleep(for: .milliseconds(50))
    #expect(viewModel.isCancelled)
}
```

## Testing Actor Reentrancy

```swift
@Test func cacheHandlesReentrancy() async throws {
    let cache = Cache()

    // Concurrent requests for same key
    async let result1 = cache.fetch("key")
    async let result2 = cache.fetch("key")
    async let result3 = cache.fetch("key")

    let results = try await [result1, result2, result3]

    // All should get same value (no duplicate fetches)
    #expect(Set(results).count == 1)
}
```

## Testing Retain Cycles

```swift
@Test func viewModelDeallocates() async {
    var viewModel: ViewModel? = ViewModel()
    weak var weakViewModel = viewModel

    viewModel?.startPolling()
    viewModel = nil

    try? await Task.sleep(for: .milliseconds(100))

    #expect(weakViewModel == nil, "ViewModel should deallocate")
}
```

## Thread Sanitizer in Tests

Enable TSan in test scheme:
1. Edit Scheme → Test → Diagnostics
2. Enable "Thread Sanitizer"

Or in CI:
```bash
xcodebuild test -enableThreadSanitizer YES
```

## Checklist

- [ ] Async tests are `async` functions, not `Task {}` wrappers
- [ ] Use `confirmation()` for async event testing
- [ ] Inject `Clock` for time-dependent code
- [ ] Use `withMainSerialExecutor` for deterministic MainActor tests
- [ ] Test cancellation paths explicitly
- [ ] Test for retain cycles with weak references
- [ ] Run tests with Thread Sanitizer enabled
