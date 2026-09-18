# Security Patterns in Concurrent Code

## How to Use This Reference

Read this when dealing with authentication tokens, Keychain access, payment processing, or any security-sensitive operations in async code. Every `await` is a potential window for state corruption in security-critical paths.

---

## Serialize Token Refresh to Prevent Auth Cascade 🟠

When multiple concurrent requests encounter an expired token, each independently triggering a refresh can cause cascade failures. OAuth servers that invalidate previous tokens on issuance mean the first refresh succeeds, but subsequent ones fail — causing a cascade of 401s.

```swift
// BUG -- multiple concurrent refreshes, race condition
class AuthManager {
    func validToken() async throws -> String {
        if isExpired(currentToken) {
            currentToken = try await refreshToken() // 3 callers = 3 refreshes
        }
        return currentToken
    }
}

// FIX -- actor with coalesced refresh task
actor AuthManager {
    private var currentToken: String?
    private var refreshTask: Task<String, Error>?

    func validToken() async throws -> String {
        if let token = currentToken, !isExpired(token) { return token }
        if let task = refreshTask { return try await task.value } // Join existing
        let task = Task { try await performRefresh() }
        refreshTask = task
        do {
            let token = try await task.value
            currentToken = token
            refreshTask = nil
            return token
        } catch {
            refreshTask = nil
            throw error
        }
    }
}
```

**Key pattern:** Store the in-flight Task synchronously (before the first `await`) so subsequent callers see it and join, rather than starting their own refresh.

---

## Every await Is a TOCTOU Window 🟠

Any `await` between a permission check and its use creates a Time-of-Check to Time-of-Use (TOCTOU) vulnerability. Actors are re-entrant, so state can change between `await` calls within the same actor method.

```swift
// VULNERABLE -- check-await-act pattern
actor SecureStore {
    private var permissions: Set<Permission> = []
    private var data: [String: Data] = [:]

    func readData(key: String, requester: User) async throws -> Data {
        guard permissions.contains(.read(user: requester)) else {
            throw PermissionDenied()
        }
        // REENTRANCY WINDOW -- permissions could be revoked here
        let decrypted = await cryptoService.decrypt(data[key]!)
        return decrypted // May return data to a now-unauthorized user
    }
}

// FIX -- combine check-and-use into non-suspending section, or re-check after await
actor SecureStore {
    private var permissions: Set<Permission> = []
    private var data: [String: Data] = [:]

    func readData(key: String, requester: User) async throws -> Data {
        guard permissions.contains(.read(user: requester)) else {
            throw PermissionDenied()
        }
        let encrypted = data[key]! // Read synchronously (no reentrancy)
        let decrypted = await cryptoService.decrypt(encrypted)
        // Re-check after await
        guard permissions.contains(.read(user: requester)) else {
            throw PermissionDenied()
        }
        return decrypted
    }
}
```

**For file operations:** Use file descriptors (not paths) and avoid separate `FileManager.fileExists()` → `open()` patterns. The file can be replaced between the two calls.

---

## Keychain Access Needs Serialization 🟠

Apple's Keychain Services (`SecItem*`) is thread-safe for individual atomic operations, but multi-step read-modify-write sequences are vulnerable to race conditions. The popular `keychain-swift` library had documented `EXC_BAD_ACCESS` crashes from concurrent access.

```swift
// RACE CONDITION -- two callers read-modify-write simultaneously
func updateKeychainPassword(newPassword: String) {
    let existing = readKeychainItem(key: "password")  // Thread 1: reads old
    // Thread 2 also reads old value here
    let updated = transform(existing, newPassword)
    writeKeychainItem(key: "password", value: updated) // Both write, last wins
}

// FIX -- actor-based Keychain wrapper
actor KeychainActor {
    func readItem(key: String) -> Data? {
        // SecItemCopyMatching -- safe, runs within actor isolation
        var query = baseQuery(key: key)
        query[kSecReturnData as String] = true
        var result: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        return status == errSecSuccess ? result as? Data : nil
    }

    func writeItem(key: String, value: Data) {
        // Delete + Add is atomic within actor isolation
        deleteItem(key: key)
        var query = baseQuery(key: key)
        query[kSecValueData as String] = value
        SecItemAdd(query as CFDictionary, nil)
    }
}
```

---

## Never @unchecked Sendable on Sensitive Data Types 🟠

`@unchecked Sendable` on types carrying tokens, PII, or encryption keys completely disables the compiler's data race detection. A data race on a password field could expose partially-written or stale values.

```swift
// DANGEROUS -- sensitive data with no synchronization
final class UserCredentials: @unchecked Sendable {
    var accessToken: String = ""    // Data race: partial write visible
    var refreshToken: String = ""   // Data race: stale value used
    var encryptionKey: Data = Data() // Data race: corrupted key material
}

// FIX -- actor containment, access via methods only
actor CredentialStore {
    private var accessToken: String = ""
    private var refreshToken: String = ""
    private var encryptionKey: Data = Data()

    func currentAccessToken() -> String { accessToken }
    func update(access: String, refresh: String) {
        accessToken = access
        refreshToken = refresh
    }
}
```

**Policy:** Require code review approval for every `@unchecked Sendable` usage. Types holding tokens, PII, or cryptographic material must NEVER use it.

---

## Actor Reentrancy Enables Double-Spend 🟠

An actor method that checks a balance, then `await`s a network call, then deducts can be re-entered. Two concurrent calls both pass the check — the balance goes negative.

```swift
// DOUBLE-SPEND -- two concurrent withdrawals both succeed
actor PaymentActor {
    var balance: Decimal = 100
    func withdraw(_ amount: Decimal) async throws {
        guard balance >= amount else { throw InsufficientFunds() }
        let receipt = await gateway.charge(amount) // Reentrancy window
        balance -= amount // Both calls deduct
    }
}

// FIX -- pessimistic reservation before await
actor PaymentActor {
    var balance: Decimal = 100
    private var reserved: Decimal = 0

    func withdraw(_ amount: Decimal) async throws {
        let available = balance - reserved
        guard available >= amount else { throw InsufficientFunds() }
        reserved += amount // Reserve synchronously before await
        defer { reserved -= amount }
        let receipt = await gateway.charge(amount)
        guard receipt.success else { throw PaymentFailed() }
        balance -= amount
    }
}
```

**Key:** The reservation happens **synchronously** (before any `await`), so concurrent callers see the reserved amount immediately.

---

## URLSession Can Send Duplicate POST Requests 🟠

`URLSession` does not deduplicate in-flight requests by default. Shared instances used across concurrent tasks without coordination can send duplicate state-mutating requests (POST/PUT/DELETE).

```swift
// BUG -- user double-taps "Purchase", two POST requests sent
func purchase(itemID: String) async throws -> Receipt {
    let request = makePurchaseRequest(itemID: itemID)
    let (data, _) = try await URLSession.shared.data(for: request)
    return try JSONDecoder().decode(Receipt.self, from: data)
}

// FIX -- actor-based request deduplication
actor RequestDeduplicator {
    private var inFlight: [String: Task<Data, Error>] = [:]

    func deduplicated(key: String, request: URLRequest) async throws -> Data {
        if let task = inFlight[key] { return try await task.value }
        let task = Task {
            let (data, _) = try await URLSession.shared.data(for: request)
            return data
        }
        inFlight[key] = task
        defer { inFlight[key] = nil }
        return try await task.value
    }
}
```

**Use the request key** (e.g., `"purchase-\(itemID)"`) to identify logically identical requests. Different item IDs should proceed concurrently.

---

## Security Checklist for Concurrent Code

- [ ] Token refresh serialized — no concurrent refresh attempts
- [ ] No TOCTOU between permission check and state mutation (re-check after await)
- [ ] Keychain operations serialized via actor or lock
- [ ] No `@unchecked Sendable` on types holding tokens, PII, or crypto material
- [ ] Financial/balance operations use pessimistic reservation before `await`
- [ ] State-mutating HTTP requests (POST/PUT/DELETE) deduplicated
- [ ] Sensitive data types are non-Sendable by design, contained in actors
- [ ] Error messages from concurrent operations do not leak internal state
- [ ] Cancellation of security operations cleans up partial state (no half-written credentials)
