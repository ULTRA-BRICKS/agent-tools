# CLDR Pluralization — Complete Reference

## Categories Required by Language

| Language | Categories Required | Notes |
|---|---|---|
| English, Spanish, Portuguese, Italian, French, German, Dutch | `one`, `other` | 2 forms |
| Russian, Ukrainian | `one`, `few`, `many`, `other` | 4 forms, complex rules |
| Polish | `one`, `few`, `many`, `other` | 4 forms, DIFFERENT from Russian! |
| Czech, Slovak | `one`, `few`, `many`, `other` | 4 forms |
| Arabic | `zero`, `one`, `two`, `few`, `many`, `other` | All 6 forms |
| Japanese, Chinese, Korean, Turkish, Thai, Vietnamese | `other` | 1 form only |
| Hindi | `one`, `other` | `one` = 0 or 1 (different from English!) |

## Russian/Ukrainian Rules

| Category | Rule | Numbers |
|---|---|---|
| `one` | Ends in 1, NOT 11 | 1, 21, 31, 41, 51, 101, 121... |
| `few` | Ends in 2-4, NOT 12-14 | 2, 3, 4, 22, 23, 24, 32... |
| `many` | Ends in 0, 5-9, or 11-14 | 0, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 20, 25... |
| `other` | Fractions | 1.5, 2.7, 0.3... |

**Example: "файл" (file)**
| Number | Category | Form |
|---|---|---|
| 1 | one | 1 файл |
| 2 | few | 2 файла |
| 5 | many | 5 файлов |
| 11 | many | 11 файлов |
| 21 | one | 21 файл |
| 22 | few | 22 файла |
| 100 | many | 100 файлов |
| 101 | one | 101 файл |
| 1.5 | other | 1.5 файла |

## Polish Rules — DIFFERENT FROM RUSSIAN

| Category | Rule | Numbers |
|---|---|---|
| `one` | ONLY the integer 1 | 1 |
| `few` | Ends in 2-4, NOT 12-14 | 2, 3, 4, 22, 23, 24, 32... |
| `many` | Everything else integer | 0, 5-21, 25-31, 35-41... |
| `other` | Fractions | 1.5, 2.7... |

**Critical difference:** Number 21:
- Russian → `one`: "21 файл"
- Polish → `many`: "21 plików"

Number 101:
- Russian → `one`: "101 файл"
- Polish → `many`: "101 plików"

## Arabic Rules

| Category | Rule | Numbers |
|---|---|---|
| `zero` | 0 | 0 |
| `one` | 1 | 1 |
| `two` | 2 | 2 |
| `few` | 3-10, 103-110, 203-210... | 3, 4, 5, 6, 7, 8, 9, 10, 103... |
| `many` | 11-99, 111-199, 211-299... | 11, 12, 99, 111... |
| `other` | 100-102, 200-202, 300-302... | 100, 101, 102, 200... |

## Verification Test Set

For ANY language with complex plural rules, test these numbers:
```
[0, 1, 2, 3, 4, 5, 10, 11, 12, 14, 20, 21, 22, 25, 100, 101, 111, 1.5]
```

This set covers all boundary conditions for Russian, Ukrainian, Polish, Czech, and Arabic plural rules.

## .stringsdict Format

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>items_count</key>
    <dict>
        <key>NSStringLocalizedFormatKey</key>
        <string>%#@count@</string>
        <key>count</key>
        <dict>
            <key>NSStringFormatSpecTypeKey</key>
            <string>NSStringPluralRuleType</string>
            <key>NSStringFormatValueTypeKey</key>
            <string>lld</string>
            <key>one</key>
            <string>%lld item</string>
            <key>other</key>
            <string>%lld items</string>
        </dict>
    </dict>
</dict>
</plist>
```

**Important:** Use `lld` for Swift `Int`, not `d` or `ld`.

## String Catalog Plurals

In Xcode: right-click a key → "Vary by Plural" → Xcode shows all CLDR-required categories for each language automatically. This is the safest approach — Xcode knows which categories each language needs.
