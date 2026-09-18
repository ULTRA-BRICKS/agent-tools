# SwiftUI Localization Patterns

## Automatic Localization

SwiftUI automatically treats string literals as `LocalizedStringKey` in these views:
- `Text("key")` — looks up "key" in String Catalog
- `Button("key")` — label is localized
- `Toggle("key")` — label is localized
- `Label("key", systemImage:)` — title is localized
- `.navigationTitle("key")` — title is localized

## String vs LocalizedStringKey Trap

```swift
// ✅ Literal — LocalizedStringKey, localization lookup occurs
Text("greeting_key")

// ❌ Variable — String overload, NO localization
let key = "greeting_key"
Text(key)  // Displays literal "greeting_key"

// ✅ Fix for dynamic keys
Text(LocalizedStringKey(key))

// ✅ Strings that should NOT be localized
Text(verbatim: "v2.0.1")
Text(verbatim: "https://example.com")
```

## Interpolation Type Trap

```swift
// ❌ Swift infers String — Image becomes description text
let label = "Delete \(Image(systemName: "trash"))"

// ✅ Explicit type — Image renders correctly
let label: LocalizedStringKey = "Delete \(Image(systemName: "trash"))"
```

`LocalizedStringKey` has custom `StringInterpolation` handling `Image`, `Date`, `Text`, and `FormatStyle`. Without the type annotation, Swift uses `DefaultStringInterpolation` which calls `.description`.

## Custom View Parameters

```swift
// ❌ String parameter — callers' literals skip localization
struct CardView: View {
    let title: String
    var body: some View { Text(verbatim: title) }
}

// ✅ LocalizedStringKey — auto-localization works
struct CardView: View {
    let title: LocalizedStringKey
    var body: some View { Text(title) }
}

// ✅ Generic approach (like Apple's views)
struct CardView<Title: StringProtocol>: View {
    let title: Title
    init(_ title: Title) { self.title = title }
    var body: some View { Text(title) }
}
```

## Swift Package Localization

```swift
// Package.swift
let package = Package(
    name: "MyPackage",
    defaultLocalization: "en",  // REQUIRED for localized packages
    ...
)

// Inside the package:
Text("package_key", bundle: .module)  // .module is auto-generated
String(localized: "package_key", bundle: .module)

// Expose bundle for consumers if needed:
public extension Bundle {
    static let myPackage = Bundle.module
}
```

**Common enterprise bug:** Omitting `bundle:` in a Swift Package. The lookup silently defaults to `Bundle.main` and returns the untranslated key.

## Enum Display Names

```swift
// ❌ Enum rawValue is a String variable — no localization
enum Tab: String { case home, search, profile }
Text(tab.rawValue)  // Displays "home", "search", "profile"

// ✅ Computed property with localized string
enum Tab: String {
    case home, search, profile

    var displayName: LocalizedStringKey {
        switch self {
        case .home: "tab.home"
        case .search: "tab.search"
        case .profile: "tab.profile"
        }
    }
}
Text(tab.displayName)
```

## LocalizedStringResource (iOS 16+)

Bundles key, default value, comment, table, and bundle into a single type:

```swift
// Define
let resource = LocalizedStringResource("greeting", defaultValue: "Hello!",
    comment: "Main greeting on home screen")

// Use anywhere
String(localized: resource)
Text(resource)
```

Ideal for passing localizable content across API boundaries and into ViewModels.

## Developer String Leaks

```swift
// ❌ These enter the String Catalog as translation entries
Text("TODO: fix this")
Text("Debug: \(value)")

// ✅ Prevent extraction
Text(verbatim: "TODO: fix this")
#if DEBUG
Text(verbatim: "Debug: \(value)")
#endif
```
