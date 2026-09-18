# Silent Failure Patterns in Swift/iOS

These are the most common ways errors disappear in production iOS code. AI coding assistants produce every one of these anti-patterns by default because their training data is overwhelmingly tutorial code.

## Table of Contents
- [Task {} swallows errors](#task--swallows-errors)
- [try? erases diagnostics](#try-erases-diagnostics)
- [Combine pipelines die after one error](#combine-pipelines-die-after-one-error)
- [SwiftUI .task {} has no throws signature](#swiftui-task--has-no-throws-signature)
- [print(error) is not error handling](#printerror-is-not-error-handling)
- [URLSession does not throw for HTTP errors](#urlsession-does-not-throw-for-http-errors)
- [NotificationCenter silent failures](#notificationcenter-silent-failures)
- [Core Data save failures](#core-data-save-failures)

---

## Task {} swallows errors

`Task.init` is `@discardableResult`, so the compiler never warns when you ignore the returned `Task<Void, Error>`. If code inside throws, the error is silently discarded — no crash, no log, nothing.

```swift
// ❌ BAD — Error silently discarded
func loadData() {
    Task {
        try await fetchItems() // If this throws, nobody knows
    }
}

// ✅ GOOD — Handle errors inside the Task
func loadData() {
    Task {
        do {
            try await fetchItems()
        } catch {
            Logger.networking.error("Fetch failed: \(error.localizedDescription, privacy: .public)")
            ErrorReporter.shared.recordNonFatal(error, context: ["operation": "fetchItems"])
        }
    }
}
```

The rule: **every `Task {}` containing `try` must have an explicit `do/catch` with observability inside it**.

**Structured concurrency propagates errors automatically** — `withThrowingTaskGroup` cancels sibling tasks and propagates failures upward. Unstructured `Task {}` puts error handling entirely on you.

`Task.detached {}` has the same problem — and additionally strips priority inheritance, task-locals, and cancellation propagation. It is rarely the correct choice.

---

## try? erases diagnostics

`try?` converts any thrown error to `nil`, destroying the error type, message, and stack context. It is the single most dangerous convenience in Swift for production observability.

```swift
// ❌ BAD — Silent data loss
try? context.save()  // Core Data save failed? You'll never know why.

// ❌ BAD — Invisible network failure
let user = try? await fetchUser(id: userId)
// Was it a 401? A timeout? DNS failure? All information lost.

// ✅ GOOD — try? only for truly optional operations
try? FileManager.default.removeItem(at: tempCacheURL) // Cache cleanup — failure is fine

// ✅ GOOD — do/catch when the error matters
do {
    try context.save()
} catch {
    let nsError = error as NSError
    Logger.database.error("Core Data save failed: \(nsError.userInfo, privacy: .private)")
    ErrorReporter.shared.recordNonFatal(error, context: ["entity": entityName])
    context.rollback()
}
```

**`try?` is only acceptable for**: temp file cleanup, optional cache reads, cosmetic prefetches, and other best-effort operations where failure is genuinely irrelevant. For network, persistence, auth, or any user-facing operation, always use `do/catch`.

---

## Combine pipelines die after one error

When a Combine publisher emits a failure, it cancels itself and stops publishing permanently. Using `.replaceError(with: [])` at the pipeline end doesn't just hide one error — it kills the entire pipeline.

```swift
// ❌ BAD — Pipeline dies after first error, search stops working
$searchQuery
    .debounce(for: .seconds(0.5), scheduler: DispatchQueue.main)
    .flatMap { query in
        APIService.search(query)
    }
    .replaceError(with: [])  // One API error = search permanently broken
    .assign(to: &$results)

// ✅ GOOD — Handle errors inside flatMap to keep pipeline alive
$searchQuery
    .debounce(for: .seconds(0.5), scheduler: DispatchQueue.main)
    .flatMap { query in
        APIService.search(query)
            .retry(2)
            .catch { error -> Just<[Result]> in
                ErrorReporter.shared.recordNonFatal(error, context: ["query": query])
                return Just([])
            }
    }
    .receive(on: DispatchQueue.main)
    .assign(to: &$results)
```

`.replaceError()` changes the publisher's `Failure` type to `Never`, which terminates it. Error recovery must happen **inside `flatMap`** to keep the outer pipeline alive.

---

## SwiftUI .task {} has no throws signature

The `.task` modifier's signature is `@Sendable () async -> Void` — it does not throw. Errors from async code inside `.task` must be caught explicitly.

```swift
// ❌ BAD — Error vanishes, user sees indefinite loading
.task {
    profile = try? await fetchProfile(id: 1) // Failure → nil forever
}

// ✅ GOOD — Explicit error state with CancellationError handling
.task {
    do {
        profile = try await fetchProfile(id: 1)
    } catch is CancellationError {
        // View disappeared — expected, don't report
    } catch {
        loadError = error
        ErrorReporter.shared.recordNonFatal(error, context: ["screen": "profile"])
    }
}
```

**Critical nuance**: when a view disappears, `.task` cancels its work by throwing `CancellationError`. Always distinguish cancellation from real errors — cancellation is normal lifecycle behavior, not a failure. Reporting CancellationError as an error creates noise.

---

## print(error) is not error handling

`print()` writes to stdout, which:
- Is not part of the unified logging system
- Has no log levels, no privacy controls, no filtering, no persistence
- Is invisible on production devices (no debugger attached)
- Cannot be streamed remotely or searched in log archives

Replace every `print()` in error-handling code with `Logger.<category>.error()`.

---

## URLSession does not throw for HTTP errors

Only transport-level errors (no network, DNS failure, timeout) cause `URLSession` to throw. A `200` and a `500` both return `(Data, URLResponse)` successfully.

```swift
// ❌ BAD — Treats all responses as success
let (data, _) = try await URLSession.shared.data(from: url)

// ✅ GOOD — Validate HTTP status code explicitly
let (data, response) = try await URLSession.shared.data(from: url)
guard let httpResponse = response as? HTTPURLResponse else {
    throw NetworkError.invalidResponse
}
guard (200...299).contains(httpResponse.statusCode) else {
    Logger.networking.error("HTTP \(httpResponse.statusCode, privacy: .public) for \(url.path, privacy: .public)")
    throw NetworkError.httpError(statusCode: httpResponse.statusCode, data: data)
}
```

---

## NotificationCenter silent failures

Notifications fail silently when:
- **String-based names have typos** — missed notifications with no compiler warning
- **Observer token is not stored** — observation immediately deallocates
- **userInfo type casts fail** — guard-let falls through silently

Prefer typed `Notification.Name` constants and store observer tokens as properties.

---

## Core Data save failures

`context.save()` failures commonly occur from validation errors, merge conflicts, constraint violations, and threading violations. These are frequently hidden by `try?` or `print`-only catch blocks.

```swift
// ✅ GOOD — Full error context for Core Data saves
do {
    try context.save()
} catch {
    let nsError = error as NSError
    Logger.database.error("Core Data save failed: code=\(nsError.code, privacy: .public) domain=\(nsError.domain, privacy: .public)")
    if let detailedErrors = nsError.userInfo[NSDetailedErrorsKey] as? [NSError] {
        for detail in detailedErrors {
            Logger.database.error("  Detail: \(detail.localizedDescription, privacy: .public)")
        }
    }
    ErrorReporter.shared.recordNonFatal(error, context: [
        "entity": entityName,
        "operation": "save",
        "errorCode": nsError.code
    ])
    context.rollback()
}
```
