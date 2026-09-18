# Testing & Debugging Concurrent Code

## How to Use This Reference

Read this when writing tests for async code, debugging flaky tests caused by non-deterministic scheduling, setting up Thread Sanitizer in CI, or profiling concurrency performance with Instruments.

---

## Use withMainSerialExecutor for Deterministic Async Tests 🟢

Point-Free's `epam-swift-concurrency-extras` provides `withMainSerialExecutor {}` which forces all async work to execute serially on the main thread. Without it, tests asserting intermediate state (e.g., `isLoading = true` between start and completion) fail nearly 100% of the time.

```swift
// FLAKY -- scheduler decides when Task body runs
@MainActor func testLoadingState() async {
    let viewModel = ViewModel()
    viewModel.loadData()
    XCTAssertTrue(viewModel.isLoading) // Race: load may already finish
}

// DETERMINISTIC -- serial execution
@MainActor func testLoadingState() async {
    await withMainSerialExecutor {
        let viewModel = ViewModel(apiClient: mockClient)
        viewModel.loadData()
        await Task.yield() // Allow the task to start
        XCTAssertTrue(viewModel.isLoading)
        // Fulfill mock response...
        await Task.yield()
        XCTAssertFalse(viewModel.isLoading)
    }
}
```

**Critical caveat:** Insert `await Task.yield()` inside injected closures that don't perform real async work. Swift can inline synchronous closures past the suspension point, skipping the intermediate state entirely.

**Dependency:** `epam-swift-concurrency-extras` (Point-Free, MIT license)

---

## Inject Clock Protocol for Time-Dependent Code 🟢

Never use `Task.sleep` or `ContinuousClock` directly in business logic. Inject a `Clock` conforming type for testability.

```swift
// UNTESTABLE -- hardcoded sleep
class Poller {
    func startPolling() async {
        while !Task.isCancelled {
            await fetchData()
            try? await Task.sleep(for: .seconds(30)) // Cannot control in tests
        }
    }
}

// TESTABLE -- injected clock
class Poller<C: Clock> where C.Duration == Duration {
    private let clock: C
    init(clock: C) { self.clock = clock }

    func startPolling() async {
        while !Task.isCancelled {
            await fetchData()
            try? await clock.sleep(for: .seconds(30)) // Controllable in tests
        }
    }
}

// Production: Poller(clock: ContinuousClock())
// Tests:      Poller(clock: ImmediateClock()) -- no waiting
// Advanced:   Poller(clock: TestClock())      -- manual time advancement
```

| Clock Type | Behavior | Use |
|-----------|----------|-----|
| `ContinuousClock` | Real wall-clock time | Production |
| `ImmediateClock` | Returns immediately | Unit tests, SwiftUI previews |
| `TestClock` | Manual `.advance(by:)` | Tests needing precise time control |

---

## Use await fulfillment(of:) Instead of wait(for:) 🟡

The synchronous `wait(for:timeout:)` blocks the current thread waiting for an expectation — if the fulfillment needs the same thread, it deadlocks. In Swift 6, the synchronous version is a hard error in async contexts.

```swift
// DEADLOCK -- blocks thread that needs to fulfill the expectation
func testAsync() async {
    let expectation = expectation(description: "done")
    Task { @MainActor in
        expectation.fulfill() // Needs main thread, but it's blocked
    }
    wait(for: [expectation], timeout: 5) // Blocks main thread
}

// FIX -- async fulfillment
func testAsync() async {
    let expectation = expectation(description: "done")
    Task { @MainActor in
        expectation.fulfill()
    }
    await fulfillment(of: [expectation], timeout: 5) // Suspends, doesn't block
}
```

---

## Mark Test Classes @MainActor for UI-Isolated Code 🟢

When testing `@MainActor`-isolated ViewModels or UI components, annotate the test class with `@MainActor`. In Swift Testing (vs XCTest), test methods can run on any thread — `@MainActor` is always required for MainActor-isolated code.

```swift
// XCTest
@MainActor
final class ViewModelTests: XCTestCase {
    func testInitialState() {
        let vm = ViewModel() // @MainActor type -- safe to create
        XCTAssertEqual(vm.items.count, 0)
    }

    func testLoadData() async {
        let vm = ViewModel(client: MockClient())
        await vm.loadData()
        XCTAssertEqual(vm.items.count, 3)
    }
}

// Swift Testing
@MainActor
struct ViewModelTests {
    @Test func initialState() {
        let vm = ViewModel()
        #expect(vm.items.isEmpty)
    }

    @Test func loadData() async {
        let vm = ViewModel(client: MockClient())
        await vm.loadData()
        #expect(vm.items.count == 3)
    }
}
```

---

## Write Both Deterministic and Realistic Tests 🟢

**Deterministic tests** (via `withMainSerialExecutor`) verify precise state transitions — `isLoading` toggles at exactly the right time, errors surface in the right order. **Non-deterministic tests** on the real global executor catch true concurrency bugs that only appear under parallelism.

| Test Type | Purpose | Tools |
|-----------|---------|-------|
| Deterministic | Verify state transitions, business logic | `withMainSerialExecutor`, `ImmediateClock` |
| Realistic | Catch data races, deadlocks, reentrancy | Real executor, `ContinuousClock`, TSan |
| Stress | Find intermittent failures | `concurrentPerform`, repeated runs, TSan |

Both are needed: deterministic tests prevent regression, realistic tests validate safety.

---

## Use AsyncStream.makeStream for Controllable Test Streams 🟢

`AsyncStream.makeStream(of:)` returns a stream/continuation pair. Inject the continuation into production code; control timing from the test.

```swift
// Production code accepts any AsyncStream
class EventProcessor {
    func process(events: AsyncStream<Event>) async {
        for await event in events { handle(event) }
    }
}

// Test controls exactly when events arrive
@Test func testEventProcessing() async {
    let (stream, continuation) = AsyncStream.makeStream(of: Event.self)
    let processor = EventProcessor()

    Task { await processor.process(events: stream) }

    continuation.yield(.login)
    // Assert intermediate state...
    continuation.yield(.purchase)
    // Assert...
    continuation.finish()
}
```

---

## Abstract Task Creation for Dependency Injection 🟢

Replace direct `Task { }` calls with a protocol to control task execution in tests.

```swift
// Protocol for task creation
protocol TaskSpawning: Sendable {
    func spawn(_ operation: @escaping @Sendable () async -> Void)
}

// Production implementation
struct LiveTaskSpawner: TaskSpawning {
    func spawn(_ operation: @escaping @Sendable () async -> Void) {
        Task { await operation() }
    }
}

// Test implementation -- captures and controls execution
final class MockTaskSpawner: TaskSpawning, @unchecked Sendable {
    private let lock = NSLock()
    private var operations: [@Sendable () async -> Void] = []

    func spawn(_ operation: @escaping @Sendable () async -> Void) {
        lock.withLock { operations.append(operation) }
    }

    func runAll() async {
        let ops = lock.withLock { let o = operations; operations = []; return o }
        for op in ops { await op() }
    }
}
```

---

## Thread Sanitizer Platform Notes 🟢

| Platform | TSan Support | Notes |
|----------|-------------|-------|
| macOS (native) | ✅ Full | Best support, recommended for CI |
| iOS Simulator | ✅ Full | Use for iOS-targeted tests |
| iOS Device | ❌ Not available | Cannot run TSan on physical devices |
| Linux | ⚠️ ~50% false positives | Swift Concurrency primitives not fully modeled |

**TSan cannot run simultaneously with ASan** (Address Sanitizer). Run them in separate CI jobs.

**Enable in Xcode:** Edit Scheme → Test → Diagnostics → Thread Sanitizer

**Enable in CI:** `xcodebuild test -enableThreadSanitizer YES`

---

## Swift Concurrency Instrument in Instruments 🟢

The Swift Concurrency template (Xcode 14+, iOS 16+) provides:

- **Task creation and execution** per thread — see which tasks run where
- **Task hierarchy** visualization — parent/child relationships
- **Stuck continuations** detection — find leaked or long-running suspensions
- **Actor contention** — identify actors with high wait times

**Key pattern to look for:** When a task unexpectedly runs on the main thread (MainActor inheritance), the Swift Tasks lane makes this immediately visible. Pin child tasks to the timeline for focused analysis.

---

## Always Set Timeouts on Concurrent Tests 🟢

A leaked continuation, infinite stream, or deadlock will hang the entire test suite indefinitely.

```swift
// Swift Testing -- built-in timeout
@Test(.timeLimit(.minutes(1)))
func testConcurrentOperation() async {
    // Test that might hang if continuation is never resumed
}

// XCTest -- timeout on expectations
func testAsyncOperation() async {
    let expectation = expectation(description: "operation completes")
    // ... trigger operation ...
    await fulfillment(of: [expectation], timeout: 10) // 10 second timeout
}
```

**CI:** Set a global test timeout at the xcodebuild level: `xcodebuild test -maximum-test-execution-time-allowance 120`

---

## Test Template

```swift
import XCTest
@testable import YourModule

@MainActor
final class FeatureViewModelTests: XCTestCase {
    private var sut: FeatureViewModel!
    private var mockClient: MockAPIClient!

    override func setUp() {
        mockClient = MockAPIClient()
        sut = FeatureViewModel(client: mockClient)
    }

    override func tearDown() {
        sut = nil
        mockClient = nil
    }

    // Deterministic: verify state transitions
    func testLoadingStateTransitions() async {
        await withMainSerialExecutor {
            sut.loadData()
            await Task.yield()
            XCTAssertTrue(sut.isLoading)
            mockClient.completeWithSuccess()
            await Task.yield()
            XCTAssertFalse(sut.isLoading)
            XCTAssertEqual(sut.items.count, 3)
        }
    }

    // Realistic: verify concurrent safety
    func testConcurrentLoads() async {
        async let load1: () = sut.loadData()
        async let load2: () = sut.loadData()
        _ = await (load1, load2)
        // Should not crash, data should be consistent
        XCTAssertFalse(sut.isLoading)
    }
}
```
