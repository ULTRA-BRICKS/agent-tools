# Core Data and Swift Concurrency

Thread-safe patterns for using Core Data with Swift Concurrency.

## Core Principles

Core Data's thread safety rules don't change with Swift Concurrency:
- **Cannot pass `NSManagedObject` between threads**
- **Must access objects on their context's thread**
- **`NSManagedObjectID` is thread-safe** (can pass around)

## NSManagedObject Cannot Be Sendable

```swift
@objc(Article)
public class Article: NSManagedObject {
    @NSManaged public var title: String  // Mutable, can't be Sendable
}

// DON'T: Silence warnings without fixing safety
extension Article: @unchecked Sendable {}  // 🔴 DANGEROUS
```

## Pattern 1: Data Access Objects (DAO)

Thread-safe value types representing managed objects:

```swift
// Managed object (NOT Sendable)
@objc(Article)
public class Article: NSManagedObject {
    @NSManaged public var title: String?
    @NSManaged public var timestamp: Date?
}

// DAO (Sendable) - safe to pass across isolation boundaries
struct ArticleDAO: Sendable, Identifiable {
    let id: NSManagedObjectID
    let title: String
    let timestamp: Date

    init?(managedObject: Article) {
        guard let title = managedObject.title,
              let timestamp = managedObject.timestamp else {
            return nil
        }
        self.id = managedObject.objectID
        self.title = title
        self.timestamp = timestamp
    }
}
```

**Benefits**: Sendable, immutable, clear data transfer.
**Drawbacks**: Boilerplate, requires rewrite of fetch/mutation logic.

## Pattern 2: Pass Only NSManagedObjectID

```swift
actor ArticleStore {
    private let context: NSManagedObjectContext

    func fetchArticleIDs() async throws -> [NSManagedObjectID] {
        try await context.perform {
            let request = Article.fetchRequest()
            return try context.fetch(request).map(\.objectID)
        }
    }

    func article(for objectID: NSManagedObjectID) async throws -> ArticleDAO? {
        try await context.perform {
            guard let article = try? context.existingObject(with: objectID) as? Article else {
                return nil
            }
            return ArticleDAO(managedObject: article)
        }
    }
}
```

## Pattern 3: Actor-Isolated Context

```swift
actor CoreDataStack {
    let container: NSPersistentContainer

    init(modelName: String) {
        container = NSPersistentContainer(name: modelName)
    }

    func loadStores() async throws {
        try await withCheckedThrowingContinuation { continuation in
            container.loadPersistentStores { _, error in
                if let error {
                    continuation.resume(throwing: error)
                } else {
                    continuation.resume()
                }
            }
        }
    }

    func perform<T: Sendable>(_ block: @escaping (NSManagedObjectContext) throws -> T) async rethrows -> T {
        try await container.viewContext.perform {
            try block(container.viewContext)
        }
    }
}
```

## @MainActor Conflict

If your app uses `@MainActor` default isolation (Swift 6.2), beware:

```swift
// Problem: @MainActor conflicts with Core Data's thread requirements
@MainActor
class ArticleViewModel: ObservableObject {
    @Published var articles: [ArticleDAO] = []

    func load() async {
        // viewContext.perform already ensures main thread
        // @MainActor + perform can cause double-dispatch
    }
}
```

**Solution**: Use a dedicated actor for Core Data, not `@MainActor`:

```swift
actor ArticleRepository {
    private let context: NSManagedObjectContext

    func fetchAll() async throws -> [ArticleDAO] {
        try await context.perform {
            let request = Article.fetchRequest()
            return try context.fetch(request).compactMap { ArticleDAO(managedObject: $0) }
        }
    }
}

@MainActor
class ArticleViewModel: ObservableObject {
    @Published var articles: [ArticleDAO] = []
    private let repository = ArticleRepository()

    func load() async {
        articles = try? await repository.fetchAll() ?? []
    }
}
```

## Async Context.perform

iOS 15+ provides async `perform`:

```swift
extension NSManagedObjectContext {
    func perform<T>(schedule: ScheduledTaskType = .immediate, _ block: @escaping () throws -> T) async rethrows -> T
}

// Usage
let articles = try await context.perform {
    let request = Article.fetchRequest()
    return try context.fetch(request)
}
```

## What's Missing (Manual Bridge Needed)

No async alternative for `loadPersistentStores`:

```swift
func loadStores() async throws {
    try await withCheckedThrowingContinuation { continuation in
        container.loadPersistentStores { _, error in
            if let error {
                continuation.resume(throwing: error)
            } else {
                continuation.resume()
            }
        }
    }
}
```

## Checklist

- [ ] Never pass `NSManagedObject` across isolation boundaries
- [ ] Use DAO pattern or `NSManagedObjectID` for cross-actor data
- [ ] Keep Core Data access in dedicated actor, not `@MainActor`
- [ ] Use async `context.perform` for all context access
- [ ] Bridge `loadPersistentStores` with continuation
- [ ] Test with Thread Sanitizer enabled
