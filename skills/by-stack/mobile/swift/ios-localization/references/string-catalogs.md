# String Catalogs (.xcstrings) — Complete Reference

## Format Overview

String Catalogs replace both `.strings` and `.stringsdict` with a single JSON file. Introduced at WWDC 2023 (Xcode 15), default for all new projects by WWDC 2025.

### JSON Structure

```json
{
  "sourceLanguage": "en",
  "version": "1.0",
  "strings": {
    "greeting_format": {
      "extractionState": "manual",
      "comment": "Greeting with user name, e.g. 'Hello, Maria!'",
      "localizations": {
        "en": {
          "stringUnit": {
            "state": "translated",
            "value": "Hello, %@!"
          }
        },
        "ru": {
          "stringUnit": {
            "state": "translated",
            "value": "Привет, %@!"
          }
        }
      }
    }
  }
}
```

### Plural Variations Structure

```json
{
  "items_count": {
    "localizations": {
      "ru": {
        "variations": {
          "plural": {
            "one": { "stringUnit": { "state": "translated", "value": "%lld элемент" } },
            "few": { "stringUnit": { "state": "translated", "value": "%lld элемента" } },
            "many": { "stringUnit": { "state": "translated", "value": "%lld элементов" } },
            "other": { "stringUnit": { "state": "translated", "value": "%lld элемента" } }
          }
        }
      }
    }
  }
}
```

### Extraction States

| State | Meaning |
|---|---|
| `extracted_with_value` | Auto-extracted from source code |
| `manual` | Manually added by developer |
| `migrated` | Imported from .strings/.stringsdict |
| `stale` | Key no longer found in source code |

### Translation States

| State | Meaning |
|---|---|
| `new` | No translation yet |
| `needs_review` | Translation exists but needs review |
| `translated` | Translation complete |

## WWDC Evolution

| Year | Xcode | Features |
|---|---|---|
| 2023 | 15 | Format introduced, visual editor, auto-extraction |
| 2024 | 16 | Mismatched format specifier validation, "Don't Translate" marking |
| 2025 | 26 | Auto-generated comments (ML), type-safe Swift symbols, `#bundle` macro |

## Critical Pitfalls

### Merge Conflicts
`.xcstrings` are large single JSON files. Git text merge cannot understand JSON tree structure — semantically non-conflicting changes appear as textual conflicts. **Mitigations:**
- Split strings across multiple catalogs (by feature/module)
- Use Swift Packages with per-package catalogs
- Review diffs carefully — most conflicts are in unrelated keys

### Comment Order Randomization (Xcode Bug)
When the same key appears with different comments across source files, Xcode concatenates them with `\n` in random order between builds. This causes false-positive dirty Git diffs. **Mitigation:** Use one canonical comment per key.

### Plurals Without Visible Number
String Catalogs cannot handle "Day"/"Days" without showing the count. For this pattern, `.stringsdict` is still required alongside `.xcstrings`.

### Same Table Name Coexistence
`.strings` and `.xcstrings` files CANNOT coexist with the same table name. Migration must be complete per table.

### String Variable Extraction
Strings passed as `String` variables (not literals) won't be extracted:
```swift
let message = "greeting_key"
Text(message)  // NOT extracted — it's a String variable, not a literal
Text("greeting_key")  // Extracted — it's a literal
```

## Xcode 26 Features

### Auto-Generated Comments
On-device ML model generates translator comments automatically from context. Reduces the "empty comment" problem but doesn't eliminate the need for developer-provided context.

### Type-Safe Swift Symbols
Generated from `LocalizedStringResource` entries:
```swift
// Generated automatically
Text(.greetingFormat(name))  // Type-safe, autocompleted
// Instead of
Text("greeting_format \(name)")  // String literal, no type safety
```

### `#bundle` Macro
Resolves correct bundle across packages and frameworks:
```swift
Text("key", bundle: #bundle)  // Auto-resolves to correct bundle
```
