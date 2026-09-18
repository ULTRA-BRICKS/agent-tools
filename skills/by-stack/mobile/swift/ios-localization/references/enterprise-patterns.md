# Enterprise Localization Patterns

## Modular Apps (Swift Packages)

Every SPM package with localized resources needs:

```swift
// Package.swift
let package = Package(
    name: "FeatureSettings",
    defaultLocalization: "en",  // REQUIRED
    ...
    targets: [
        .target(
            name: "FeatureSettings",
            resources: [.process("Resources")]  // Contains .lproj directories
        )
    ]
)
```

Directory structure:
```
Sources/FeatureSettings/
├── Resources/
│   ├── en.lproj/
│   │   └── Localizable.strings
│   ├── ru.lproj/
│   │   └── Localizable.strings
│   └── Localizable.xcstrings  // Or use String Catalog
├── Views/
│   └── SettingsView.swift
└── ...
```

**Bundle resolution:**
```swift
// Bundle.module is auto-generated but internal
// Expose for consumers if needed:
public extension Bundle {
    static let featureSettings = Bundle.module
}

// Always use in views:
Text("settings_title", bundle: .module)
String(localized: "settings_title", bundle: .module)
```

**Most common enterprise bug:** Omitting `bundle:` silently defaults to `.main` and returns the untranslated key. This passes English testing because the key often IS the English text.

## White-Label Apps

Three architectures for per-brand localization:

### 1. Multiple Xcode Targets
Each target has its own `Localizable.strings`:
```
MyApp/
├── BrandA/Localizable.xcstrings
├── BrandB/Localizable.xcstrings
└── Shared/SharedLocalizable.xcstrings
```

### 2. Runtime Bundle Loading
```swift
class BrandManager {
    static let shared = BrandManager()
    private(set) var bundle: Bundle

    func configure(brand: Brand) {
        let path = Bundle.main.path(forResource: brand.bundleName, ofType: "bundle")!
        bundle = Bundle(path: path)!
    }
}

// Usage
String(localized: "welcome", bundle: BrandManager.shared.bundle)
```

### 3. OTA Localization (Lokalise, Phrase, Crowdin)
Translations downloaded at runtime and stored in a custom bundle. Fallback to compiled strings if network unavailable.

## Accessibility String Localization

Apple documentation: `accessibilityLabel` is "a succinct label in a **localized string**."

```swift
// ❌ AI routinely generates hardcoded English
button.accessibilityLabel = "Close button"
slider.accessibilityValue = "50 percent"

// ✅ Localized
button.accessibilityLabel = String(localized: "close_button.a11y.label",
    comment: "Accessibility: Close button")
slider.accessibilityValue = String(localized: "slider.a11y.value \(percentage)",
    comment: "Accessibility: Slider value, e.g. '50 percent'")

// ✅ SwiftUI — auto-localized from literal
Button(action: close) { Image(systemName: "xmark") }
    .accessibilityLabel("close_button.a11y.label")

// ✅ accessibilityIdentifier should NOT be localized
button.accessibilityIdentifier = "closeButton"  // Stable across locales
```

## Testing Localization

### Xcode Pseudolanguages (Edit Scheme → Options → App Language)

| Pseudolanguage | What It Tests |
|---|---|
| **Double Length** | Text expansion — German averages 30% longer than English |
| **Right to Left** | RTL layout without needing Arabic translations |
| **Accented** | Reveals hardcoded strings (accented chars don't appear in untranslated text) |
| **Bounded** | Shows truncation with `[# text #]` brackets |

### Launch Arguments
```
-NSShowNonLocalizedStrings YES
```
Uppercases any string that missed localization — makes them visually obvious.

### Automated Testing
```swift
// Use accessibilityIdentifier for element lookup (not accessibilityLabel)
let button = app.buttons["closeButton"]  // Stable across locales

// Test with specific locale
app.launchArguments += ["-AppleLocale", "ru_RU"]
app.launchArguments += ["-AppleLanguages", "(ru)"]
app.launch()
```

## Text Expansion Guidelines

| Source Language | Target Expansion |
|---|---|
| English → German | +30% average |
| English → French | +15-20% |
| English → Japanese | Can be shorter (but vertical space for ruby annotations) |
| English → Arabic | Similar length but RTL layout |
| "OK" → German "Einverstanden" | 3x longer for short strings |

**Rule:** Never use fixed widths on containers with localized text. Use flexible Auto Layout constraints and test with Double Length pseudolanguage.
