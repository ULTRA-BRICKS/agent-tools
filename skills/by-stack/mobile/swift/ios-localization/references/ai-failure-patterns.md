# AI Failure Patterns — Complete Reference

30 patterns where AI coding assistants consistently generate broken localization code, with ❌/✅ code pairs.

## Table of Contents

1. [String Management (Rules 1-7)](#string-management)
2. [Pluralization (Rules 8-10)](#pluralization)
3. [SwiftUI-Specific (Rules 11-15)](#swiftui-specific)
4. [Date/Number/Currency (Rules 16-21)](#datenumbercurrency)
5. [Layout and RTL (Rules 22-25)](#layout-and-rtl)
6. [Accessibility (Rules 26-27)](#accessibility)
7. [Tooling (Rules 28-30)](#tooling)

## String Management

### Rule 1: Never hardcode user-facing strings
```swift
// ❌ AI-generated: no localization
label.text = "Loading..."
title = "Settings"

// ✅ UIKit (iOS 15+)
label.text = String(localized: "loading_indicator", defaultValue: "Loading...")
title = String(localized: "settings_title", defaultValue: "Settings")

// ✅ SwiftUI — auto-localized from literal
Text("Loading...")  // Extracted to String Catalog if key exists
```

### Rule 2: Never use English text as the localization key
```swift
// ❌ "Water" is ambiguous — noun (drink) or verb (garden)?
NSLocalizedString("Water", comment: "")

// ✅ Semantic keys disambiguate
NSLocalizedString("menu.drink.water", comment: "Water as a beverage option in the drink menu")
NSLocalizedString("garden.action.water", comment: "Action to water plants in the garden")
```

### Rule 3: Always provide meaningful translator comments
```swift
// ❌ Empty comment — translator has no context
NSLocalizedString("greeting_format", comment: "")

// ✅ Context, variables, and examples
NSLocalizedString("greeting_format", comment: "Greeting with user name, e.g. 'Hello, Maria!'")
String(localized: "items_count", defaultValue: "\(count) items",
       comment: "Number of items in shopping cart. %lld = item count, e.g. '3 items'")
```

### Rule 4: Never concatenate localized string fragments
```swift
// ❌ Prevents word reordering — breaks Japanese, Korean, Arabic
let message = NSLocalizedString("hello", comment: "") + ", " + name + "!"

// ✅ Single format string — translator controls word order
let format = NSLocalizedString("greeting_format", comment: "Greeting: %@ = user name")
let message = String(format: format, name)

// ✅ SwiftUI
Text("greeting_format \(name)")  // LocalizedStringKey interpolation
```

### Rule 5: Always use positional format specifiers
```swift
// ❌ Locks translators into English argument order
String(format: NSLocalizedString("invite_format", comment: ""), senderName, receiverName)
// Translator CANNOT reorder: "%@ invited %@"

// ✅ Positional specifiers allow reordering
// English: "%1$@ invited %2$@" → "Alice invited Bob"
// Korean: "%2$@가 %1$@을 초대했습니다" → "Bob가 Alice을 초대했습니다"
```

### Rule 6: Same key for different contexts
```swift
// ❌ "Order" as noun and verb share a key
Text("Order")  // In order history: "Your order"
Text("Order")  // As action button: "Place order"

// ✅ Separate keys with distinct comments
Text("order_history.title")  // comment: "Title for order history list"
Text("order_action.button")  // comment: "Button label to place a new order"
```

### Rule 7: Use String(localized:) over NSLocalizedString for iOS 15+
```swift
// ❌ Legacy API
let text = NSLocalizedString("welcome_message", comment: "Welcome screen greeting")

// ✅ Modern API — bidi isolation, locale-aware digits
let text = String(localized: "welcome_message", comment: "Welcome screen greeting")
```

## Pluralization

### Rule 8: Implement ALL CLDR-required plural categories
```swift
// ❌ AI generates only one/other — breaks Russian, Polish, Arabic
// .stringsdict with only:
//   one: "%lld file"
//   other: "%lld files"
// Russian result: "2 файлов" (WRONG — should be "2 файла")

// ✅ Russian needs one/few/many/other:
//   one: "%lld файл"      (1, 21, 31, 101...)
//   few: "%lld файла"     (2, 3, 4, 22, 23, 24...)
//   many: "%lld файлов"   (0, 5-20, 25-30...)
//   other: "%lld файла"   (1.5, 2.7...)
```

### Rule 9: Never use count-based conditionals for plurals
```swift
// ❌ Breaks for every language with >2 plural forms
let text = count == 1 ? "\(count) item" : "\(count) items"

// ✅ Use String Catalog plural variations or .stringsdict
// String Catalog: key "items_count" with plural variations per language
// The system selects the correct form based on CLDR rules
```

### Rule 10: Polish `one` differs from Russian `one`
```
Russian `one`: integer ends in 1 but not 11 → 1, 21, 31, 101 (NOT 11, 111)
Polish `one`:  ONLY the integer 1 → just 1

Number 21:
  Russian → one: "21 файл"
  Polish  → many: "21 plików"

Never copy plural logic between Slavic languages.
```

## SwiftUI-Specific

### Rule 11: Use Text(verbatim:) for non-localizable strings
```swift
// ❌ These enter the String Catalog as translation entries
Text("v2.0.1")
Text("https://example.com")
Text("TODO: fix this layout")

// ✅ Excluded from localization
Text(verbatim: "v2.0.1")
Text(verbatim: "https://example.com")
#if DEBUG
Text(verbatim: "TODO: fix this layout")
#endif
```

### Rule 12: Explicitly type LocalizedStringKey with interpolation
```swift
// ❌ Swift infers String — Image interpolation breaks
let text = "Delete \(Image(systemName: "trash"))"
// Result: "Delete Image(...)" as literal text

// ✅ Explicit type — Image renders correctly
let text: LocalizedStringKey = "Delete \(Image(systemName: "trash"))"
```

### Rule 13: Accept LocalizedStringKey in custom view parameters
```swift
// ❌ String parameter silently skips localization
struct CardView: View {
    let title: String  // Callers' literals won't localize
}

// ✅ LocalizedStringKey enables auto-localization
struct CardView: View {
    let title: LocalizedStringKey
}
```

### Rule 14: Pass bundle: .module in Swift Packages
```swift
// ❌ Looks up in Bundle.main — finds nothing
Text("package_greeting")  // Inside a Swift Package

// ✅ Correct bundle
Text("package_greeting", bundle: .module)
String(localized: "package_greeting", bundle: .module)
```

### Rule 15: Never use NSLocalizedString with string interpolation
```swift
// ❌ Creates a unique key PER VALUE — never matches translations
NSLocalizedString("Hello \(userName)", comment: "")
// Key is literally "Hello Alice", "Hello Bob", etc.

// ✅ Static key, dynamic value via format
String(format: NSLocalizedString("greeting_format", comment: ""), userName)
```

## Date/Number/Currency

### Rule 16: en_US_POSIX for API date parsing
```swift
// ❌ Breaks for Buddhist calendar users (year 2568) and 12-hour time
let formatter = DateFormatter()
formatter.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
let date = formatter.date(from: serverDateString) // FAILS

// ✅ Fixed locale for parsing
let formatter = DateFormatter()
formatter.locale = Locale(identifier: "en_US_POSIX")
formatter.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
let date = formatter.date(from: serverDateString) // Always works
```

### Rule 17: Never use custom dateFormat for user-facing dates
```swift
// ❌ Month/day order is English-specific
formatter.dateFormat = "MM/dd/yyyy"  // Wrong for most of the world

// ✅ iOS 15+
label.text = date.formatted(.dateTime.month().day().year())

// ✅ Older
formatter.dateStyle = .medium
formatter.timeStyle = .short

// ✅ Custom pattern that auto-reorders per locale
formatter.setLocalizedDateFormatFromTemplate("MMMMd")
```

### Rule 18: yyyy not YYYY, dd not DD
```swift
// ❌ YYYY = ISO week-numbering year — wrong near Jan 1
formatter.dateFormat = "YYYY-MM-dd"  // Dec 30, 2024 → "2025-12-30" !

// ❌ DD = day of year (1-366)
formatter.dateFormat = "MM/DD/yyyy"  // Jan 15 → "01/015/2025" !

// ✅ Correct
formatter.dateFormat = "yyyy-MM-dd"  // Calendar year
```

### Rule 20: Never interpolate currency
```swift
// ❌ Symbol position, decimals, grouping all wrong for most locales
label.text = "$\(price)"
label.text = "\(price) €"

// ✅ iOS 15+
label.text = price.formatted(.currency(code: "USD"))

// ✅ Older
let formatter = NumberFormatter()
formatter.numberStyle = .currency
formatter.currencyCode = "USD"
label.text = formatter.string(from: NSNumber(value: price))
```

### Rule 21: Use localizedStringWithFormat for user-facing numbers
```swift
// ❌ Always Western Arabic digits
label.text = String(format: "%d items", count)

// ✅ Locale-aware digits (Eastern Arabic in ar locale)
label.text = String.localizedStringWithFormat(
    NSLocalizedString("items_count", comment: ""), count)
```

## Layout and RTL

### Rule 22: Leading/trailing, never left/right
```swift
// ❌ Never flips for Arabic/Hebrew
NSLayoutConstraint.activate([
    label.leftAnchor.constraint(equalTo: view.leftAnchor, constant: 16),
    label.rightAnchor.constraint(equalTo: view.rightAnchor, constant: -16)
])

// ✅ Flips automatically for RTL
NSLayoutConstraint.activate([
    label.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 16),
    label.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -16)
])
```

### Rule 23: Force LTR for inherently directional content
```swift
// Phone numbers, playback controls, progress bars are always LTR
phoneLabel.semanticContentAttribute = .forceLeftToRight  // UIKit
progressBar.environment(\.layoutDirection, .leftToRight)  // SwiftUI
```

### Rule 24: Text alignment .natural, not .left
```swift
// ❌ Always left — broken for Arabic/Hebrew
label.textAlignment = .left

// ✅ Auto-adapts to language direction
label.textAlignment = .natural
```

### Rule 25: No fixed widths on localized text
```swift
// ❌ German averages 30% longer than English
Text(localizedString).frame(width: 120)

// ✅ Flexible layout
Text(localizedString).frame(maxWidth: .infinity, alignment: .leading)
```

## Accessibility

### Rule 26: Localize accessibility labels
```swift
// ❌ Hardcoded English
button.accessibilityLabel = "Close"

// ✅ Localized
button.accessibilityLabel = String(localized: "close_button.accessibility",
    comment: "Accessibility label for close button")
```

### Rule 27: Never localize accessibilityIdentifier
```swift
// ✅ Identifier is for test automation — must be stable across locales
button.accessibilityIdentifier = "closeButton"
```

## Tooling

### Rule 28: Use String Catalogs for new projects
Use `.xcstrings` instead of manually creating `.strings`/`.stringsdict` files on Xcode 15+.

### Rule 29: Test with pseudolanguages
Xcode → Edit Scheme → Options → Application Language: Double Length, Right to Left, Accented, Bounded.

### Rule 30: Observe locale change notifications
```swift
// ❌ Cached formatters use stale locale after user changes language
static let dateFormatter = DateFormatter()

// ✅ Invalidate on locale change
NotificationCenter.default.addObserver(forName: NSLocale.currentLocaleDidChangeNotification,
    object: nil, queue: .main) { _ in
    self.invalidateFormatters()
}
```
