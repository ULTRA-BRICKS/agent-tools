# Localization Testing — Complete Reference

## Xcode Pseudolanguages

Available in Edit Scheme → Options → Application Language:

| Pseudolanguage | Purpose | What to Check |
|---|---|---|
| **Double Length** | Simulates text expansion (~30% for German) | Truncation, overflow, layout breaks |
| **Right to Left** | Full RTL layout without Arabic translations | Constraint direction, alignment, image flipping |
| **Accented** | Adds diacritical marks to English text | Reveals hardcoded strings (no accents = not localized) |
| **Bounded** | Wraps strings in `[# #]` brackets | Shows exact bounds of localized text, reveals truncation |

## Launch Arguments

```bash
# Uppercase non-localized strings (makes them visually obvious)
-NSShowNonLocalizedStrings YES

# Force specific locale
-AppleLocale ru_RU
-AppleLanguages "(ru)"

# Force specific calendar
-AppleCalendar buddhist

# Force 12-hour time
-AppleICUForce12HourTime YES
```

## Automated Testing

### Locale-Specific Tests
```swift
func testRussianPluralization() {
    let app = XCUIApplication()
    app.launchArguments += ["-AppleLocale", "ru_RU"]
    app.launchArguments += ["-AppleLanguages", "(ru)"]
    app.launch()

    // Test with numbers that exercise all plural categories
    // 1 (one), 2 (few), 5 (many), 21 (one), 22 (few)
}
```

### Element Lookup
```swift
// ✅ Use accessibilityIdentifier — stable across locales
let button = app.buttons["closeButton"]

// ❌ accessibilityLabel changes per locale
let button = app.buttons["Close"]  // Fails in Russian, Arabic, etc.
```

### Snapshot Testing with Locale
```swift
func testSettingsScreen_Russian() {
    let app = XCUIApplication()
    app.launchArguments += ["-AppleLocale", "ru_RU"]
    app.launch()

    let screenshot = app.screenshot()
    // Compare with reference snapshot for Russian locale
}
```

## .xcstrings Validation Script

Use `scripts/xcstrings_tool.py validate` to check:
- Missing translations (state = "new")
- Stale entries (no longer in source code)
- Missing plural categories for each language
- Mismatched format specifiers between source and translations
- Empty translation values

## Manual Testing Checklist

1. **Run with Double Length** — check every screen for truncation
2. **Run with RTL** — check every screen for layout issues
3. **Run with Accented** — scan for unaccented text (= hardcoded)
4. **Run with Bounded** — check for strings that clip `[# #]` markers
5. **Test with `-NSShowNonLocalizedStrings YES`** — find uppercased text
6. **Test date/time** with Buddhist calendar and 12-hour time
7. **Test numbers** with Arabic locale (Eastern Arabic numerals)
8. **Test currency** with different locale (symbol position, separators)
9. **Test plural forms** with verification set `[0, 1, 2, 3, 4, 5, 10, 11, 12, 14, 20, 21, 22, 25, 100, 101, 111, 1.5]`
10. **VoiceOver in non-English** — verify accessibility labels are translated

## CI Integration

```yaml
# Run UI tests with pseudolanguages
- name: Test RTL Layout
  run: |
    xcodebuild test \
      -scheme MyApp \
      -destination 'platform=iOS Simulator,name=iPhone 16' \
      -testLanguage "RTL" \
      -resultBundlePath RTLTestResults.xcresult

- name: Test Double Length
  run: |
    xcodebuild test \
      -scheme MyApp \
      -destination 'platform=iOS Simulator,name=iPhone 16' \
      -testLanguage "Double Length" \
      -resultBundlePath DoubleLengthResults.xcresult
```

## Third-Party Tools

| Tool | Purpose |
|---|---|
| Lokalise / Phrase / Crowdin | OTA translation management, CI integration |
| BartyCrouch | CLI that updates .strings files from source code |
| SwiftGen | Code-generated string constants from .strings files |
| Xcode String Catalog Editor | Built-in visual editor with progress tracking |
