# Date, Number & Currency Formatting

## Date Parsing (API/Server)

**Rule: ALWAYS use `en_US_POSIX` locale for fixed-format date parsing.**

```swift
// ❌ Breaks for Buddhist calendar (year 2568) and 12-hour time users
let formatter = DateFormatter()
formatter.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
let date = formatter.date(from: "2025-03-29T14:30:00")  // May fail!

// ✅ Fixed locale — always works
let formatter = DateFormatter()
formatter.locale = Locale(identifier: "en_US_POSIX")
formatter.timeZone = TimeZone(identifier: "UTC")  // Or server's timezone
formatter.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
let date = formatter.date(from: "2025-03-29T14:30:00")  // Always works

// ✅ iOS 16+ — ISO8601 parser
let date = try Date.ISO8601FormatStyle().parse("2025-03-29T14:30:00Z")
```

## Date Display (User-Facing)

**Rule: NEVER use custom `dateFormat` for user-facing dates.**

```swift
// ❌ Month/day order is English-specific
formatter.dateFormat = "MM/dd/yyyy"

// ✅ iOS 15+ — Date.formatted()
label.text = date.formatted(.dateTime.month().day().year())
// US: "Mar 29, 2025"    Germany: "29. Mär. 2025"    Japan: "2025年3月29日"

// ✅ System styles
formatter.dateStyle = .medium
formatter.timeStyle = .short

// ✅ Custom pattern that auto-reorders per locale
formatter.setLocalizedDateFormatFromTemplate("MMMMd")
// US: "March 29"    Germany: "29. März"    Japan: "3月29日"
```

## Date Format Specifier Traps

| Specifier | Meaning | Common Mistake |
|---|---|---|
| `yyyy` | Calendar year (2025) | `YYYY` = ISO week year, wrong near Jan 1 |
| `dd` | Day of month (01-31) | `DD` = Day of year (001-366) |
| `MM` | Month (01-12) | `mm` = Minutes |
| `HH` | 24-hour (00-23) | `hh` = 12-hour (requires AM/PM) |
| `ss` | Seconds | `SS` = Fractional seconds |

**YYYY vs yyyy example:**
```
Date: December 30, 2024 (Monday)
yyyy: 2024 ✅ (calendar year)
YYYY: 2025 ❌ (ISO week year — this week belongs to 2025)
```

## Number Formatting

```swift
// ❌ Always Western Arabic digits
label.text = String(format: "%d items", count)

// ✅ Locale-aware (Eastern Arabic digits in ar locale)
label.text = String.localizedStringWithFormat(
    NSLocalizedString("items_count", comment: ""), count)

// ✅ iOS 15+
label.text = count.formatted()  // Locale-aware by default
```

## Currency Formatting

```swift
// ❌ Symbol position, decimals, grouping all wrong for most locales
label.text = "$\(price)"             // US-only
label.text = "\(price) €"            // Wrong decimal separator for many EU locales
label.text = String(format: "$%.2f", price)  // Hardcoded everything

// ✅ iOS 15+
label.text = price.formatted(.currency(code: "USD"))
// US: "$9.99"    Germany: "9,99 $"    Japan: "$9.99"

// ✅ Older
let formatter = NumberFormatter()
formatter.numberStyle = .currency
formatter.currencyCode = "USD"
label.text = formatter.string(from: NSNumber(value: price))
```

**Currency formatting varies by locale:**
| Locale | Format for $1,234.56 |
|---|---|
| en_US | $1,234.56 |
| de_DE | 1.234,56 $ |
| fr_FR | 1 234,56 $ |
| ja_JP | $1,234.56 |
| ar_SA | US$ ١٬٢٣٤٫٥٦ |

## Percentage Formatting

```swift
// ❌ Hardcoded
label.text = "\(Int(progress * 100))%"

// ✅ Locale-aware
label.text = progress.formatted(.percent)
```

## Measurement Formatting

```swift
// ✅ Auto-converts units per locale
let distance = Measurement(value: 5, unit: UnitLength.kilometers)
label.text = distance.formatted()
// US: "3.1 mi"    Germany: "5 km"    UK: "3.1 mi"
```

## Formatter Caching

```swift
// ❌ Cached formatter uses stale locale
static let dateFormatter: DateFormatter = {
    let f = DateFormatter()
    f.dateStyle = .medium
    return f
}()

// ✅ Invalidate on locale change
NotificationCenter.default.addObserver(
    forName: NSLocale.currentLocaleDidChangeNotification,
    object: nil, queue: .main) { _ in
    self.invalidateFormatters()
}
```
