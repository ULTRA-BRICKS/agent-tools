# Test Organization & CI/CD Reference

## How to Use This Reference

Read this when setting up a new test target, organizing existing tests into a maintainable structure, naming test files and methods, configuring Test Plans for CI, planning code coverage measurement, setting up parallel testing, or managing flaky tests.

---

## Recommended Directory Structure

```text
MyApp/
+-- Sources/
|   +-- MyApp/
|       +-- Features/
|       |   +-- Cart/
|       |   |   +-- CartViewModel.swift
|       |   |   +-- CartRepository.swift
|       |   +-- Profile/
|       |       +-- ProfileViewModel.swift
|       +-- Shared/
+-- Tests/
    +-- MyAppTests/
        +-- Features/
        |   +-- Cart/
        |   |   +-- CartViewModelTests.swift
        |   |   +-- CartRepositoryTests.swift
        |   +-- Profile/
        |       +-- ProfileViewModelTests.swift
        +-- Mocks/
        |   +-- MockCartRepository.swift
        |   +-- MockUserService.swift
        |   +-- MockNetworkClient.swift
        +-- Helpers/
        |   +-- Item+Sample.swift
        |   +-- User+Sample.swift
        |   +-- XCTestCase+Extensions.swift
        +-- Integration/
            +-- CartCheckoutIntegrationTests.swift
```

**Rules:**

- Test file mirrors its source file: `CartViewModel.swift` -> `CartViewModelTests.swift`
- Same folder depth. If source is in `Features/Cart/`, test is in `Features/Cart/`
- `Mocks/` is flat -- one mock per file, reused across all tests
- `Helpers/` contains test data factories and shared test utilities
- `Integration/` is a clearly-marked subdirectory

---

## Test Naming Conventions

### XCTest Method Names

Pattern: `test_<methodOrScenario>_<condition>_<expectedResult>`

```swift
func test_loadItems_whenNetworkFails_showsErrorState() async { }
func test_addToCart_whenItemAlreadyInCart_incrementsQuantity() { }
func test_checkout_whenCartIsEmpty_throwsEmptyCartError() async throws { }
```

### Swift Testing Display Names

Use natural language that reads as a sentence:

```swift
@Test("shows error state when network is unavailable")
@Test("increments quantity when adding an item already in cart")
```

### Suite Naming

```swift
@Suite("CartViewModel") struct CartViewModelTests { }
```

---

## Test Target Setup

### Single Test Target vs Multiple

| Approach | When to use |
|----------|-------------|
| **One target** (`MyAppTests`) | Most apps. Simpler, faster build. |
| **Two targets** (`MyAppTests` + `MyAppUITests`) | Always separate UI tests -- they're slow and require a simulator. |
| **Three or more** | Only when modules need separate test targets. |

### Target Build Settings

```text
SWIFT_STRICT_CONCURRENCY = complete
ENABLE_TESTABILITY = YES
```

### Test Target Should NOT Import

- `SwiftUI` (ViewModel tests don't need it)
- Production networking SDKs (mock the protocol instead)
- Crash reporting SDKs (they interfere with crash tests)

---

## Test Plans

### Recommended Layout

Create 3 Xcode test plans: Unit (every push), Integration (merge to develop), UI (nightly/pre-release).

Enable code coverage in unit test plan, disable in UI test plan. Enable Address/Thread Sanitizer in integration test plan.

```json
{
  "configurations": [
    {
      "name": "Standard",
      "options": {
        "codeCoverageEnabled": true,
        "testTimeoutsEnabled": true,
        "defaultTestExecutionTimeAllowance": 60
      }
    },
    {
      "name": "Thread Sanitizer",
      "options": {
        "threadSanitizerEnabled": true,
        "codeCoverageEnabled": false
      }
    },
    {
      "name": "CI",
      "options": {
        "codeCoverageEnabled": true,
        "testRepetitionMode": "retryOnFailure",
        "maximumTestRepetitions": 3,
        "testTimeoutsEnabled": true,
        "defaultTestExecutionTimeAllowance": 120
      }
    }
  ]
}
```

---

## CI Configuration

### xcodebuild Command

```bash
xcodebuild test \
  -project MyApp.xcodeproj \
  -scheme MyApp \
  -testPlan CI \
  -destination "platform=iOS Simulator,name=iPhone 16,OS=latest" \
  -resultBundlePath TestResults.xcresult \
  -maximum-test-execution-time-allowance 120 \
  | xcbeautify
```

### Split CI for Speed

Split CI: `build-for-testing` (creates `.xctestrun`) -> distribute to N runners for `test-without-building`. Eliminates redundant compilation.

```bash
# Build once
xcodebuild build-for-testing -scheme App -derivedDataPath ./build

# Test on N runners
xcodebuild test-without-building -xctestrun ./build/Build/Products/*.xctestrun
```

### SPM for Pure Logic

SPM `swift test`: ~60x faster for pure logic. Extract business logic into Swift Package. App target = thin shell. Reserve `xcodebuild` for UI tests only.

### Code Coverage Collection

```bash
xcodebuild test -enableCodeCoverage YES -derivedDataPath DerivedData ...

# Convert to lcov format
xcrun xccov view --report --json DerivedData/Logs/Test/*.xcresult > coverage.json
```

---

## Coverage Targets

### Per Module Type

| Layer | Minimum Coverage |
|-------|-----------------|
| Business Logic | 90%+ |
| Networking | 85%+ |
| ViewModels | 80% |
| Repositories | 70% |
| Utilities/Formatters | 90% |
| UI/Views | 60% (exclude from unit coverage) |

Exclude generated code and 3rd-party wrappers.

### Files to Exclude

```text
**/AppDelegate.swift
**/SceneDelegate.swift
**/*App.swift
**/ContentView.swift
**/Preview Content/**
**/*Mock*.swift
**/*Sample*.swift
```

---

## Parallel Testing

Xcode parallelizes by TEST CLASS, not by test method. One class with 80 tests bottlenecks an entire runner. Split large classes.

Parallel tests MUST NOT share state. Each test class runs in its own runner process (XCTest) or same process (Swift Testing).

---

## Time Budgets

| Test type | Budget |
|-----------|--------|
| Unit test | < 0.1s |
| Integration test | < 1s |
| UI test | < 30s |

Parse xcresult timing data in CI and flag regressions.

---

## Flaky Test Management

### Quarantine Policy

Track test success rates over N days. Tests below 95% pass rate -> auto-quarantined (don't block PRs). When stabilized (>99%) -> auto-unquarantined.

**Anti-pattern:** Retries without quarantine tracking. Teams "just re-run" which masks real bugs.

### Tracking

```markdown
# Known Flaky Tests -- refactoring/flaky-tests.md

| Test | File | Frequency | Root cause | Owner | Status |
|------|------|-----------|------------|-------|--------|
| test_loadItems_... | ItemListVMTests | ~10% CI | Race: no withMainSerialExecutor | @dev | In progress |
```

**Policy:**

1. A test that fails 2+ times in CI in one week is declared flaky
2. Add it to `refactoring/flaky-tests.md` with root cause analysis
3. Add `@Suite(.serialized)` as a temporary stabilizer
4. Fix within 2 sprints

---

## Test Discovery Gaps

```bash
# Find ViewModels without test files
find Sources -name "*ViewModel.swift" -exec basename {} .swift \; | sort > /tmp/vm_sources.txt
find Tests -name "*ViewModelTests.swift" -exec basename {} Tests.swift \; | sort > /tmp/vm_tests.txt
comm -23 /tmp/vm_sources.txt /tmp/vm_tests.txt
```

---

## Factory Pattern

Every test class has private `makeSUT()` that creates SUT + all dependencies, calls `trackForMemoryLeaks`, returns tuple. Tests never call initializers directly.

Never make `private` properties `internal`/`public` just to test them. Test behavior through public interface, not implementation.

Use `XCTSkipIf`/`XCTSkipUnless` for conditional/environment tests. Keeps suite green without hiding tests behind `#if` flags.
