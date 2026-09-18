# UI Testing (XCUITest)

## How to Use This Reference

Read this when writing XCUITest UI tests, implementing the Page Object Model pattern, handling system alerts, managing launch arguments, testing deep links, or reducing UI test flakiness.

---

## Page Object Model

Use protocol-based Screen objects. Return `Self` for same-screen actions, return new Screen type for navigation.

```swift
protocol Screen {
    var app: XCUIApplication { get }
}

struct LoginScreen: Screen {
    let app: XCUIApplication

    @discardableResult
    func typeEmail(_ email: String) -> Self {
        let field = app.textFields[AccessibilityID.Login.emailField.rawValue]
        field.tap()
        field.typeText(email)
        return self
    }

    func tapLogin() -> HomeScreen {
        app.buttons["login"].tap()
        return HomeScreen(app: app)
    }
}

// Test reads declaratively:
LoginScreen(app: app)
    .typeEmail("user@test.com")
    .typePassword("pass")
    .tapLogin()
    .verifyWelcome()
```

Page Objects should NOT inherit from XCTestCase. Use structs conforming to `Screen` protocol.

---

## Accessibility Identifiers

Use enum-based identifiers shared between app and test targets. The file must be in BOTH targets.

```swift
enum AccessibilityID {
    enum Login: String {
        case emailField = "login.email"
        case passwordField = "login.password"
        case loginButton = "login.submit"
    }
    enum Home: String {
        case welcomeLabel = "home.welcome"
    }
}

// App: .accessibilityIdentifier(AccessibilityID.Login.emailField.rawValue)
// Test: app.textFields[AccessibilityID.Login.emailField.rawValue]
```

---

## waitForExistence -- #1 AI Mistake

ALWAYS check the return value of `waitForExistence`. Ignoring it means test silently proceeds when element never appears.

```swift
// WRONG -- return value discarded
button.waitForExistence(timeout: 5)
button.tap() // crashes if not found

// CORRECT
XCTAssertTrue(button.waitForExistence(timeout: 5), "Submit button did not appear")
button.tap()
```

### waitAndTap extension

```swift
extension XCUIElement {
    @discardableResult
    func waitAndTap(timeout: TimeInterval = 5, file: StaticString = #filePath, line: UInt = #line) -> Self {
        XCTAssertTrue(waitForExistence(timeout: timeout), "Element \(identifier) not found", file: file, line: line)
        tap()
        return self
    }
}
```

### exists vs isHittable

`.exists` returns true for off-screen elements. Use `.isHittable` to confirm visibility.

### waitForNonExistence

XCUITest has none built-in. Build your own:

```swift
extension XCUIElement {
    func waitForNonExistence(timeout: TimeInterval = 5) -> Bool {
        let predicate = NSPredicate(format: "exists == false")
        let expectation = XCTNSPredicateExpectation(predicate: predicate, object: self)
        return XCTWaiter().wait(for: [expectation], timeout: timeout) == .completed
    }
}
```

### Never use sleep()

Use `waitForExistence` or `XCTWaiter` instead.

---

## Flaky Test Prevention

### continueAfterFailure

Set as FIRST line in setUp for UI tests:

```swift
override func setUp() {
    super.setUp()
    continueAfterFailure = false
    app = XCUIApplication()
    app.launch()
}
```

### Disable animations

```swift
// Test:
app.launchArguments.append("-disableAnimations")

// App:
if CommandLine.arguments.contains("-disableAnimations") {
    UIView.setAnimationsEnabled(false)
}
```

**Tip:** Prefer `window.layer.speed = 100` over fully disabling -- avoids missed callback bugs.

### Centralized timeouts

```swift
enum Timeout {
    static let short: TimeInterval = 2
    static let medium: TimeInterval = 5
    static let long: TimeInterval = 10
    static let network: TimeInterval = 15
}
```

---

## System Alerts

`addUIInterruptionMonitor` + MUST call `app.tap()` to trigger the monitor.

```swift
addUIInterruptionMonitor(withDescription: "Location") { alert in
    alert.buttons["Allow While Using App"].tap()
    return true
}
app.tap() // REQUIRED to trigger monitor
```

### "Don't Allow" uses smart quote

"Don't Allow" uses RIGHT SINGLE QUOTE (U+2019): `"Don\u{2019}t Allow"`, not straight apostrophe.

### Monitor order

Monitors fire in REVERSE order of registration. Use springboard approach as fallback on iOS 14+.

---

## Launch Arguments

Launch arguments MUST be set BEFORE `app.launch()`.

```swift
override func setUp() {
    super.setUp()
    app = XCUIApplication()
    app.launchArguments += ["-UITesting", "-disableAnimations"]
    app.launchEnvironment["MOCK_DATA"] = "fixtures/items.json"
    app.launch() // AFTER setting arguments
}
```

Use enum-based LaunchArgument registry -- single source of truth. Use `launchEnvironment` for structured mock data injection (JSON, fixture names).

---

## Process Separation

XCUITest runs in a SEPARATE PROCESS. Cannot mock objects directly. Communication limited to:
- Launch arguments
- Launch environment
- Accessibility hierarchy

---

## Deep Link Testing

iOS 16.4+: Use `XCUIDevice.shared.system.open(url)`. Pre-16.4: Safari-based approach.

Handle "Open" confirmation dialog via springboard.

---

## Screenshots

Auto-capture in `tearDown` with `.lifetime = .deleteOnSuccess`:

```swift
override func tearDown() {
    let screenshot = XCUIScreen.main.screenshot()
    let attachment = XCTAttachment(screenshot: screenshot)
    attachment.lifetime = .deleteOnSuccess
    add(attachment)
    super.tearDown()
}
```

Override `record(_ issue:)` for automatic failure screenshots.

---

## Element Query Performance

- Use `.firstMatch` for performance -- avoids full hierarchy traversal
- Use accessibility identifier per cell, not index: `cell.accessibilityIdentifier = "item.cell.\(item.id)"`
- Use `app.wait(for: .runningForeground)` after terminate/relaunch -- not `sleep()`

---

## Quick Decision: Which UI Test Pattern?

```
Need to test a multi-screen user flow?
+-- YES -> Page Object Model with Screen protocol
+-- NO  -> Need to test a single screen interaction?
    +-- YES -> Direct XCUIElement queries in test
    +-- NO  -> Need accessibility audit?
        +-- YES -> performAccessibilityAudit() (Xcode 15+)
        +-- NO  -> Probably a unit test, not a UI test
```
