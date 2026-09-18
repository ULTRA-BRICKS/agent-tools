# Dependency Injection in UIKit Context

## The Problem with Singletons

`NetworkManager.shared`, `AppDelegate.shared.service` — impossible to mock in tests, hidden dependencies, thread safety issues, app extension crashes.

## Constructor Injection — The Gold Standard

```swift
class ProfileViewController: UIViewController {
    private let viewModel: ProfileViewModel
    init(viewModel: ProfileViewModel) {
        self.viewModel = viewModel
        super.init(nibName: nil, bundle: nil)
    }
    required init?(coder: NSCoder) { fatalError("Use init(viewModel:)") }
}
```

Dependencies are non-optional, immutable, visible, compiler-enforced.

## Factory Pattern for VC Creation

```swift
protocol ViewControllerFactory {
    func makeHomeViewController() -> HomeViewController
    func makeDetailViewController(item: Item) -> DetailViewController
}

class AppViewControllerFactory: ViewControllerFactory {
    private let dependencies: AppDependencies
    func makeHomeViewController() -> HomeViewController {
        let vm = HomeViewModel(service: dependencies.userService)
        return HomeViewController(viewModel: vm)
    }
    // ...
}

struct AppDependencies {
    let userService: UserServiceProtocol
    let itemService: ItemServiceProtocol
    static func makeProduction() -> AppDependencies { /* ... */ }
    static func makeMock() -> AppDependencies { /* ... */ }
}
```

Coordinator holds the factory and uses it to create VCs.

## Storyboard DI Workaround (iOS 13+)

`storyboard.instantiateViewController(identifier:creator:)` — allows custom `init(coder:)`. **Programmatic creation still preferred** (no XML conflicts, no string IDs).

## @Injected Property Wrapper (Large Apps)

Lightweight DI modeled after SwiftUI's `@Environment`:

```swift
@propertyWrapper
struct Injected<T> {
    private let keyPath: WritableKeyPath<InjectedValues, T>
    var wrappedValue: T { InjectedValues[keyPath] }
    init(_ keyPath: WritableKeyPath<InjectedValues, T>) { self.keyPath = keyPath }
}

// Usage: @Injected(\.networkService) var networkService: NetworkServiceProtocol
// Tests: InjectedValues[\.networkService] = MockNetworkService()
// ⚠️ Always reset in tearDown
```

## Decision Table

| Approach | Compile-time safe | Testability | Scales to 50+ screens |
| --- | --- | --- | --- |
| Constructor injection | Yes | Excellent | Verbose |
| Factory pattern | Yes | Excellent | Good |
| @Injected wrapper | No (runtime) | Good (reset in tearDown) | Excellent |
| Third-party (Swinject) | No (runtime) | Good | Excellent |

**Recommendation**: Pure DI for < 20 screens; containers for larger apps.

## ❌ Anti-Patterns

- **Force-unwrapped deps**: `var service: NetworkService!` — runtime crash
- **AppDelegate as service locator** — thread unsafe, unavailable in extensions
- **Singleton abuse**: `NetworkManager.shared.fetch(...)` — hidden, untestable
