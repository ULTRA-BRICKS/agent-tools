# Thread Safety & Lock Selection

## How to Use This Reference

Read this when protecting shared mutable state, selecting a lock type, implementing reader-writer patterns, or reviewing code for data race safety.

---

## Lock Selection Hierarchy

| Lock | Relative Speed | Use Case | Notes |
|------|---------------|----------|--------|
| `os_unfair_lock` | 1x (fastest) | Short critical sections | **Must heap-allocate in Swift** |
| `OSAllocatedUnfairLock` | ~1x | iOS 16+, safe wrapper | Recommended by Apple |
| `Mutex` (Synchronization) | ~1x | iOS 18+ (Swift 6) | Standard library |
| `pthread_mutex_t` | ~1.4x | Portable code | C API, verbose |
| `NSLock` | ~1.5-2x | General purpose, any iOS | Recommended by Apple DTS |
| `DispatchQueue.sync` | ~7-8x | Reader-writer with barriers | Async support, higher overhead |
| `DispatchSemaphore` | Similar to unfair lock | Rate-limiting only | **No priority donation** |

**Decision:**

1. Need iOS 16+? → `OSAllocatedUnfairLock`
2. Need iOS 18+ / Swift 6? → `Mutex`
3. Need any iOS version? → `NSLock`
4. Need concurrent reads, exclusive writes? → `DispatchQueue` with barriers
5. Need rate-limiting (NOT mutual exclusion)? → `DispatchSemaphore`

---

## OSAllocatedUnfairLock (iOS 16+)

Apple's safe wrapper — handles heap allocation automatically:

```swift
final class ThreadSafeCache<Key: Hashable, Value> {
    private let lock = OSAllocatedUnfairLock(initialState: [Key: Value]())

    func value(for key: Key) -> Value? {
        lock.withLock { $0[key] }
    }

    func setValue(_ value: Value, for key: Key) {
        lock.withLock { $0[key] = value }
    }
}
```

---

## NSLock (Any iOS)

Safe, simple, no deployment target requirements:

```swift
final class ThreadSafeCache<Key: Hashable, Value> {
    private let lock = NSLock()
    private var storage = [Key: Value]()

    func value(for key: Key) -> Value? {
        lock.withLock { storage[key] }
    }

    func setValue(_ value: Value, for key: Key) {
        lock.withLock { storage[key] = value }
    }
}
```

---

## os_unfair_lock: The Memory Corruption Trap

**`os_unfair_lock` must NOT be a direct Swift stored property.** Swift can move value types in memory, corrupting the lock's internal state. Even Alamofire had this bug.

```swift
// CRASH — Swift may relocate this in memory
final class BadCache {
    private var lock = os_unfair_lock()  // Value type, can be moved
    // ...
}

// PREFERRED FIX: Use OSAllocatedUnfairLock (iOS 16+) or NSLock (any iOS)
// See sections above — they are safe by construction.

// LEGACY FIX (pre-iOS 16 only): Heap-allocate manually
final class SafeCache {
    private let lock = UnsafeMutablePointer<os_unfair_lock>.allocate(capacity: 1)

    init() { lock.initialize(to: os_unfair_lock()) }
    deinit { lock.deinitialize(count: 1); lock.deallocate() }

    func withLock<T>(_ body: () throws -> T) rethrows -> T {
        os_unfair_lock_lock(lock)
        defer { os_unfair_lock_unlock(lock) }
        return try body()
    }
}
```

---

## Reader-Writer Pattern with Barriers

Concurrent reads via `sync`, exclusive writes via `async(flags: .barrier)`. **Barriers on global queues are silently ignored** — no compiler warning, no runtime error.

**`DispatchQueue.global()` identity hazard:** `DispatchQueue.global(.background)` is NOT guaranteed to return the same queue object on every call — the system may return different backing queue instances. This means you cannot safely use a `global()` queue as the identity key for a barrier: two barrier calls intended for the same logical queue may land on different objects and execute concurrently. **Always use a custom named concurrent queue for barriers.**

```swift
final class ThreadSafeArray<Element> {
    // MUST be custom concurrent queue — barriers are ignored on global queues
    private let queue = DispatchQueue(
        label: "com.app.threadsafe.array",
        attributes: .concurrent
    )
    private var storage = [Element]()

    // Concurrent reads — multiple threads can read simultaneously
    var all: [Element] {
        queue.sync { storage }
    }

    func element(at index: Int) -> Element {
        queue.sync { storage[index] }
    }

    // Exclusive writes — barrier waits for all reads to finish
    func append(_ element: Element) {
        queue.async(flags: .barrier) { [self] in
            storage.append(element)
        }
    }

    func remove(at index: Int) {
        queue.async(flags: .barrier) { [self] in
            storage.remove(at: index)
        }
    }
}
```

**When to use:** When reads vastly outnumber writes and profiling shows read contention with a simple lock.

**When NOT to use:** For most cases, `NSLock` is simpler and fast enough. SDWebImage switched from barrier queues to locks for simplicity — barriers are correct but add complexity. Only use barriers when profiling confirms read contention is a bottleneck.

---

## The @Atomic Trap

`@Atomic` property wrappers protect individual get/set, but **compound operations are NOT atomic**:

```swift
@Atomic var count = 0

// NOT SAFE — this is two operations: read, then write
count += 1  // Equivalent to: temp = count; count = temp + 1

// SAFE — use the lock directly for compound operations
_count.withLock { $0 += 1 }
```

**Rule:** Never use `@Atomic` for counters, flags that are read-modify-write, or any compound operation. Use a lock directly.

---

## Thread-Safe Singleton

```swift
final class ServiceManager {
    // dispatch_once equivalent — guaranteed thread-safe by Swift
    static let shared = ServiceManager()
    private init() {}
}
```

Swift guarantees `static let` initialization is thread-safe (uses `dispatch_once` internally). No additional synchronization needed for the singleton itself. You still need synchronization for the singleton's mutable state.

---

## Actor-like Serial Queue Pattern

For pre-Swift Concurrency code that needs actor-like isolation:

```swift
final class AccountManager {
    private let queue = DispatchQueue(label: "com.app.accountManager")
    private var balance: Decimal = 0

    func deposit(_ amount: Decimal, completion: @escaping (Decimal) -> Void) {
        queue.async { [self] in
            balance += amount
            let current = balance
            DispatchQueue.main.async { completion(current) }
        }
    }

    func withdraw(_ amount: Decimal, completion: @escaping (Result<Decimal, Error>) -> Void) {
        queue.async { [self] in
            guard balance >= amount else {
                DispatchQueue.main.async { completion(.failure(InsufficientFundsError())) }
                return
            }
            balance -= amount
            let current = balance
            DispatchQueue.main.async { completion(.success(current)) }
        }
    }
}
```

All mutable state is accessed only on `queue`. External callers interact via async methods with completion handlers. This is the GCD equivalent of an actor.

---

## Sendable Marking

When preparing code for Swift 6 strict concurrency:

```swift
// Value types with value-type stored properties are implicitly Sendable
struct UserID: Sendable {
    let value: UUID
}

// Classes must be final + use internal synchronization
final class ThreadSafeCache: @unchecked Sendable {
    private let lock = NSLock()
    private var storage: [String: Any] = [:]

    func get(_ key: String) -> Any? {
        lock.withLock { storage[key] }
    }

    func set(_ key: String, value: Any) {
        lock.withLock { storage[key] = value }
    }
}
```

**`@unchecked Sendable`** tells the compiler "I've manually ensured thread safety." Use only when you have synchronization in place. The compiler trusts you — get it wrong and you get data races.
