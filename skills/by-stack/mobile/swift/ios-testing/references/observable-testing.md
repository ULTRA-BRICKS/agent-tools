# Testing @Observable ViewModels (SwiftUI)

## How to Use This Reference

Read this when testing ViewModels that use the `@Observable` macro (iOS 17+), migrating tests from `ObservableObject`/`@Published` patterns, testing navigation state, or verifying `@Observable` property tracking behavior. For `@Published`/Combine ViewModel testing, see `xctest-patterns.md`.

---

## @Observable vs ObservableObject — Testing Impact

@Observable removes Combine entirely. There is no `$property` publisher to sink on. Testing patterns must change.

```swift
// WRONG -- won't compile with @Observable
cancel = sut.$beers.dropFirst().sink { _ in exp.fulfill() }

// CORRECT -- use withObservationTracking
withObservationTracking { _ = sut.beers } onChange: { exp.fulfill() }
```

---

## withObservationTracking Fundamentals

### Single-fire registration

`withObservationTracking` fires its `onChange` callback only ONCE per registration. You must re-register for subsequent changes.

### willSet semantics

`onChange` uses willSet semantics -- the new value is NOT yet available inside the `onChange` closure.

```swift
// WRONG -- new value not available in onChange
withObservationTracking { _ = sut.name } onChange: {
    XCTAssertEqual(sut.name, "New") // FAILS -- still "Old"
}

// CORRECT -- assert AFTER waitForExpectations
withObservationTracking { _ = sut.name } onChange: { exp.fulfill() }
sut.name = "New"
waitForExpectations(timeout: 1.0)
XCTAssertEqual(sut.name, "New") // correct
```

### Property-specific tracking

You MUST read the specific property you want to observe in the `apply` closure.

```swift
// WRONG -- tracking title but count changes; onChange NOT called
withObservationTracking { _ = sut.title } onChange: { exp.fulfill() }
sut.count += 1 // onChange NOT fired

// CORRECT -- track the property you expect to change
withObservationTracking { _ = sut.count } onChange: { exp.fulfill() }
sut.count += 1 // onChange fires
```

### Thread safety

`withObservationTracking` onChange fires on the MUTATING thread -- not necessarily MainActor. Mark ViewModel `@MainActor` for thread safety.

### let constants are NOT tracked

Observation only works for `var` stored properties. `let` constants are never tracked.

---

## Reusable waitForChanges Helper

Reduce boilerplate with a reusable helper:

```swift
extension XCTestCase {
    func waitForChanges<T, U>(
        to keyPath: KeyPath<T, U>,
        on parent: T,
        timeout: Double = 1.0
    ) {
        let exp = expectation(description: #function)
        withObservationTracking {
            _ = parent[keyPath: keyPath]
        } onChange: {
            exp.fulfill()
        }
        waitForExpectations(timeout: timeout)
    }
}

// Usage:
waitForChanges(to: \.items, on: sut)
XCTAssertEqual(sut.items.count, 3)
```

---

## Synchronous @Observable -- No Ceremony Needed

For purely synchronous @Observable state changes, test directly -- no `withObservationTracking` needed:

```swift
let sut = CounterVM()
sut.increment()
XCTAssertEqual(sut.count, 1) // direct assertion
```

---

## Testing Intermediate Async States

Testing intermediate states like `isLoading` flip requires `withMainSerialExecutor` (Point-Free pattern):

```swift
@MainActor func test_loadData_setsLoadingState() async {
    await withMainSerialExecutor {
        let task = Task { await sut.loadData() }
        await Task.yield()
        XCTAssertTrue(sut.isLoading)  // deterministically true
        mockClient.completeWithSuccess(items: [.sample()])
        await task.value
        XCTAssertFalse(sut.isLoading)
    }
}
```

---

## What NOT to Test

- **Do NOT unit test SwiftUI View `body`** -- test ViewModel instead. Views are ephemeral struct descriptions.
- **`@Bindable` is a View-layer concern** -- tests don't need it. Mutate properties directly.
- **`@AppStorage` / `@Environment`** only work inside SwiftUI view hierarchy. Wrap behind protocol, inject via init.
- **No `objectWillChange` publisher** -- @Observable has none. Don't try to subscribe to it.

```swift
// WRONG -- @AppStorage in ViewModel won't react outside View
// CORRECT -- protocol wrapper
protocol SettingsStore { var theme: String { get set } }
```

---

## Navigation State Testing

### NavigationPath

`NavigationPath` is type-erased -- you can check `.count` but cannot inspect individual elements. Use typed `[Route]` array for element inspection.

```swift
// WRONG -- can't inspect elements
#expect(sut.navigationPath.count == 1) // only count available

// CORRECT -- use typed array
@Observable final class Router {
    var routes: [Route] = []
}
#expect(sut.routes.last == .detail(id: "123"))
```

### Sheet/FullScreenCover

Test sheet/fullScreenCover as `Optional<Item>` in ViewModel. Don't test `.sheet(isPresented:)` binding behavior -- that's SwiftUI's job.

```swift
@Test func showDetail_setsSheetItem() {
    sut.showDetail(for: item)
    #expect(sut.sheetItem == item)
}

@Test func dismissSheet_clearsItem() {
    sut.sheetItem = item
    sut.dismissSheet()
    #expect(sut.sheetItem == nil)
}
```

---

## Snapshot Testing @Observable Views

Snapshot testing alerts/sheets requires hosting controller in a `UIWindow` -- set `window.makeKeyAndVisible()` and `RunLoop.current.run(until: Date())`.

---

## Swift 6.2+ Observations API

Swift 6.2+ introduces `Observations` (AsyncSequence from @Observable properties with didSet semantics). Values can be missed if produced faster than consumed.

```swift
// Swift 6.2+
for await items in sut.values(for: \.items) {
    // didSet semantics -- new value IS available
}
```

---

## Memory Leak Detection for @Observable

### XCTest pattern:
```swift
override func tearDown() {
    addTeardownBlock { [weak sut = self.sut] in
        XCTAssertNil(sut, "ViewModel leaked -- possible retain cycle")
    }
    sut = nil
    super.tearDown()
}
```

### Swift Testing pattern:
`addTeardownBlock` is unavailable. Use `@Suite` class with `deinit` or `LeakChecker` helper.
