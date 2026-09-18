# Accessibility Testing — Complete Reference

Testing tools, automated audits, manual VoiceOver protocol, and CI pipeline setup.

## Table of Contents

1. [Xcode Accessibility Inspector](#xcode-accessibility-inspector)
2. [XCTest Accessibility Audit](#xctest-accessibility-audit)
3. [VoiceOver Manual Testing Protocol](#voiceover-manual-testing-protocol)
4. [Common Audit Findings](#common-audit-findings)
5. [CI Pipeline Setup](#ci-pipeline-setup)
6. [Third-Party Tools](#third-party-tools)

## Xcode Accessibility Inspector

**Three tabs:**

1. **Inspector:** Select any element to see Label, Value, Hint, Traits, Frame. Auto-Navigate simulates VoiceOver linear navigation.
2. **Audit:** Run Audit to detect missing labels, contrast issues, clipped text, small targets, fixed fonts, filename-as-label.
3. **Settings:** Simulate Dynamic Type, Bold Text, Reduce Transparency, Invert Colors, Reduce Motion — without changing device settings.

**How to use:**
1. Open Xcode > Open Developer Tool > Accessibility Inspector
2. Select target (Simulator or device)
3. Click the crosshair to inspect individual elements
4. Click Audit to run automated checks
5. Click Settings to simulate accessibility preferences

## XCTest Accessibility Audit

Available since Xcode 15. Runs automated accessibility checks in UI tests.

### Full audit
```swift
func testAccessibility() throws {
    let app = XCUIApplication()
    app.launch()
    try app.performAccessibilityAudit()
}
```

### Selective audit — specific categories
```swift
try app.performAccessibilityAudit(for: [
    .contrast,
    .dynamicType,
    .sufficientElementDescription
])
```

### Filter known issues
```swift
try app.performAccessibilityAudit(for: .all) { issue in
    // Return true to IGNORE this issue
    guard let element = issue.element,
          element.label == "Known Issue" else { return false }
    return issue.auditType == .dynamicType
}
```

### Regression pattern — audit in tearDown
```swift
override func tearDownWithError() throws {
    try app.performAccessibilityAudit()
}
```

**Audit types:**
| Type | Checks |
|---|---|
| `.contrast` | Text/background contrast ratio |
| `.dynamicType` | Font responds to Dynamic Type changes |
| `.sufficientElementDescription` | Elements have meaningful labels |
| `.hitRegion` | Touch targets meet minimum size |
| `.textClipped` | Text not truncated/clipped |
| `.trait` | Traits match element behavior |
| `.all` | All of the above |

## VoiceOver Manual Testing Protocol

Automated tools catch ~30% of accessibility issues. Manual VoiceOver testing is essential.

### 10-Step Protocol

1. **Toggle VoiceOver** — Triple-click side button (or Settings > Accessibility > VoiceOver)
2. **Linear navigation** — Swipe right through entire screen. Verify logical order.
3. **Element identification** — Every interactive element: clear label, correct role, state announced.
4. **Heading navigation** — Use Rotor > Headings. Verify all sections have headings.
5. **Custom actions** — Select elements with actions, swipe up/down to cycle through them.
6. **Modal dialogs** — Present modals. Verify focus is trapped and escape gesture dismisses.
7. **Dynamic content** — Trigger loading states, errors, empty states. Verify announcements fire.
8. **Screen Curtain test** — Triple three-finger tap to turn off screen. Complete tasks using only audio. This is the gold standard.
9. **Redundancy check** — No element should announce "Submit button. Button." (redundant type in label).
10. **Decorative check** — Decorative images should be completely silent.

### What to Verify Per Element

| Element Type | Verify |
|---|---|
| Button | Label describes action, "Button" trait announced |
| Link | Label describes destination, "Link" trait announced |
| Toggle | Label + current value ("On"/"Off") + "Toggle" |
| Text field | Label + current text + "Text field" |
| Image (informative) | Meaningful label describing content |
| Image (decorative) | Silent — not announced |
| Section heading | Label + "Heading" trait |
| List cell | Label + hint if applicable + custom actions |
| Tab | Label + "Tab, X of Y" |
| Alert | Title + message announced, focus moves to alert |

## Common Audit Findings

Issues developers most frequently miss (ranked by frequency):

1. Image filenames used as labels ("icon_heart_outline_24.png")
2. Missing labels on icon-only buttons
3. Insufficient contrast (light gray on white, especially placeholders)
4. Fixed font sizes (`.system(size:)`)
5. Touch targets under 44x44pt
6. Redundant traits in labels ("Search button" → "Search button. Button.")
7. Inaccessible custom controls (no traits, no value)
8. Missing loading/error state announcements
9. Focus not trapped on modals / not released on dismiss
10. Decorative images exposed to VoiceOver
11. Unlabeled text fields
12. Broken reading order from absolute positioning or z-index

## CI Pipeline Setup

### GitHub Actions
```yaml
name: Accessibility Audit
on: [pull_request]
jobs:
  a11y-audit:
    runs-on: macos-14
    steps:
      - uses: actions/checkout@v4
      - name: Select Xcode
        run: sudo xcode-select -s /Applications/Xcode_16.app
      - name: Test
        run: |
          xcodebuild test \
            -project MyApp.xcodeproj \
            -scheme MyAppUITests \
            -sdk iphonesimulator \
            -destination 'platform=iOS Simulator,name=iPhone 16,OS=18.0' \
            -resultBundlePath TestResults.xcresult | xcpretty
```

### Integration pattern
Add `performAccessibilityAudit()` to existing UI test tearDown so every UI test also validates accessibility. This catches regressions at zero additional test-writing cost.

## Third-Party Tools

| Tool | Type | Description |
|---|---|---|
| CVS Health a11y-check | Static analyzer | 25+ WCAG rules, CLI, scans source without building |
| A11yUITests (Rob Whitaker) | XCUITest extension | 12 checks as XCUITest cases |
| GTXiLib (Google) | Unit test framework | Auto-detects missing labels, incorrect traits |
| AccessibilitySnapshot (Cash App) | Snapshot testing | Visual regression with accessibility annotations |
| Deque axe DevTools Mobile | Commercial | XCUITest/Appium integration, WCAG rules |
| BrowserStack App Accessibility | Cloud service | Real device testing, CI/CD integration |
