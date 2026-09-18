# Compiler Diagnostics → Fix Mapping

Maps common strict-concurrency compiler errors to likely fixes. Try fixes in order listed.

## "Sending 'x' risks causing data races"

The compiler found a value crossing an isolation boundary where it could still be accessed from the sending side.

**Fixes (try in order):**

1. **Check if region-based isolation handles it** — If the sender stops using the value after passing it, the compiler may accept it. Avoid adding `Sendable` prematurely.
2. **Mark the parameter `sending`** — Tells the compiler the caller transfers ownership.
3. **Make the type `Sendable`** — If genuinely thread-safe (value type, immutable class, or internally synchronized).
4. **Use `nonisolated(nonsending)` (Swift 6.2)** — If the function no longer hops executors, the value may not cross a boundary.
5. **Last resort: `@unchecked Sendable`** — Only if type uses manual synchronization (locks) with verified correctness.

## "Static property 'x' is not concurrency-safe"

Global or static variable accessible from multiple isolation domains.

**Fixes:**

1. **Annotate with `@MainActor`** — `@MainActor static let shared = MyType()`
2. **Make Sendable** — If truly constant and immutable (`let`-only struct)
3. **Use `nonisolated(unsafe)`** — Only for genuinely immutable state where compiler can't prove safety

## "Capture of 'x' with non-sendable type in `@Sendable` closure"

Closure crossing isolation boundaries captures non-Sendable value.

**Fixes:**

1. **Make captured value Sendable** — Structs/enums with Sendable properties just need conformance
2. **Restructure to avoid capture** — Pass needed data as parameter: `let id = object.id; Task { use(id) }`
3. **Keep work on same actor** — If closure doesn't need concurrency
4. **Use `sending` parameter** — For clean ownership transfer

## "Main actor-isolated conformance cannot be used in nonisolated context"

Isolated conformance used from non-isolated code.

**Fixes:**

1. **Move use site onto same actor** — Make consuming code `@MainActor`
2. **Remove isolation from conformance** — If protocol methods don't need actor-protected state

## "Expression is 'async' but is not marked with 'await'"

Call crosses isolation boundary requiring async hop.

**Fix:** Add `await`. If in sync code that cannot be async, wrap in `Task {}`.

## "@preconcurrency conformance crashes at runtime" (Swift 6.2)

`@preconcurrency` is compile-time only. SE-0423 enforces runtime isolation checks.

**Fixes:**

1. **Make protocol method `async`** — Proper isolation boundary
2. **Add `@MainActor` to protocol** — If all conformers are UI-bound
3. **Remove `@MainActor` from type** — If protocol methods don't need it

## "Actor-isolated type does not conform to protocol"

Protocol and type describe different isolation boundaries.

**Fixes:**

| Requirement | Solution |
|-------------|----------|
| Type-level isolation incidental | Remove isolation |
| Conformance should be `@MainActor` only | `extension MyType: @MainActor Protocol {}` |

## Quick Diagnostic Triage

```
Error mentions "Sendable"?
├─ "risks causing data races" → Check region isolation, then sending, then Sendable
├─ "non-sendable capture" → Restructure to avoid capture, or make type Sendable
└─ "static property" → @MainActor or nonisolated(unsafe)

Error mentions "isolation"?
├─ "Main actor-isolated" → Move caller to @MainActor or use MainActor.run
├─ "Actor-isolated conformance" → Use isolated conformance or remove isolation
└─ "@preconcurrency crash" → Make protocol async or remove @MainActor

Error mentions "async"?
├─ "not marked with await" → Add await or wrap in Task
└─ "async_without_await lint" → Remove async if not needed, or suppress narrowly
```
