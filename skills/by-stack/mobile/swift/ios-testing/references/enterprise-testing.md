# Enterprise Testing Patterns

## How to Use This Reference

Read this when testing authentication/OAuth token refresh flows, feature flag behavior, analytics event tracking, deep link routing, push notification handling, memory leak detection patterns, test data builders, or accessibility auditing.

---

## Auth / OAuth Testing

### 401 -> refresh -> retry chain

Test with single-retry cap. Assert refresh called exactly once.

```swift
// Production pattern:
func loadAuthorized<T: Decodable>(_ url: URL, allowRetry: Bool = true) async throws -> T {
    let (data, response) = try await session.data(from: url)
    if (response as? HTTPURLResponse)?.statusCode == 401 && allowRetry {
        _ = try await authManager.refreshToken()
        return try await loadAuthorized(url, allowRetry: false)
    }
    return try JSONDecoder().decode(T.self, from: data)
}

// Test:
@Test func expired_token_refreshes_and_retries() async throws {
    mockNetwork.responses = [
        .failure(HTTPError(statusCode: 401)),
        .success(validData)
    ]
    let result: User = try await sut.loadAuthorized(url)
    #expect(mockAuth.refreshTokenCallCount == 1)
    #expect(result.name == "Alice")
}
```

### Concurrent 401 serialization

Serialize concurrent 401 token refresh behind actor. Test with 3+ concurrent requests -- assert `refreshToken()` called exactly once.

### Three token states

Every auth test suite needs: valid, expired-but-refreshable, expired-and-unrefreshable.

---

## Feature Flags

### Minimum 3 tests per flag

Every feature flag requires minimum 3 tests: flag=ON, flag=OFF, flag=MISSING/nil (defaults).

```swift
protocol FeatureFlagProvider {
    func isEnabled(_ flag: FeatureFlag) -> Bool?
}

@Test(arguments: [true, false, nil])
func checkout_respectsNewPaymentFlag(flagValue: Bool?) {
    mockFlags.values[.newPayment] = flagValue
    sut.processCheckout()
    let expected = flagValue ?? false // default when missing
    #expect(sut.usedNewPayment == expected)
}
```

### Interaction pairs

Test known interaction pairs (e.g., `newCheckout` + `newPayments`) -- matrix tests for all combinations.

### Swift enums

Use Swift enums for feature flags to get compiler exhaustivity checking.

---

## Analytics Testing

Create `AnalyticsTrackerSpy`. Assert event name, exact params dictionary, AND call count. Never assert just "was called".

```swift
class AnalyticsTrackerSpy: AnalyticsTracking {
    struct TrackedEvent {
        let event: String
        let properties: [String: Any]
    }
    var trackedEvents: [TrackedEvent] = []

    func track(_ event: String, properties: [String: Any]) {
        trackedEvents.append(TrackedEvent(event: event, properties: properties))
    }
}

// Test:
XCTAssertEqual(spy.trackedEvents.count, 1)
XCTAssertEqual(spy.trackedEvents[0].event, "purchase_completed")
XCTAssertEqual(spy.trackedEvents[0].properties["amount"] as? Double, 29.99)
```

---

## Deep Linking

### Route parsing

Parse URLs into `Route` enum (pure function), test routing logic independently of navigation.

```swift
enum Route: Equatable {
    case profile(userId: String)
    case product(id: String)
    case settings
    case unknown

    init(url: URL) {
        // parsing logic
    }
}

func test_route_profileURL_parsesUserId() {
    let route = Route(url: URL(string: "myapp://profile/123")!)
    XCTAssertEqual(route, .profile(userId: "123"))
}
```

### App state matrix

Test deep link routing across all app states: cold start, warm start, active with modal, during auth, during onboarding.

---

## Push Notifications

Decode push payloads into typed models. Unit test JSON -> Model parsing independently.

Test:
- Valid payloads with all fields
- Malformed payloads
- Missing fields
- Unknown action types

```swift
@Test func decodePushPayload_validJSON_parsesCorrectly() throws {
    let json = """
    {"aps": {"alert": "New message"}, "action": "open_chat", "chatId": "42"}
    """.data(using: .utf8)!

    let payload = try JSONDecoder().decode(PushPayload.self, from: json)
    #expect(payload.action == .openChat)
    #expect(payload.chatId == "42")
}
```

---

## Memory Leak Detection

### trackForMemoryLeaks extension

Call in EVERY `makeSUT()` factory method.

```swift
extension XCTestCase {
    func trackForMemoryLeaks(
        _ instance: AnyObject,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        addTeardownBlock { [weak instance] in
            XCTAssertNil(
                instance,
                "Potential memory leak: \(String(describing: type(of: instance)))",
                file: file,
                line: line
            )
        }
    }
}

// In makeSUT:
private func makeSUT(file: StaticString = #filePath, line: UInt = #line) -> (sut: ViewModel, spy: MockRepo) {
    let spy = MockRepo()
    let sut = ViewModel(repo: spy)
    trackForMemoryLeaks(sut, file: file, line: line)
    trackForMemoryLeaks(spy, file: file, line: line)
    return (sut, spy)
}
```

### Swift Testing

`addTeardownBlock` unavailable. Use `@Suite` class with `deinit` or `LeakChecker` helper.

---

## Test Data Builders

### Static factory

Create `.create()` static factory with default values. Tests only specify the ONE relevant property.

```swift
extension User {
    static func create(
        id: String = "default-id",
        name: String = "Default",
        plan: Plan = .free
    ) -> User {
        User(id: id, name: name, plan: plan)
    }
}

func test_upgrade_freeUser_showsPaywall() {
    let user: User = .create(plan: .free) // only specifies relevant delta
    sut.upgrade(user: user)
    XCTAssertTrue(sut.showsPaywall)
}
```

### Builder pattern

For complex object graphs, use Builder pattern with chainable `with()` methods. Builders live in test target only.

---

## Accessibility Testing

### performAccessibilityAudit (Xcode 15+)

Add `try app.performAccessibilityAudit()` to at least one UI test per major screen.

Catches: missing labels, insufficient contrast, small hit areas, Dynamic Type failures.

### Audit categories

Use `XCUIAccessibilityAuditType` categories:
- `.dynamicType`
- `.contrast`
- `.sufficientElementDescription`
- `.hitRegion`

```swift
func test_homeScreen_passesAccessibilityAudit() throws {
    let app = XCUIApplication()
    app.launch()
    try app.performAccessibilityAudit(for: [.dynamicType, .contrast, .sufficientElementDescription])
}
```

---

## Obj-C Bridging

When Swift calls Obj-C APIs not annotated with `NS_ASSUME_NONNULL_BEGIN`, values bridge as implicitly unwrapped optionals. Test nil passing explicitly.

---

## makeSUT Factory Pattern

Every test class has private `makeSUT()` that creates SUT + all dependencies, calls `trackForMemoryLeaks`, returns tuple. Tests never call initializers directly.

```swift
private func makeSUT(
    items: [Item] = [],
    error: Error? = nil,
    file: StaticString = #filePath,
    line: UInt = #line
) -> (sut: ItemListViewModel, repo: MockItemRepository) {
    let repo = MockItemRepository()
    repo.stubbedItems = items
    repo.stubbedError = error
    let sut = ItemListViewModel(repository: repo)
    trackForMemoryLeaks(sut, file: file, line: line)
    trackForMemoryLeaks(repo, file: file, line: line)
    return (sut, repo)
}
```

---

## Testing Boundaries

- Never make `private` properties `internal`/`public` just to test them. Test behavior through public interface.
- Use `XCTSkipIf`/`XCTSkipUnless` for conditional/environment tests. Keeps suite green without hiding tests behind `#if` flags.
