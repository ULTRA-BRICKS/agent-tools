# XCTest Patterns Reference

## How to Use This Reference

Read this when writing or reviewing XCTest-based tests: setting up test classes, building mock objects, adding memory leak detection, testing Combine publishers, testing @Published subscription timing, injecting schedulers, or structuring complex test setups. These patterns apply to projects on Xcode 15 or older, or when XCTestCase is required (UI testing, performance testing).

---

## XCTestCase Structure

Every `XCTestCase` subclass follows this template:

```swift
import XCTest
@testable import MyApp

@MainActor
final class ItemListViewModelTests: XCTestCase {

    // MARK: - System Under Test

    private var sut: ItemListViewModel!
    private var mockRepository: MockItemRepository!

    // MARK: - Lifecycle

    override func setUp() {
        super.setUp()
        mockRepository = MockItemRepository()
        sut = ItemListViewModel(repository: mockRepository)
    }

    override func tearDown() {
        addTeardownBlock { [weak sut = self.sut] in
            XCTAssertNil(sut, "ItemListViewModel leaked -- possible retain cycle")
        }
        sut = nil
        mockRepository = nil
        super.tearDown()
    }

    // MARK: - Tests

    func test_loadItems_success_showsLoadedItems() async {
        // Arrange
        mockRepository.stubbedItems = [.sample(name: "First"), .sample(name: "Second")]

        // Act
        await sut.loadItems()

        // Assert
        XCTAssertEqual(sut.items.count, 2)
        XCTAssertEqual(mockRepository.fetchItemsCallCount, 1)
    }

    func test_loadItems_networkError_showsFailedState() async {
        mockRepository.stubbedError = URLError(.notConnectedToInternet)

        await sut.loadItems()

        guard case .failed(let error) = sut.state else {
            return XCTFail("Expected .failed state, got \(sut.state)")
        }
        XCTAssertNotNil(error)
    }
}
```

**Naming convention**: `test_<method>_<condition>_<expectedResult>` makes failures self-documenting.

---

## Mock Pattern: Stub + Spy

Every protocol dependency gets a mock with:
- `stubbed*` properties for controlling what the mock returns
- `*CallCount` for verifying methods were called
- `*Calls` arrays for inspecting arguments when order matters
- `reset()` for resetting state in complex multi-step tests

```swift
@MainActor
final class MockItemRepository: ItemRepositoryProtocol {

    // MARK: - Stubs (what to return)
    var stubbedItems: [Item] = []
    var stubbedSavedItem: Item?
    var stubbedError: Error?

    // MARK: - Spies (what was called)
    var fetchItemsCallCount = 0
    var saveItemCallCount = 0
    var lastSavedItem: Item?
    var deleteItemsCallCount = 0
    var lastDeletedItems: [Item] = []

    // MARK: - Protocol Conformance
    func fetchItems() async throws -> [Item] {
        fetchItemsCallCount += 1
        if let error = stubbedError { throw error }
        return stubbedItems
    }

    func saveItem(_ item: Item) async throws -> Item {
        saveItemCallCount += 1
        lastSavedItem = item
        if let error = stubbedError { throw error }
        return stubbedSavedItem ?? item
    }

    func deleteItems(_ items: [Item]) async throws {
        deleteItemsCallCount += 1
        lastDeletedItems = items
        if let error = stubbedError { throw error }
    }

    func reset() {
        stubbedItems = []; stubbedSavedItem = nil; stubbedError = nil
        fetchItemsCallCount = 0; saveItemCallCount = 0; lastSavedItem = nil
        deleteItemsCallCount = 0; lastDeletedItems = []
    }
}
```

### Protocol vs closure DI

Rule of thumb: 1-2 methods -> closure-based DI. 3+ methods or need call count tracking -> protocol-based DI.

```swift
// Closure-based -- zero mock classes
let vm = ProductVM(reloading: { Product.stub() })

// Protocol-based -- clear API contract
class UserVM { init(service: UserServiceProtocol) { } }
```

---

## Memory Leak Detection

```swift
// In tearDown():
addTeardownBlock { [weak sut = self.sut] in
    XCTAssertNil(sut, "ItemListViewModel has a memory leak")
}
sut = nil
mockRepository = nil

// Reusable helper (add to XCTestCase+Extensions.swift):
extension XCTestCase {
    func assertNoMemoryLeak(
        _ instance: AnyObject,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        addTeardownBlock { [weak instance] in
            XCTAssertNil(
                instance,
                "Potential memory leak: \(type(of: instance!))",
                file: file,
                line: line
            )
        }
    }
}
```

---

## Test Data Factories

```swift
extension Item {
    static func sample(
        id: UUID = UUID(),
        name: String = "Sample Item",
        price: Double = 9.99,
        isAvailable: Bool = true
    ) -> Item {
        Item(id: id, name: name, price: price, isAvailable: isAvailable)
    }
}

// Usage: let item = Item.sample(name: "Draft Item")
```

---

## Testing Combine Publishers (@Published)

### Always dropFirst()

`@Published` emits current value immediately on subscription. Always use `dropFirst()`.

```swift
// WRONG -- collects initial empty array
let publisher = viewModel.$items.collect(2).first()

// CORRECT
let publisher = viewModel.$items.dropFirst().collect(2).first()
```

### Subscribe BEFORE triggering action

Otherwise emission races with subscription.

```swift
// WRONG
viewModel.search("Swift")
let value = try awaitPublisher(viewModel.$results.dropFirst().first())

// CORRECT
let publisher = viewModel.$results.dropFirst().first()
viewModel.search("Swift")
let value = try awaitPublisher(publisher)
```

### @Published fires in willSet, NOT didSet

Assert the value from the sink closure, not `sut.property`.

```swift
// WRONG -- sut.beers may not be set yet
cancel = sut.$beers.dropFirst().sink { _ in exp.fulfill() }
// ... wait ...
XCTAssertEqual(sut.beers, expectedBeers) // may read stale value

// CORRECT -- capture delivered value
var receivedBeers: [Beer]?
cancel = sut.$beers.dropFirst().sink { receivedBeers = $0; exp.fulfill() }
```

### Cancellable cleanup

Store cancellables in a test property and nil them in `tearDown`. Leaked subscriptions cause cross-test pollution.

### Multiple fulfillments guard

Guard against multiple expectation fulfillments -- `CurrentValueSubject`/`@Published` emit multiple values. Use `dropFirst().first()` or `expectedFulfillmentCount`.

### Inverted expectations

Use inverted expectations (`shouldNotFire.isInverted = true`) to test that something does NOT happen.

### Assertions inside receiveValue are swallowed

Assertions inside `receiveValue` closures are swallowed by Combine. Capture value, assert AFTER wait.

### retry() off-by-one

`retry(N)` resubscribes N additional times (total attempts = N+1). AI gets this off by one.

---

## Scheduler Injection

Inject schedulers for debounce/throttle -- NEVER test real time delays.

```swift
// WRONG -- hardcoded scheduler
$query.debounce(for: .seconds(0.3), scheduler: DispatchQueue.main)

// CORRECT -- injectable
$query.debounce(for: .seconds(0.3), scheduler: scheduler)

// Test with TestScheduler:
scheduler.advance(by: .seconds(0.3))
XCTAssertEqual(mockService.searchCallCount, 1)
```

`ImmediateScheduler` collapses all time to zero -- will NOT work with `debounce`, `throttle`, `delay`. Use `TestScheduler` for those.

Any `receive(on:)` or `subscribe(on:)` in production makes the pipeline async. Inject those schedulers too.

---

## @MainActor in Tests

`@MainActor` on test classes is MANDATORY in Xcode 16+ / Swift 6 when testing @MainActor-isolated ViewModels.

```swift
// WRONG (Swift 6 error)
final class MyVMTests: XCTestCase {
    func test() { let vm = MyViewModel() } // Error: main actor-isolated init
}

// CORRECT
@MainActor final class MyVMTests: XCTestCase, Sendable {
    override func setUp() async throws { sut = MyViewModel() }
}
```

Use `setUp() async throws` and `tearDown() async throws` (not the non-throwing versions) in Xcode 16+.

---

## Async Testing Patterns

### await fulfillment vs wait(for:)

`waitForExpectations(timeout:)` for SYNCHRONOUS test methods. `await fulfillment(of:timeout:)` for ASYNC test methods.

```swift
// DEADLOCK -- NEVER use wait(for:) in async test methods
func testBroken() async {
    let exp = expectation(description: "done")
    Task { exp.fulfill() }
    waitForExpectations(timeout: 1) // HANGS FOREVER
}

// CORRECT
func testFixed() async {
    let exp = expectation(description: "done")
    Task { exp.fulfill() }
    await fulfillment(of: [exp], timeout: 1)
}
```

### ViewModel tests using Task {} internally

Use expectations -- not direct assertions. The test method returns before the Task completes.

### makeSUT() factory

Use `makeSUT()` factory instead of (or alongside) `setUp`. Avoids concurrency warnings in Swift 6.

---

## Asserting ViewState Enum

```swift
func test_loadItems_success_isInLoadedState() async {
    mockRepository.stubbedItems = [.sample(), .sample()]

    await sut.loadItems()

    guard case .loaded(let items) = sut.state else {
        return XCTFail("Expected .loaded, got \(sut.state)")
    }
    XCTAssertEqual(items.count, 2)
}
```

---

## Testing Coordinators

```swift
final class MockNavigationController: UINavigationController {
    private(set) var pushedViewControllers: [UIViewController] = []
    private(set) var presentedVCs: [UIViewController] = []

    override func pushViewController(_ vc: UIViewController, animated: Bool) {
        super.pushViewController(vc, animated: animated)
        pushedViewControllers.append(vc)
    }

    override func present(_ vc: UIViewController, animated: Bool, completion: (() -> Void)? = nil) {
        super.present(vc, animated: animated)
        presentedVCs.append(vc)
    }
}
```

---

## Performance Tests

Use `measure {}` blocks for performance regression detection. This has NO Swift Testing equivalent.

```swift
func test_sortPerformance_oneThousandItems() {
    let items = (0..<1000).map { Item.sample(name: "Item \($0)") }
    measure {
        _ = items.sorted { $0.name < $1.name }
    }
}
```

---

## Lifecycle Mapping (XCTest -> Swift Testing)

| XCTest | Swift Testing |
|--------|--------------|
| `setUp/tearDown` | `init/deinit` (class) or just `init` (struct) |
| `setUpWithError() throws` | `init() throws` |
| `addTeardownBlock` | No direct equivalent; use `defer` or class `deinit` |
| `continueAfterFailure = false` | Use `#require` for must-pass assertions |
| `XCTestExpectation + fulfill() + wait` | `confirmation()` |
| `expectedFulfillmentCount` | `confirmation(expectedCount:)` |
| `isInverted = true` | `confirmation(expectedCount: 0)` |

---

## Test File Organization

```text
Tests/
+-- ViewModelTests/
|   +-- ItemListViewModelTests.swift
|   +-- CartViewModelTests.swift
+-- CoordinatorTests/
|   +-- HomeCoordinatorTests.swift
+-- RepositoryTests/
|   +-- ItemRepositoryTests.swift    <- integration tests
+-- Mocks/
|   +-- MockItemRepository.swift
|   +-- MockNavigationController.swift
+-- Helpers/
    +-- Item+Sample.swift
    +-- XCTestCase+Extensions.swift  <- assertNoMemoryLeak, etc.
```

**One mock per file.** Keep mocks in `Mocks/` -- not nested inside test files.
