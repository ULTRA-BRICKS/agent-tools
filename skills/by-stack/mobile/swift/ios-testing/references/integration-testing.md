# Integration Testing

## How to Use This Reference

Read this when testing real URLProtocol-based network mocking, Core Data / SwiftData persistence, Keychain operations, UserDefaults isolation, FileManager temp directories, or system services (BGTaskScheduler, push notifications).

---

## URLProtocol Mocking

### Ephemeral configuration

Always use `.ephemeral` configuration -- never `.default` -- prevents cached responses leaking between tests.

```swift
class MockURLProtocol: URLProtocol {
    static var requestHandler: ((URLRequest) throws -> (HTTPURLResponse, Data))?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let handler = Self.requestHandler else {
            XCTFail("No request handler set")
            client?.urlProtocolDidFinishLoading(self)
            return
        }
        do {
            let (response, data) = try handler(request)
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}
```

### Critical callbacks

Always call all three client callbacks: `didReceive`, `didLoad`, `urlProtocolDidFinishLoading` -- or request hangs.

### Cleanup

Reset static `requestHandler` in `tearDown`.

### Swift 6 Sendable

Mark `MockURLProtocol` as `@unchecked Sendable`, wrap handler in actor. Parallel tests + static handler = data races.

---

## Core Data Testing

### /dev/null store (recommended)

Use `/dev/null` URL -- NOT `NSInMemoryStoreType`. Apple recommended (WWDC 2018). `NSInMemoryStoreType` breaks cascading deletes.

```swift
let description = NSPersistentStoreDescription()
description.url = URL(fileURLWithPath: "/dev/null")
description.shouldAddStoreAsynchronously = false
```

### Share managed object model

Share `NSManagedObjectModel` across all tests -- loading `.momd` is expensive. Use static lazy property.

```swift
private static let managedObjectModel: NSManagedObjectModel = {
    let bundle = Bundle(for: SomeClass.self)
    let url = bundle.url(forResource: "Model", withExtension: "momd")!
    return NSManagedObjectModel(contentsOf: url)!
}()
```

### Cross-target namespace

In test targets, explicitly pass `NSManagedObjectModel` to `NSPersistentContainer` -- auto-detection by name fails across target namespaces.

### Cleanup

Set `container = nil` in `tearDown`.

---

## SwiftData Testing

### In-memory store

```swift
let config = ModelConfiguration(isStoredInMemoryOnly: true)
let container = try ModelContainer(for: Item.self, configurations: config)
```

### MainActor requirement

Mark test class `@MainActor` -- `container.mainContext` is MainActor-isolated. Compiler error in Swift 6 otherwise.

### Isolation

Create container per test method -- not as shared lazy property -- for full isolation.

### @Query is untestable

`@Query` only works inside SwiftUI views. Pull data-access into ViewModels with `ModelContext` injection.

```swift
// WRONG -- @Query in ViewModel
// CORRECT -- inject ModelContext
class ItemViewModel {
    private let context: ModelContext
    init(context: ModelContext) { self.context = context }

    func fetchItems() throws -> [Item] {
        try context.fetch(FetchDescriptor<Item>())
    }
}
```

---

## Keychain Testing

### Host application required

Keychain tests require a host application. Framework test targets without host fail with `OSStatus -25300` or `-34018`.

### Protocol wrapper

Use protocol wrapper + mock dictionary -- avoids needing real Keychain.

```swift
protocol KeychainManageable {
    func get(valueByKey key: String) -> String?
    func set(asValue value: String, byKey key: String)
}

class MockKeychainManager: KeychainManageable {
    private var storage: [String: String] = [:]
    func get(valueByKey key: String) -> String? { storage[key] }
    func set(asValue value: String, byKey key: String) { storage[key] = value }
}
```

### Real Keychain cleanup

If testing real Keychain, clean up with `SecItemDelete` in `tearDown`.

**Simulator persistence warning:** Keychain data persists across simulator app installs — unlike UserDefaults, uninstalling the app does **not** clear the Keychain. This causes false failures in integration tests that rely on a clean Keychain state. Always call `SecItemDelete` in **both `setUp` AND `tearDown`** when testing real Keychain operations on simulator.

---

## UserDefaults Testing

Use `UserDefaults(suiteName:)` with unique name -- never `.standard`.

```swift
override func setUp() {
    super.setUp()
    let suiteName = "test.suite.\(name)"
    UserDefaults.standard.removePersistentDomain(forName: suiteName)
    testDefaults = UserDefaults(suiteName: suiteName)!
}

override func tearDown() {
    UserDefaults.standard.removePersistentDomain(forName: suiteName)
    super.tearDown()
}
```

Call `removePersistentDomain(forName:)` in BOTH `setUp` and `tearDown`.

---

## FileManager Testing

Use unique temp directory per test (UUID-based), create in `setUp`, remove recursively in `tearDown`.

```swift
private var tempDir: URL!

override func setUp() {
    super.setUp()
    tempDir = FileManager.default.temporaryDirectory
        .appendingPathComponent(UUID().uuidString)
    try! FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
}

override func tearDown() {
    try? FileManager.default.removeItem(at: tempDir)
    super.tearDown()
}
```

Prefer `addTeardownBlock` for file cleanup -- co-locates creation and cleanup.

`NSTemporaryDirectory()` is NOT automatically cleaned between test runs -- always pre-clean.

---

## System Services

### BGTaskScheduler

BGTaskScheduler CANNOT be unit tested directly. Extract handler logic into testable functions. Use LLDB `_simulateLaunchForTaskWithIdentifier:` for debug testing.

### UNUserNotificationCenter

Crashes in test targets without host app. Use protocol wrapper.

### General pattern

For ANY system singleton that can't be initialized in tests (UNUserNotificationCenter, BGTaskScheduler, SKPaymentQueue), use protocol-wrapper-and-inject pattern.

```swift
protocol NotificationScheduling {
    func requestAuthorization(options: UNAuthorizationOptions) async throws -> Bool
    func add(_ request: UNNotificationRequest) async throws
}

extension UNUserNotificationCenter: NotificationScheduling {}

class MockNotificationScheduler: NotificationScheduling {
    var authorizationResult = true
    var addCallCount = 0

    func requestAuthorization(options: UNAuthorizationOptions) async throws -> Bool {
        authorizationResult
    }

    func add(_ request: UNNotificationRequest) async throws {
        addCallCount += 1
    }
}
```
