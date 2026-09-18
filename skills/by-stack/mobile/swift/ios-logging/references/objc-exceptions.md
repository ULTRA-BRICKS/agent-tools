# Objective-C Exceptions and Swift Error Handling

Swift's `do/catch` only catches Swift `Error` types. **Objective-C `NSException` instances are NOT caught by Swift's error handling** — they terminate the app without Swift having any chance to intervene.

## Error Type Coverage

| Error type | Swift do/catch | NSExceptionHandler | Signal handler | MetricKit |
|---|:---:|:---:|:---:|:---:|
| Swift `Error` (throws) | Yes | No | No | No |
| ObjC `NSException` | **No** | Yes | No | Yes |
| Swift trap (force unwrap nil) | No | No | Yes (SIGTRAP) | Yes |
| EXC_BAD_ACCESS | No | No | Yes (SIGSEGV) | Yes |

## Common NSException Triggers

These Objective-C APIs throw NSExceptions that Swift cannot catch:

- **NSArray/NSDictionary** out-of-bounds or nil key access
- **Invalid KVO key paths** (typo in `#keyPath` string or unregistered observer)
- **Unrecognized selectors** (calling a method that doesn't exist on an object)
- **Core Data threading violations** (accessing managed objects from wrong thread)
- **NSInvalidArgumentException** (wrong argument types to ObjC methods)
- **Storyboard/Nib loading** (missing outlet connections, wrong class names)

## Defensive Pattern

Validate inputs before calling ObjC APIs rather than trying to catch exceptions:

```swift
// ❌ BAD — Swift do/catch will NOT catch NSRangeException
do {
    let item = nsArray.object(at: 999) // NSRangeException → crash
} catch {
    // This catch block NEVER executes for NSException
}

// ✅ GOOD — Validate before calling
guard index < nsArray.count else {
    Logger.database.error("Index \(index, privacy: .public) out of bounds for array of \(nsArray.count, privacy: .public)")
    return nil
}
let item = nsArray.object(at: index)
```

## Swift-ObjC Bridge Error Behavior

When bridging Swift `throws` to ObjC (`@objc func doWork() throws`), ObjC determines success/failure by the **return value** (nil = failure), not by whether `NSError` was set. If an ObjC method returns non-nil AND sets an NSError, **Swift's catch block is never invoked** — the error is silently lost.

```swift
// ObjC sees: non-nil return = success, ignores NSError
// Swift catch block never fires even though an error occurred
```

## NSSetUncaughtExceptionHandler

Last-resort safety net, but runs in a dying process with severe restrictions:
- Must be async-signal-safe (no ObjC/Swift allocation, no network calls)
- Only one handler is supported — second call replaces the first
- Crash reporting SDKs already install this handler

Crash reporting SDKs layer three mechanisms:
1. `NSSetUncaughtExceptionHandler` for ObjC exceptions
2. UNIX signal handlers (`sigaction`) for Swift traps and memory violations
3. Mach exception handlers for low-level machine exceptions

Don't install your own handler if you're using a crash reporting SDK — you'll interfere with it.
