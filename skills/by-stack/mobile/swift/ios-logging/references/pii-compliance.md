# PII Compliance in Logging

GDPR and CCPA both classify IP addresses, device IDs, and behavioral data as personal information. The app publisher is the data controller responsible for all data processing — including what happens inside third-party SDKs.

## What to Never Log

### Authentication data
- Passwords, password hashes
- JWT/OAuth tokens, refresh tokens
- API keys, client secrets
- Session tokens, CSRF tokens

### Personal identifiers
- Email addresses (use `.private(mask: .hash)` if correlation needed)
- Phone numbers
- Full names
- Social Security Numbers, passport numbers
- IDFA, IDFV (without consent)

### Sensitive data
- Location data (without explicit consent)
- Health data (HealthKit)
- Financial data (account numbers, card numbers)
- Communication contents (messages, emails)

### Operational leaks (commonly missed)
- Full HTTP request/response bodies (may contain auth headers)
- Database queries with user parameters
- URL paths containing emails or user IDs (`/users/john@example.com/profile`)
- Stack traces with user data in variable names
- Core Data fault descriptions (may contain entity values)

## Privacy Manifests (Enforced Since May 2024)

`PrivacyInfo.xcprivacy` files must declare any use of Required Reason APIs. Apps submitted without proper manifests are **rejected during App Store review**.

APIs requiring declaration:
- `UserDefaults`
- File timestamp APIs (`NSFileCreationDate`, `NSFileModificationDate`)
- System boot time APIs
- Disk space APIs

Crash reporting SDKs (Sentry, Crashlytics, Bugfender) ship their own manifests — Xcode merges them automatically. If an SDK lacks a manifest, add declarations to your own.

## Crash Reporting and ATT Consent

Crash reporting generally **does not require App Tracking Transparency consent**, provided:
- Data is used only for improving your app
- Data is not shared with data brokers
- Data is not linked with third-party data for advertising

Apple's definition of "tracking" specifically refers to linking user/device data across different companies' apps and websites. Error diagnostics for your own app are not tracking.

However, Firebase Crashlytics's terms (Section 2.6) require developers to obtain EU user consent for data transfer and storage. Read your SDK's specific terms.

## Redaction Patterns

### os.Logger privacy annotations (first line of defense)

```swift
logger.notice("Login for \(username, privacy: .private(mask: .hash)), plan: \(plan, privacy: .public)")
```

### Manual redaction for crash reporting metadata

```swift
enum Redactor {
    static func maskID(_ id: String) -> String {
        guard id.count > 4 else { return "****" }
        return "***\(id.suffix(4))"
    }

    static func maskEmail(_ email: String) -> String {
        guard let at = email.firstIndex(of: "@") else { return "***" }
        return "\(email.prefix(1))***\(email[at...])"
    }

    static func maskPhone(_ phone: String) -> String {
        let digits = phone.filter(\.isNumber)
        guard digits.count > 4 else { return "****" }
        return "***\(digits.suffix(4))"
    }
}

// Usage in error reporting context
ErrorReporter.shared.recordNonFatal(error, context: [
    "userId": Redactor.maskID(userId),       // "***4f8a"
    "email": Redactor.maskEmail(email),       // "j***@example.com"
    "operation": "checkout"                    // Non-PII: no redaction needed
])
```

### Property wrapper for model-level redaction

```swift
@propertyWrapper
struct Redacted<Value> {
    var wrappedValue: Value
}

extension Redacted: CustomStringConvertible {
    var description: String { "--redacted--" }
}

extension Redacted: CustomDebugStringConvertible {
    var debugDescription: String { "--redacted--" }
}

// Usage
struct User {
    let id: String
    @Redacted var email: String
    @Redacted var phoneNumber: String
}

// print(user) or string interpolation shows:
// User(id: "abc123", email: --redacted--, phoneNumber: --redacted--)
```

This prevents accidental PII exposure through `print()`, `String(describing:)`, or log interpolation. The `@Redacted` wrapper ensures that even careless logging won't leak the value.
