# Sendable & Data Transfer

## How to Use This Reference

Read this when dealing with Sendable conformance errors, choosing between `sending`, `@unchecked Sendable`, and actor wrapping, or understanding how the compiler tracks value regions across isolation boundaries.

---

## Prefer `sending` Over @unchecked Sendable 🟠

SE-0430's `sending` keyword allows non-Sendable values to cross isolation boundaries safely when the compiler can prove the value is in a "disconnected region" — no other code holds a reference. `Task.init`, `CheckedContinuation.resume(returning:)`, and many concurrency APIs now use `sending` parameters.

```swift
// ANTI-PATTERN -- @unchecked Sendable to pass non-Sendable type
final class RequestBuilder: @unchecked Sendable { // No synchronization!
    var headers: [String: String] = [:]
}

// FIX -- use sending parameter (compiler verifies safety)
func submitRequest(_ builder: sending RequestBuilder) async {
    // Compiler ensures caller cannot use builder after this call
    let request = builder.build()
    await network.send(request)
}
```

**Key:** `sending` provides compile-time safety without requiring Sendable conformance. The compiler proves the value is uniquely owned at the transfer point.

---

## Region-Based Isolation Reduces Sendable Requirements 🟢

SE-0414 introduced region-based isolation tracking. The compiler tracks "regions" of values that might reference each other. A freshly created non-Sendable value that is not used after being passed across isolation is valid without Sendable conformance.

```swift
// This works WITHOUT Sendable conformance in Swift 6
class ImageProcessor { /* non-Sendable */ }

@MainActor func processImage() async {
    let processor = ImageProcessor()        // Created here
    await backgroundActor.run(processor)    // Passed to another isolation domain
    // processor is NOT used after this line -- compiler proves safety
}
```

**Practical impact:** Don't reflexively add Sendable to every class. The compiler's flow analysis may already prove safety. Add Sendable only when the compiler requires it.

---

## @unchecked Sendable Hides Runtime Crashes 🔴

`@unchecked Sendable` suppresses compile-time checks but does NOT suppress runtime actor isolation assertions. A closure created in `@MainActor` context and stored in an `@unchecked Sendable` box will still crash with `dispatch_assert_queue_fail` when called from a background context.

```swift
// CRASH -- @unchecked Sendable hides the problem, runtime catches it
final class CallbackBox: @unchecked Sendable {
    let closure: () -> Void // Captured in @MainActor context
}

@MainActor func setup() {
    let box = CallbackBox(closure: { updateUI() }) // Captures MainActor context
    Task.detached {
        box.closure() // Runtime crash: dispatch_assert_queue_fail
    }
}
```

**Three acceptable uses of @unchecked Sendable:**

| Case | Justification | Requirement |
|------|--------------|-------------|
| Type uses `Mutex` or `NSLock` for ALL mutable state | Synchronization is external to the type system | Document the lock discipline |
| Immutable reference type (all `let` properties of Sendable types) | No mutation possible | Compiler should infer this; file a bug if it doesn't |
| C/ObjC interop type that is known thread-safe | Type comes from C header | Add a comment explaining thread safety |

Every other use is a latent data race. Require code review approval for every `@unchecked Sendable`.

---

## @MainActor Closures Are Implicitly Sendable 🟢

In Swift 6, `@MainActor` closures are safely transferable across isolation domains because they're guaranteed to run on the MainActor. The combination `@MainActor @Sendable () -> Void` is redundant.

```swift
// REDUNDANT in Swift 6
func register(callback: @MainActor @Sendable () -> Void) { /* ... */ }

// SUFFICIENT in Swift 6
func register(callback: @MainActor () -> Void) { /* ... */ }
```

**Note:** Add explicit `@Sendable` only if you need Swift 5 backward compatibility.

---

## Swift Does Not Infer Sendable for Public Types 🟢

Public structs and enums do NOT get automatic Sendable inference even if all stored properties are Sendable. You must declare conformance explicitly because Sendable is a **public API contract**. Internal types get automatic inference.

```swift
// Internal -- compiler infers Sendable automatically
struct InternalConfig {
    let timeout: TimeInterval
    let retryCount: Int
}

// Public -- NO automatic inference, must be explicit
public struct PublicConfig: Sendable {
    public let timeout: TimeInterval
    public let retryCount: Int
}
```

**Action:** After making a type public, always check if it should conform to Sendable. Forgetting this is the #1 source of Sendable errors when extracting code into Swift packages.

---

## @preconcurrency import Applies to the Entire File 🟠

`@preconcurrency import SomeModule` suppresses Sendable warnings for **ALL** uses of that module's types in the file. While addressing one legitimate issue, you may paper over actual data race bugs elsewhere in the same file.

```swift
// DANGEROUS -- suppresses ALL Sendable warnings for Firebase types in this file
@preconcurrency import FirebaseAuth

func authenticate() async {
    let user = Auth.auth().currentUser  // No warnings, even if unsafe
    Task { process(user) }              // Potential race -- silenced
}
```

**Mitigation:**
- Keep files with `@preconcurrency` imports **small and focused** — one responsibility per file
- Add a comment explaining which specific type requires the suppression
- Log a task in `refactoring/discovered.md` to remove when the SDK updates
- Periodically remove the annotation and check if the SDK has been updated

---

## Sendable Decision Matrix

```
Is the type a value type (struct/enum)?
+-- YES -> Are all stored properties Sendable?
|   +-- YES -> Is the type public?
|   |   +-- YES -> Add explicit `: Sendable` conformance
|   |   +-- NO  -> Automatic inference (nothing to do)
|   +-- NO  -> Can the non-Sendable property be replaced?
|       +-- YES -> Replace with Sendable equivalent
|       +-- NO  -> Keep type non-Sendable, contain in single domain
+-- NO (class) -> Does it need to cross isolation boundaries?
    +-- NO  -> Keep non-Sendable (simplest, safest)
    +-- YES -> Is all state protected by a lock?
        +-- YES -> @unchecked Sendable with documented lock discipline
        +-- NO  -> Convert to actor, or use Mutex<State>, or redesign as struct
```

**Priority order for fixing Sendable errors:**
1. Use `sending` parameter (if value is uniquely owned at transfer)
2. Redesign as value type (struct/enum)
3. Use `Mutex<State>` wrapper (synchronous access)
4. Convert to actor (async access)
5. `@preconcurrency import` (temporary bridge for third-party)
6. `@unchecked Sendable` (last resort, documented, code-reviewed)
