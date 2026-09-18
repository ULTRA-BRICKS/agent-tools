# Testing UIKit MVVM

<test_generation_rules>## Test Structure: Arrange → Act → Assert + Memory Leak Check

```swift
final class ItemListViewModelTests: XCTestCase {
    private var sut: ItemListViewModel!
    private var mockRepository: MockItemRepository!
    private var cancellables: Set<AnyCancellable>!

    override func setUp() {
        super.setUp()
        mockRepository = MockItemRepository()
        sut = ItemListViewModel(repository: mockRepository)
        cancellables = []
    }

    override func tearDown() {
        addTeardownBlock { [weak sut = self.sut] in
            XCTAssertNil(sut, "Possible retain cycle")
        }
        sut = nil; mockRepository = nil; cancellables = nil
        super.tearDown()
    }

    func testFetchUpdatesStateToLoaded() {
        mockRepository.stubbedItems = [Item.sample(), Item.sample(name: "Second")]
        let exp = XCTestExpectation(description: "State updates")

        sut.$state.dropFirst()
            .sink { state in
                if case .loaded(let items) = state { XCTAssertEqual(items.count, 2); exp.fulfill() }
            }
            .store(in: &cancellables)

        sut.fetch()
        wait(for: [exp], timeout: 2.0)
    }
}
```

## Testing Combine Publishers

Use `XCTestExpectation` + `sink` + `dropFirst()` (skip initial value on subscribe).

**async context gotcha**: Use `await fulfillment(of:)`, never `wait(for:)` — deadlocks in async.

## Testing async/await ViewModels

```swift
@MainActor
func testLoadProfile() async {
    let vm = ProfileViewModel(service: mockService)
    await vm.loadProfile()
    XCTAssertNotNil(vm.profile)
    XCTAssertEqual(mockService.fetchProfileCallCount, 1)
}
```

## Mock Pattern

```swift
final class MockItemRepository: ItemRepositoryProtocol {
    var stubbedItems: [Item] = []
    var stubbedError: Error?
    var fetchItemsCallCount = 0

    func fetchItems() async throws -> [Item] {
        fetchItemsCallCount += 1
        if let error = stubbedError { throw error }
        return stubbedItems
    }
    // ... similar for save, delete
}
```

## Memory Leak Detection

```swift
extension XCTestCase {
    func assertNoMemoryLeak(_ instance: AnyObject, file: StaticString = #filePath, line: UInt = #line) {
        addTeardownBlock { [weak instance] in
            XCTAssertNil(instance, "Potential memory leak", file: file, line: line)
        }
    }
}
// Usage: assertNoMemoryLeak(sut)
```

## Testing Coordinators

```swift
final class MockNavigationController: UINavigationController {
    var pushCalled = false
    var lastPushedVC: UIViewController?
    override func pushViewController(_ vc: UIViewController, animated: Bool) {
        super.pushViewController(vc, animated: animated)
        pushCalled = true; lastPushedVC = vc
    }
}

func testStartPushesHomeVC() {
    let nav = MockNavigationController()
    let coordinator = HomeCoordinator(navigationController: nav, factory: MockFactory())
    coordinator.start()
    XCTAssertTrue(nav.pushCalled)
    XCTAssertTrue(nav.lastPushedVC is HomeViewController)
}
```

## Test File Organization

```text
Tests/
├── ViewModelTests/       # One file per ViewModel
├── CoordinatorTests/     # One file per Coordinator
├── RepositoryTests/
├── Mocks/                # MockItemRepository, MockNavigationController, MockFactory, etc.
└── Helpers/              # Item+Sample.swift, XCTestCase+Extensions.swift
```

**Test data factory**: `extension Item { static func sample(id: UUID = .init(), name: String = "Test") -> Item { ... } }`
</test_generation_rules>
