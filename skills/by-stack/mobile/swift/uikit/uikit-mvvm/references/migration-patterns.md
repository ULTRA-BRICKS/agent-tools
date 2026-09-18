# Migration Patterns for Production Codebases

## MVC → MVVM Incremental Migration

**Key principle**: Codebase will be hybrid MVC/MVVM for 6-12 months. Leave stable, rarely-touched screens in MVC.

**Steps per screen**: (1) Create ViewModel alongside VC, (2) Move business logic to VM, (3) Add binding layer, (4) Write VM tests, (5) Refactor VC to observe VM.

### Binding Evolution Path

```text
Phase 1: Closures / Bindable<T> → any iOS, no Combine knowledge needed
Phase 2: Combine @Published + sink → one-to-many, operators
Phase 3: async/await + Combine (iOS 15+) → cleaner async, keep Combine for UI binding
```

### What to Migrate First

| Priority | Criteria | Example |
| --- | --- | --- |
| 1st | Screens with MVC bugs | Cart with race conditions |
| 2nd | Screens under active dev | Feature being rebuilt |
| 3rd | Screens needing tests | Payment flow |
| Last | Stable, rarely-touched | About page |

## GCD → Combine Migration (Most Common Legacy Transition)

### Completion handler → @Published + sink

Service layer can stay GCD-based. ViewModel switches from closures to `@Published`. VC switches to `sink`.

```swift
// BEFORE: viewModel.onOrdersLoaded = { orders in ... }
// AFTER:  viewModel.$state.receive(on: .main).sink { [weak self] state in ... }.store(in: &cancellables)
```

### DispatchGroup → Publishers.Zip

```swift
// BEFORE: group.enter(); service.fetch { group.leave() }; group.notify { ... }
// AFTER:  Publishers.Zip(service1.publisher(), service2.publisher()).sink { ... }
```

### DispatchWorkItem debounce → Combine debounce

```swift
// BEFORE: workItem?.cancel(); asyncAfter(deadline: .now() + 0.3, execute: item)
// AFTER:  $searchQuery.debounce(for: .milliseconds(300), scheduler: .main).sink { ... }
```

### Serial queue → @Published (main-thread only, no manual sync)

### Wrapping completion handlers as Future publishers

```swift
extension NetworkService {
    func fetchPublisher() -> AnyPublisher<[Order], Error> {
        Future { [weak self] promise in self?.fetch { promise($0) } }.eraseToAnyPublisher()
    }
}
```

### GCD → async/await (iOS 15+)

| GCD | async/await |
| --- | --- |
| `DispatchQueue.main.async` | `@MainActor` or `MainActor.run` |
| `DispatchQueue.global().async` | plain `async` function |
| `DispatchGroup` | `TaskGroup` or `async let` |
| `DispatchSemaphore` | actors (don't block threads) |
| serial queue | `actor` |

**Never** `DispatchSemaphore.wait()` in async context — blocks cooperative pool.

### Migration Order

```text
Phase 1: Extract ViewModels → keep GCD in services, closures for VC binding
Phase 2: Combine for UI binding → @Published + sink, keep GCD in services
Phase 3: async/await in services (iOS 15+) → replace completion handlers, keep Combine for UI
```

## UIKit → SwiftUI Incremental Adoption

**UIHostingController** embeds SwiftUI in UIKit navigation. **Shared ViewModel** (`ObservableObject`) works with both: SwiftUI uses `@ObservedObject`, UIKit uses `$property.sink`.

**When to start**: iOS 15+ minimum. Start with leaf views (settings, forms), not core flows. Keep Coordinator layer in UIKit.

## Combine Adoption — When Worth It

**Worth it**: multiple subscribers, form validation chains, debounce search, wrapping delegate APIs.
**Not worth it**: simple one-shot callbacks, < 10K LOC, iOS 12 needed, team migrating to async/await already.

**Immediate wins**: NotificationCenter → `.publisher(for:)`, KVO → `.publisher(for: \.keyPath)`, Timer → `Timer.publish(every:)`.

## Source Consensus

**Universal**: MVVM most popular beyond MVC. Coordinator for navigation. DiffableDataSource + CompositionalLayout. DI essential. Binding mechanism required.

**Divergent**: DI approach (custom vs Swinject). Architecture complexity (MVVM-C sufficient vs TCA). At extreme scale (Uber, Airbnb) — custom architectures needed. For most apps: **MVVM + Coordinator + Combine + DiffableDataSource + programmatic Auto Layout + constructor injection**.
