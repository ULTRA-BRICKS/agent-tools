# os.Logger Setup and Privacy Annotations

Apple's `os.Logger` API (iOS 14+) is the production logging system. It uses a binary representation that is not string-converted at the call site, meaning debug-level messages have effectively zero cost when not being streamed.

## Logger Extension Pattern

```swift
import os

extension Logger {
    private static let subsystem = Bundle.main.bundleIdentifier!

    static let networking = Logger(subsystem: subsystem, category: "Networking")
    static let auth       = Logger(subsystem: subsystem, category: "Authentication")
    static let database   = Logger(subsystem: subsystem, category: "Database")
    static let ui         = Logger(subsystem: subsystem, category: "UI")
    static let payments   = Logger(subsystem: subsystem, category: "Payments")
    static let lifecycle  = Logger(subsystem: subsystem, category: "Lifecycle")
}
```

Add categories as needed for your domain. The subsystem is typically the bundle identifier; the category groups related log messages for filtering.

## Log Levels

| Level | Persisted? | Production visibility | Use for |
|---|---|---|---|
| `.debug` / `.trace` | Memory only, discarded if not streaming | Only when live-streaming | Development tracing, hot-path diagnostics |
| `.info` | Memory only (captured when faults occur) | Only in log archives | Helpful but non-critical context |
| `.notice` (default) | **Persisted to disk** | Yes | Important operational information |
| `.error` | **Always persisted** | Yes (yellow in Xcode) | Error conditions, recoverable failures |
| `.fault` | **Always persisted** + process chain info | Yes (red in Xcode) | Bugs, system-level failures, unrecoverable |

**`.debug` messages are free in production** — the compiler optimizes away message creation entirely. You can leave verbose debug logging in production code safely.

## Privacy Annotations (Mandatory)

This is the most important Logger feature. **Dynamic strings are redacted by default** in production logs — they appear as `<private>` without a debugger attached. Numeric types are public by default.

```swift
// ❌ BAD — No privacy annotation, useless in production logs
logger.error("Request to \(url) failed for user \(userId)")
// Production output: "Request to <private> failed for user <private>"

// ✅ GOOD — Explicit privacy annotations
logger.error("Request to \(url.absoluteString, privacy: .public) failed for user \(userId, privacy: .private(mask: .hash))")
// Production output: "Request to /api/users failed for user <mask.hash: 'uZiZmp5vMXG4evDH=='>"
```

### Privacy annotation options

| Annotation | Behavior | Use for |
|---|---|---|
| `privacy: .public` | Always visible in production logs | URLs paths, operation names, error codes, status codes |
| `privacy: .private` | Redacted as `<private>` in production | User IDs, emails, names, device IDs |
| `privacy: .private(mask: .hash)` | Consistent hash — correlate without exposing | User IDs when you need cross-event correlation |
| `privacy: .sensitive` | Always redacted, even in development | Passwords, tokens, API keys (though these shouldn't be logged at all) |
| (no annotation on strings) | Default: `<private>` in production | Dangerous — always specify explicitly |

### Decision guide for privacy level

```
Is this value safe for anyone with log access to see?
├── YES (URL paths, error descriptions, status codes, operation names)
│   └── privacy: .public
├── NO — Is it PII or user-specific data?
│   ├── YES — Do you need to correlate events for the same user?
│   │   ├── YES → privacy: .private(mask: .hash)
│   │   └── NO  → privacy: .private
│   └── NO — Is it a secret (token, password, key)?
│       └── DON'T LOG IT AT ALL. If you must reference it:
│           → privacy: .sensitive
└── UNSURE → Default to .private — you can always relax later
```

### Common logging patterns

```swift
// Network request
Logger.networking.info("GET \(url.path, privacy: .public) started")
Logger.networking.error("GET \(url.path, privacy: .public) failed: HTTP \(statusCode, privacy: .public)")

// Authentication
Logger.auth.notice("Login attempt for \(email, privacy: .private(mask: .hash))")
Logger.auth.error("Auth failed: \(error.localizedDescription, privacy: .public)")

// Database
Logger.database.info("Fetching \(entityName, privacy: .public), predicate: \(predicateDesc, privacy: .private)")
Logger.database.error("Save failed: \(nsError.code, privacy: .public) \(nsError.domain, privacy: .public)")

// Lifecycle
Logger.lifecycle.notice("App entered background, \(pendingUploads, privacy: .public) uploads pending")
```

## Replacing print() Systematically

| print() pattern | Logger replacement |
|---|---|
| `print("Loading data...")` | `Logger.networking.debug("Loading data...")` |
| `print("Error: \(error)")` | `Logger.networking.error("Failed: \(error.localizedDescription, privacy: .public)")` |
| `print("User: \(user.email)")` | `Logger.auth.info("User: \(user.email, privacy: .private(mask: .hash))")` |
| `print("Response: \(data)")` | `Logger.networking.debug("Response size: \(data.count, privacy: .public) bytes")` |
| `debugPrint(object)` | `Logger.ui.debug("State: \(String(describing: object), privacy: .private)")` |

## WWDC Sessions

- **"Explore logging in Swift" (WWDC 2020, 10168)** — introduced Logger API
- **"Debug with structured logging" (WWDC 2023, 10226)** — Xcode 15 structured console
