# Compiler Flags & CI Configuration

## How to Use This Reference

Read this when setting up strict concurrency checking in build settings, configuring Swift Package Manager targets for gradual migration, enabling Thread Sanitizer in CI, or adopting Swift 6.2 feature flags incrementally.

---

## SWIFT_STRICT_CONCURRENCY Build Setting 🟢

The Xcode build setting `SWIFT_STRICT_CONCURRENCY` controls how aggressively the compiler checks for concurrency safety.

| Value | Behavior | Use When |
|-------|----------|----------|
| `minimal` | Only checks code that uses concurrency features | Default in Swift 5.x projects |
| `targeted` | Checks code that uses concurrency + types passed across boundaries | First migration step |
| `complete` | Full Swift 6-level checking for ALL code | Required before enabling Swift 6 mode |

**Xcode path:** Build Settings → Swift Compiler - Upcoming Features → Strict Concurrency Checking

**Recommendation:**
- New projects: `complete` from day one
- Migration: `targeted` → fix all warnings → `complete` → fix all warnings → Swift 6 mode
- CI: Always `complete` on the main branch, even if the project is not yet in Swift 6 mode

---

## -strict-concurrency CLI Flag 🟢

For CI pipelines using command-line builds:

```bash
# Via OTHER_SWIFT_FLAGS in xcodebuild
xcodebuild build \
  -scheme MyApp \
  OTHER_SWIFT_FLAGS="-strict-concurrency=complete"

# Equivalent build setting
xcodebuild build \
  -scheme MyApp \
  SWIFT_STRICT_CONCURRENCY=complete
```

**Mapping:**

| CLI Flag | Build Setting |
|----------|--------------|
| `-strict-concurrency=minimal` | `SWIFT_STRICT_CONCURRENCY=minimal` |
| `-strict-concurrency=targeted` | `SWIFT_STRICT_CONCURRENCY=targeted` |
| `-strict-concurrency=complete` | `SWIFT_STRICT_CONCURRENCY=complete` |

---

## SPM Per-Target StrictConcurrency 🟢

Enable strict concurrency per-target in `Package.swift` for gradual adoption:

```swift
// Swift 5.8-5.9: use experimental feature
.target(
    name: "NetworkingKit",
    swiftSettings: [
        .enableExperimentalFeature("StrictConcurrency")
    ]
)

// Swift 6.0+: use upcoming feature
.target(
    name: "NetworkingKit",
    swiftSettings: [
        .enableUpcomingFeature("StrictConcurrency")
    ]
)

// Apply to ALL targets with a loop
for target in package.targets {
    var settings = target.swiftSettings ?? []
    settings.append(.enableUpcomingFeature("StrictConcurrency"))
    target.swiftSettings = settings
}
```

**Per-target adoption** is the key to gradual migration. Leaf modules first, then work up the dependency graph.

---

## Swift 6.2 Feature Flags for Incremental Adoption 🟢

Swift 6.2 introduces several behavioral changes that can be adopted individually:

| Feature Flag | SE Proposal | Effect |
|-------------|-------------|--------|
| `NonisolatedNonsendingByDefault` | SE-0461 | nonisolated async runs on caller's actor |
| `InferIsolatedConformances` | SE-0470 | Compiler infers `@MainActor` on protocol conformances |
| `SendableCompletionHandlers` | — | Completion handler closures require Sendable |
| `-default-isolation MainActor` | SE-0466 | All unannotated code defaults to @MainActor |

**Umbrella setting (Xcode 26+):** `SWIFT_APPROACHABLE_CONCURRENCY = YES` enables all of the above. This is on by default for new Xcode 26 projects. For older projects or SPM targets, enable features individually via `.enableUpcomingFeature()` in Package.swift.

**In Package.swift:**

```swift
.target(
    name: "MyTarget",
    swiftSettings: [
        .enableUpcomingFeature("NonisolatedNonsendingByDefault"),
        .enableUpcomingFeature("InferIsolatedConformances"),
    ]
)
```

**Recommendation:** Adopt `NonisolatedNonsendingByDefault` first (biggest behavioral change) and test for main-thread hangs before enabling others.

---

## SwiftLint Concurrency Rules 🟢

Relevant SwiftLint rules for concurrent code enforcement:

| Rule | What It Catches |
|------|----------------|
| `no_unchecked_sendable` (custom) | Any `@unchecked Sendable` conformance |
| `async_without_await` | Async functions that never suspend (updated in 0.62.2 to exclude `@concurrent`) |
| `legacy_objc_type` | NSMutableArray etc. instead of Swift types (not Sendable) |
| `class_delegate_protocol` | Class-bound protocols (may need Sendable) |

**Custom rule for @unchecked Sendable detection:**

```yaml
# .swiftlint.yml
custom_rules:
  no_unchecked_sendable:
    name: "No @unchecked Sendable"
    regex: '@unchecked\s+Sendable'
    message: "Use actor, Mutex, or sending instead of @unchecked Sendable. If truly needed, add // swiftlint:disable:next no_unchecked_sendable with justification comment."
    severity: error
```

---

## LIBDISPATCH_COOPERATIVE_POOL_STRICT 🟢

This environment variable forces a single cooperative thread per priority, exposing deadlocks that only appear under load on multi-core devices.

**Set in Xcode test scheme:**
1. Edit Scheme → Test → Arguments → Environment Variables
2. Add: `LIBDISPATCH_COOPERATIVE_POOL_STRICT` = `1`

**In CI (xcodebuild):**

```bash
# Cannot set env vars directly for xcodebuild test;
# use the scheme's environment variables instead.
# Alternative: set in the test plan (.xctestplan file)
```

**What it catches:**
- `DispatchSemaphore.wait()` in async contexts
- `Thread.sleep()` in async contexts
- Synchronous I/O blocking cooperative threads
- Apple framework calls that internally block (Vision, CoreML)

---

## Recommended CI Pipeline

```text
Step 1: Build with strict concurrency
  xcodebuild build -scheme MyApp SWIFT_STRICT_CONCURRENCY=complete
  → Catches all concurrency warnings as errors

Step 2: Run tests with Thread Sanitizer
  xcodebuild test -scheme MyApp -enableThreadSanitizer YES
  → Catches data races at runtime

Step 3: Run tests with cooperative pool strict mode
  (Scheme env: LIBDISPATCH_COOPERATIVE_POOL_STRICT=1)
  xcodebuild test -scheme MyApp
  → Catches cooperative pool deadlocks

Step 4: Run tests in Release configuration
  xcodebuild test -scheme MyApp -configuration Release
  → Catches release-only crashes (optimizer-related)

Step 5: SwiftLint enforcement
  swiftlint lint --strict
  → Enforces @unchecked Sendable ban, async-without-await
```

**Note:** Steps 2 and 3 should be separate CI jobs — TSan and cooperative pool strict mode can be combined, but TSan cannot run with ASan simultaneously.

---

## Build Setting Quick Reference

| Setting | Location | Recommended Value |
|---------|----------|-------------------|
| `SWIFT_STRICT_CONCURRENCY` | Build Settings → Swift Compiler | `complete` |
| `SWIFT_VERSION` | Build Settings → Swift Compiler | `6` (when ready) |
| Thread Sanitizer | Scheme → Test → Diagnostics | ✅ Enabled |
| `LIBDISPATCH_COOPERATIVE_POOL_STRICT` | Scheme → Test → Environment | `1` |
| `SWIFT_UPCOMING_FEATURE_FLAGS` | Build Settings → Swift Compiler | Per-feature adoption |
