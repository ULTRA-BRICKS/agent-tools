# iOS Localization — All 30 Rules Ranked by Severity

## 🔴 CRITICAL — Wrong output for non-English users

| # | Rule | Pattern to Detect |
|---|------|-------------------|
| 4 | Never concatenate localized string fragments | `"Hello, " + name`, `"\(greeting), \(name)"` |
| 5 | Always use positional format specifiers | `"%@ invited %@"` without positional `%1$@` |
| 6 | Implement ALL CLDR-required plural categories | `.stringsdict`/`.xcstrings` with only `one`/`other` for Slavic/Arabic |
| 7 | Never use `count == 1 ? singular : plural` | Ternary on count for plural selection |
| 8 | Polish `one` differs from Russian `one` | Copied plural logic between Slavic languages |
| 13 | Never use custom `dateFormat` for user-facing dates | `dateFormat = "MM/dd/yyyy"` on displayed dates |
| 14 | Set `en_US_POSIX` locale for API date parsing | `DateFormatter` without `locale = Locale(identifier: "en_US_POSIX")` |
| 15 | Use `yyyy` not `YYYY` | `YYYY` in date format (week-of-year year) |
| 16 | Never interpolate currency values | `"\(price) $"` or `"$\(price)"` |
| 17 | Always use leading/trailing, never left/right | `.left`/`.right` in Auto Layout constraints |
| 18 | Set text alignment to `.natural`, never `.left` | `textAlignment = .left` |
| 19 | Never use `DD` for day of month | `DD` in date format (day of year 1-366) |

## 🟡 HIGH — Localization debt or broken extraction

| # | Rule | Pattern to Detect |
|---|------|-------------------|
| 1 | Never hardcode user-facing strings | `label.text = "Loading..."` without localization |
| 2 | Never use English text as localization key | `NSLocalizedString("Water", comment: "")` |
| 3 | Always provide meaningful translator comments | `comment: ""` or missing comment |
| 9 | String variable in Text() skips localization | `Text(stringVariable)` where variable is `String` |
| 11 | In Swift Packages, always pass `bundle: .module` | Missing `bundle:` parameter in Package code |
| 12 | Never use `NSLocalizedString` with interpolation | `NSLocalizedString("Hello \(name)", comment: "")` |
| 19 | Never use fixed widths on localized text | `frame(width: 120)` on localized `Text` |
| 20 | Localize all accessibility labels and hints | `accessibilityLabel("Close")` hardcoded |
| 25 | Use `String(localized:)` over `NSLocalizedString` for iOS 15+ | `NSLocalizedString` in modern codebases |

## 🟢 MEDIUM — Non-optimal patterns

| # | Rule | Pattern to Detect |
|---|------|-------------------|
| 10 | Use `Text(verbatim:)` for non-localizable strings | `Text("v2.0")` without `verbatim:` |
| 21 | Use `localizedStringWithFormat` for user-facing numbers | `String(format:)` for displayed numbers |
| 23 | Force LTR for inherently directional content | Phone numbers, playback controls without `.forceLeftToRight` |
| 27 | Never localize `accessibilityIdentifier` | Localized identifier (for test automation) |
| 28 | Use String Catalogs for new projects on Xcode 15+ | Creating `.strings` files manually |
| 29 | Test with pseudolanguages before shipping | No pseudolanguage testing configured |
| 30 | Observe `currentLocaleDidChangeNotification` | Missing notification observer for locale changes |

## Quick Do's and Don'ts

### Do's
- DO use semantic localization keys: `"settings.account.delete"`
- DO provide translator comments with UI context and examples
- DO use positional specifiers: `"%1$@ invited %2$@"`
- DO use `Text(verbatim:)` for versions, URLs, identifiers
- DO pass `bundle: .module` in Swift Packages
- DO use `Date.formatted()` or `.dateStyle/.timeStyle` for displayed dates
- DO set `en_US_POSIX` locale on API DateFormatters
- DO use leading/trailing constraints exclusively
- DO test with Xcode pseudolanguages (Double Length, RTL, Accented, Bounded)

### Don'ts
- DON'T concatenate string fragments: `"Hello, " + name`
- DON'T use `count == 1 ? singular : plural` conditionals
- DON'T copy plural rules between Slavic languages (Polish ≠ Russian)
- DON'T use custom `dateFormat` for user-facing dates
- DON'T use `YYYY` (week-of-year year) or `DD` (day of year)
- DON'T interpolate currency: `"\(price) $"`
- DON'T use left/right constraints or `.left` text alignment
- DON'T hardcode accessibility labels in English
- DON'T use `NSLocalizedString("Hello \(name)", ...)` — interpolation creates unique keys per value
