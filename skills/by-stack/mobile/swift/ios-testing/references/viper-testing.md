# VIPER / Clean Architecture Testing

## How to Use This Reference

Read this when testing VIPER modules (Presenter, Interactor, Router), verifying module assembly and wiring, checking for memory leaks across VIPER components, or setting up mocks for VIPER protocol boundaries.

---

## Presenter Testing

Presenter tests inject mock View + mock Interactor. Assert:
- (a) user actions delegate to correct Interactor methods
- (b) Interactor output maps entities to ViewModels and calls correct View methods

```swift
func test_viewDidLoad_triggersInteractorFetch() {
    sut.viewDidLoad()
    XCTAssertTrue(mockInteractor.fetchItemsCalled)
}

func test_didFetchItems_mapsToViewModelAndUpdatesView() {
    sut.didFetch(items: [Item(firstName: "Jane", lastName: "Doe")])
    XCTAssertEqual(mockView.displayedViewModels.first?.title, "Jane Doe")
}
```

Always nil out sut/mocks in `tearDown` to prevent cross-test leakage and catch retain cycles.

---

## Interactor Testing

Interactor tests inject stub/mock services. Test pure business logic -- filtering, sorting, validation -- WITHOUT touching network or database.

```swift
func test_filterItems_returnsMatchingItems() {
    mockService.stubbedItems = [
        Item(name: "Apple"), Item(name: "Banana"), Item(name: "Apricot")
    ]

    let result = sut.filterItems(query: "Ap")

    XCTAssertEqual(result.count, 2)
    XCTAssertTrue(result.allSatisfy { $0.name.hasPrefix("Ap") })
}
```

---

## Router Testing

Spy on `UINavigationController` -- verify correct VC type pushed. Never present on a real UIWindow in unit tests.

```swift
class SpyNavigationController: UINavigationController {
    var pushedViewController: UIViewController?
    override func pushViewController(_ vc: UIViewController, animated: Bool) {
        pushedViewController = vc
        super.pushViewController(vc, animated: false)
    }
}
```

**GOTCHA:** Router holds `weak var viewController` -- if test doesn't hold strong reference, VC deallocs before assertion. Always store a strong reference in the test.

---

## Entity Boundaries

Presenter must map Entity to ViewModel. View protocol methods accept only ViewModels or primitives, NEVER domain entities.

```swift
// WRONG -- View receives domain entity
protocol ItemViewProtocol {
    func display(items: [Item]) // domain entity leaks into View
}

// CORRECT -- View receives ViewModel
protocol ItemViewProtocol {
    func display(viewModels: [ItemViewModel]) // presentation layer type
}
```

---

## Module Assembly Testing

Test module assembly/factory: verify all VIPER components are wired. Use `makeAndExpose()` pattern.

```swift
func test_moduleAssembly_wiresAllComponents() {
    let module = ItemModule.build()

    XCTAssertNotNil(module.view)
    XCTAssertNotNil(module.presenter)
    XCTAssertNotNil(module.interactor)
    XCTAssertNotNil(module.router)
}
```

---

## Weak Reference Enforcement

After releasing strong reference to VC, ALL VIPER components must dealloc. Use `weak var` + `XCTAssertNil` pattern.

```swift
func test_module_doesNotLeakMemory() {
    var view: ItemViewController? = ItemModule.build().view as? ItemViewController
    weak var weakPresenter = (view as? ItemViewProtocol)?.presenter
    weak var weakInteractor = weakPresenter?.interactor

    view = nil

    XCTAssertNil(weakPresenter, "Presenter leaked!")
    XCTAssertNil(weakInteractor, "Interactor leaked!")
}
```

**Critical rules:**
- Presenter -> View reference MUST be `weak`
- Interactor -> Presenter (output) MUST be `weak`
- Forgetting either leaks the entire module

---

## Mock Management

### Auto-generation

Use Sourcery `AutoMockable` or SwiftyMocky to auto-generate mocks. Never hand-write mocks for VIPER protocol boilerplate.

### Mock tracking

Every mock should track:
- `wasCalled: Bool`
- `callCount: Int`
- `receivedArguments`

### Focused protocols

Split fat protocols into focused role-based protocols. Each mock implements only methods it needs.

### Class constraints

All View protocols must be class-constrained (`AnyObject`) so Presenter can hold `weak` reference.

```swift
protocol ItemViewProtocol: AnyObject {
    func displayItems(_ viewModels: [ItemViewModel])
    func displayError(_ message: String)
}
```

### Contract files

Define ALL module protocols in a single `{Module}Contract.swift` file.

---

## Modern VIPER Patterns

### Async Interactor

Mark Interactor methods as `async throws`. Test methods are `async throws` -- no more XCTestExpectation for simple async flows.

```swift
protocol ItemInteractorProtocol {
    func fetchItems() async throws -> [Item]
}

@Test func fetchItems_success() async throws {
    mockService.stubbedItems = [.sample()]
    let items = try await sut.fetchItems()
    #expect(items.count == 1)
}
```

### Error Propagation

Test error propagation at each boundary: Service -> Interactor -> Presenter -> View. Each layer transforms errors appropriately.

```swift
func test_fetchItems_serviceError_presenterMapsToUserMessage() async {
    mockService.stubbedError = NetworkError.timeout

    await sut.fetchItems()

    XCTAssertEqual(mockView.displayedError, "Unable to load items. Please try again.")
}
```
