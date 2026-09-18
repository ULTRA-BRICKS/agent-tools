# Crash SDK Integration

Never call Sentry, Crashlytics, or PostHog APIs directly throughout your codebase. Use a protocol-based abstraction that wraps any crash reporting SDK, enabling testability, vendor swapping, and consistent error metadata.

## The ErrorReporter Protocol

```swift
protocol ErrorReporter: Sendable {
    func recordNonFatal(_ error: Error, context: [String: Any])
    func addBreadcrumb(message: String, category: String, level: BreadcrumbLevel, data: [String: Any]?)
    func setUserID(_ userID: String?)
    func setCustomKey(_ key: String, value: Any)
    func log(_ message: String)
}

enum BreadcrumbLevel: String, Sendable {
    case debug, info, warning, error, fatal
}
```

## Sentry Implementation

```swift
import SentrySwift

final class SentryErrorReporter: ErrorReporter {
    func recordNonFatal(_ error: Error, context: [String: Any]) {
        SentrySDK.capture(error: error) { scope in
            for (key, value) in context {
                scope.setExtra(value: value, key: key)
            }
        }
    }

    func addBreadcrumb(message: String, category: String, level: BreadcrumbLevel, data: [String: Any]?) {
        let crumb = Breadcrumb(level: sentryLevel(level), category: category)
        crumb.message = message
        crumb.data = data
        SentrySDK.addBreadcrumb(crumb)
    }

    func setUserID(_ userID: String?) {
        SentrySDK.configureScope { scope in
            scope.setUser(userID.map { User(userId: $0) })
        }
    }

    func setCustomKey(_ key: String, value: Any) {
        SentrySDK.configureScope { scope in
            scope.setExtra(value: value, key: key)
        }
    }

    func log(_ message: String) {
        SentrySDK.addBreadcrumb(Breadcrumb(level: .info, category: "log"))
    }

    private func sentryLevel(_ level: BreadcrumbLevel) -> SentryLevel {
        switch level {
        case .debug:   return .debug
        case .info:    return .info
        case .warning: return .warning
        case .error:   return .error
        case .fatal:   return .fatal
        }
    }
}
```

## Crashlytics Implementation

```swift
import FirebaseCrashlytics

final class CrashlyticsErrorReporter: ErrorReporter {
    func recordNonFatal(_ error: Error, context: [String: Any]) {
        let nsError = error as NSError
        var userInfo = nsError.userInfo
        for (key, value) in context {
            userInfo[key] = value
        }
        let enriched = NSError(domain: nsError.domain, code: nsError.code, userInfo: userInfo)
        Crashlytics.crashlytics().record(error: enriched)
    }

    func addBreadcrumb(message: String, category: String, level: BreadcrumbLevel, data: [String: Any]?) {
        // Crashlytics breadcrumbs are plain strings — no structured data support
        Crashlytics.crashlytics().log("[\(category)] \(message)")
    }

    func setUserID(_ userID: String?) {
        Crashlytics.crashlytics().setUserID(userID ?? "")
    }

    func setCustomKey(_ key: String, value: Any) {
        Crashlytics.crashlytics().setCustomValue(value, forKey: key)
    }

    func log(_ message: String) {
        Crashlytics.crashlytics().log(message)
    }
}
```

**Crashlytics limitation**: stores only the **8 most recent non-fatal exceptions per session**. Older ones are lost. If your app reports many non-fatals per session, consider sampling or deduplication.

## PostHog (Analytics, Not Crash Reporting)

PostHog as of early 2026 does not have native error tracking for iOS. The `captureException()` API exists for web SDKs only. On iOS, errors are captured as generic events:

```swift
final class PostHogErrorReporter: ErrorReporter {
    func recordNonFatal(_ error: Error, context: [String: Any]) {
        var properties = context.mapValues { "\($0)" }
        properties["error_type"] = String(describing: type(of: error))
        properties["error_message"] = error.localizedDescription
        PostHogSDK.shared.capture("app_error", properties: properties)
    }
    // ... other methods capture as PostHog events
}
```

PostHog should not be your primary crash reporter — pair it with Sentry or Crashlytics.

## Composite Reporter

When you need multiple backends:

```swift
final class CompositeErrorReporter: ErrorReporter {
    private let reporters: [ErrorReporter]

    init(_ reporters: ErrorReporter...) {
        self.reporters = reporters
    }

    func recordNonFatal(_ error: Error, context: [String: Any]) {
        reporters.forEach { $0.recordNonFatal(error, context: context) }
    }

    func addBreadcrumb(message: String, category: String, level: BreadcrumbLevel, data: [String: Any]?) {
        reporters.forEach { $0.addBreadcrumb(message: message, category: category, level: level, data: data) }
    }

    // ... delegate all methods
}
```

## Non-Fatal Errors Matter More Than Crashes

Crashes affect ~1–2% of sessions. Non-fatal errors can affect 10–30%+ silently. Non-fatal error reporting is the primary tool for detecting silent failures. Every catch block that handles an error should call `recordNonFatal` through the abstraction.

## Breadcrumbs

Breadcrumbs are timestamped events buffered locally and attached to the next error/crash report. They answer "what was the user doing before this broke?"

```swift
// Before a database migration
ErrorReporter.shared.addBreadcrumb(
    message: "Starting Core Data migration v2→v3",
    category: "database", level: .info, data: ["storeSize": storeFileSize]
)

// Before a payment operation
ErrorReporter.shared.addBreadcrumb(
    message: "User initiated checkout",
    category: "payment", level: .info, data: ["cartItems": 3, "total": 99.99]
)

// Before auth flow
ErrorReporter.shared.addBreadcrumb(
    message: "Starting OAuth flow",
    category: "auth", level: .info, data: ["provider": "apple"]
)
```

## Never Run Multiple Fatal Crash Reporters

Running Sentry and Crashlytics simultaneously causes **signal handler conflicts** — both install handlers for SIGABRT, SIGSEGV, SIGBUS, etc., and only the last one registered receives the signal. `NSSetUncaughtExceptionHandler` only supports one handler — the second call replaces the first.

If you must use multiple SDKs:
1. **Pick one for fatal crash reporting** (usually Crashlytics for Firebase ecosystem, Sentry for standalone)
2. **Disable crash handling on the secondary** — e.g., `options.enableCrashHandler = false` for Sentry
3. Use the secondary only for non-fatal capture, breadcrumbs, and analytics

## dSYMs Are Non-Negotiable

Without dSYM files, crash reports show hex addresses instead of function names.

**Setup checklist:**
- Build Settings → Debug Information Format → **"DWARF with dSYM File"** for ALL configurations (including Release)
- Apply to ALL targets (main app, extensions, widgets, watch app)
- Add dSYM upload script in Build Phases (Sentry and Crashlytics both provide scripts)
- Bitcode is deprecated since Xcode 14 — local dSYMs are authoritative
