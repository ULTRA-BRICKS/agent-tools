# GCD to Swift Concurrency Migration

## How to Use This Reference

Read this when deciding whether to migrate GCD patterns to Swift Concurrency, mapping specific patterns to their async/await equivalents, or bridging GCD code with existing async/await code.

---

## Migration Categories

### Easy to Migrate

| GCD Pattern | Swift Concurrency Equivalent |
|-------------|------------------------------|
| Serial queue for isolation | `actor` |
| `DispatchQueue.main.async` | `@MainActor` or `MainActor.run` |
| `DispatchGroup` (wait for N tasks) | `async let` or `TaskGroup` |
| Callback-based async function | `withCheckedContinuation` / `withCheckedThrowingContinuation` |
| `DispatchQueue.global().async` | `Task { }` or `Task.detached { }` |

### Hard to Migrate (Requires Careful Design)

| GCD Pattern | Challenge |
|-------------|-----------|
| Reader-writer with barriers | No direct equivalent — use `actor` with careful design |
| `OperationQueue` dependencies | `TaskGroup` doesn't support inter-task dependencies directly |
| `DispatchSource` timers | `AsyncTimerSequence` (iOS 16+), or keep GCD |
| `maxConcurrentOperationCount` | `TaskGroup` has no built-in throttle — need custom implementation |

### Keep as GCD (No Migration Path)

| GCD Pattern | Why |
|-------------|-----|
| `DispatchIO` | No async equivalent exists |
| `DispatchSource` (non-timer) | File monitoring, signal handling — no async equivalent |
| `DispatchQueue(attributes: .initiallyInactive)` | No async equivalent |
| Target queue hierarchies | Actors don't support hierarchy-based thread funneling |
| Real-time audio/video processing | Cooperative thread pool is wrong for real-time guarantees |

---

## Pattern-by-Pattern Migration

### Serial Queue → Actor

```swift
// BEFORE: Serial queue for isolation
final class AccountManager {
    private let queue = DispatchQueue(label: "com.app.account")
    private var balance: Decimal = 0

    func deposit(_ amount: Decimal, completion: @escaping (Decimal) -> Void) {
        queue.async { [self] in
            balance += amount
            DispatchQueue.main.async { completion(balance) }
        }
    }
}

// AFTER: Actor
actor AccountManager {
    private var balance: Decimal = 0

    func deposit(_ amount: Decimal) -> Decimal {
        balance += amount
        return balance
    }
}
// Usage: let newBalance = await accountManager.deposit(100)
```

### DispatchGroup → TaskGroup

```swift
// BEFORE: DispatchGroup
let group = DispatchGroup()
var users: [User]?
var products: [Product]?

group.enter()
fetchUsers { result in
    users = result
    group.leave()
}

group.enter()
fetchProducts { result in
    products = result
    group.leave()
}

group.notify(queue: .main) {
    updateUI(users: users!, products: products!)
}

// AFTER: async let (both start immediately, run concurrently)
async let users = fetchUsers()
async let products = fetchProducts()
// await only when results are needed — both are already running
try await updateUI(users: users, products: products)

// Or with TaskGroup for dynamic number of tasks
await withTaskGroup(of: FetchResult.self) { group in
    for url in urls {
        group.addTask { await fetch(url) }
    }
    for await result in group {
        process(result)
    }
}
```

### Callback → Continuation

```swift
// BEFORE: Callback-based API
func fetchUser(id: String, completion: @escaping (Result<User, Error>) -> Void) {
    URLSession.shared.dataTask(with: url) { data, _, error in
        if let error { completion(.failure(error)); return }
        let user = try! JSONDecoder().decode(User.self, from: data!)
        completion(.success(user))
    }.resume()
}

// AFTER: Wrapped with continuation
func fetchUser(id: String) async throws -> User {
    try await withCheckedThrowingContinuation { continuation in
        fetchUser(id: id) { result in
            continuation.resume(with: result)
        }
    }
}
```

---

## Critical Bugs to Avoid

### 1. Semaphore in Async Context

```swift
// BUG: Blocks cooperative thread pool — can deadlock entire async system
func fetchData() async -> Data {
    let semaphore = DispatchSemaphore(value: 0)
    var result: Data!
    URLSession.shared.dataTask(with: url) { data, _, _ in
        result = data
        semaphore.signal()
    }.resume()
    semaphore.wait()  // NEVER do this in async context
    return result
}

// FIX: Use continuation
func fetchData() async -> Data {
    await withCheckedContinuation { continuation in
        URLSession.shared.dataTask(with: url) { data, _, _ in
            continuation.resume(returning: data!)
        }.resume()
    }
}
```

### 2. Actor Reentrancy

```swift
actor ImageCache {
    private var cache: [URL: UIImage] = [:]

    func image(for url: URL) async -> UIImage {
        if let cached = cache[url] { return cached }

        // SUSPENSION POINT — another caller can enter here
        let image = await downloadImage(url)

        // cache[url] might already be set by another caller!
        cache[url] = image  // Duplicate work, but safe (actor protects dictionary)
        return image
    }
}
```

Actors allow re-entry at every `await`. State may change between suspension and resumption. Always re-check state after `await`.

### 3. Continuation Must Be Resumed Exactly Once

```swift
// BUG: Continuation never resumed on error path — hangs forever
func fetchData() async throws -> Data {
    try await withCheckedThrowingContinuation { continuation in
        fetchData { result in
            switch result {
            case .success(let data):
                continuation.resume(returning: data)
            case .failure:
                break  // BUG: Continuation leaked — caller awaits forever
            }
        }
    }
}

// FIX: Resume on every path
func fetchData() async throws -> Data {
    try await withCheckedThrowingContinuation { continuation in
        fetchData { result in
            continuation.resume(with: result)  // Handles both cases
        }
    }
}
```

**Rules:**

- `withCheckedContinuation` crashes if resumed twice (good — catches bugs)
- `withUnsafeContinuation` is undefined behavior if resumed twice (bad — silent corruption)
- Always use `withChecked*` unless profiling proves the check is a bottleneck

---

## When NOT to Migrate

1. **Working GCD code that's well-tested** — migration introduces risk for no user benefit
2. **Real-time audio/video** — cooperative thread pool has unpredictable scheduling
3. **DispatchIO / DispatchSource** — no async equivalent
4. **Code that needs `maxConcurrentOperationCount`** — TaskGroup has no built-in throttle
5. **Target queue hierarchies** — actors don't support this architecture pattern
6. **Third-party libraries that use GCD internally** — don't wrap their callbacks in continuations unless you need to compose with async code

---

## Coexistence: Alamofire Pattern

Alamofire demonstrates the best practice for gradual migration — async wrappers around GCD internals:

```swift
// Internal: Still uses GCD serial queue for request management
final class Session {
    let rootQueue = DispatchQueue(label: "com.alamofire.session.rootQueue")
    // ...
}

// External: Provides async/await API
extension Session {
    func data(from url: URL) async throws -> Data {
        try await withCheckedThrowingContinuation { continuation in
            request(url).responseData { response in
                continuation.resume(with: response.result)
            }
        }
    }
}
```

**The pattern:** Keep battle-tested GCD internals. Expose modern async/await API. Migrate internals only when there's a clear benefit (simpler code, better cancellation, reduced complexity).

---

## Migration Checklist

Before migrating a GCD pattern:

- [ ] Is there a direct Swift Concurrency equivalent?
- [ ] Will migration simplify the code (not just change syntax)?
- [ ] Are there tests covering the concurrent behavior?
- [ ] Have you checked for semaphore/lock usage in the async path?
- [ ] Is the minimum deployment target iOS 13+ (basic async) or iOS 15+ (TaskGroup)?
- [ ] Are all continuations resumed exactly once on every code path?
