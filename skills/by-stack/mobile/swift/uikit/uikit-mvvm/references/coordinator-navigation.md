# Coordinator Pattern for UIKit Navigation

## Why UIKit Needs Coordinators

UIKit's navigation APIs tightly couple view controllers. Coordinators remove navigation responsibility from VCs — they become inert, only displaying data and reporting user actions.

## Coordinator Protocol

```swift
protocol Coordinator: AnyObject {
    var childCoordinators: [Coordinator] { get set }
    var navigationController: UINavigationController { get set }
    func start()
}
```

**AppCoordinator** — root, decides auth vs main flow. **Flow Coordinators** — specific journeys (onboarding, checkout). **Tab Coordinators** — own `UITabBarController`, spawn per-tab child coordinators.

## Memory Management — The Most Critical Concern

**Parent → child**: strong. **Child → parent**: weak. **VC → coordinator**: weak.

```swift
class ChildCoordinator: Coordinator {
    weak var parentCoordinator: ParentCoordinator?
    // ...
}
class SomeViewController: UIViewController {
    weak var coordinator: SomeCoordinator?
}
```

**didFinish pattern**: `childCoordinators.removeAll { $0 === coordinator }` when child flow completes.

**deinit debugging**: `deinit { print("✅ \(Self.self) deallocated") }` — if missing when navigating away, you have a leak.

**Common leak sources**: VC strongly referencing coordinator, closures missing `[weak self]`, forgetting to remove finished child coordinators, back-button pops bypassing coordinator cleanup.

## Handling the Back Button

When user taps back / swipe-to-go-back, coordinator is never notified.

**Solution — Coordinator as UINavigationControllerDelegate:**

```swift
func navigationController(_ nav: UINavigationController,
                          didShow vc: UIViewController, animated: Bool) {
    guard let fromVC = nav.transitionCoordinator?.viewController(forKey: .from),
          !nav.viewControllers.contains(fromVC) else { return }
    // fromVC was popped — clean up its coordinator
}
```

## Coordinator + MVVM Integration

Coordinator creates ViewModel (with deps) and VC. ViewModel signals navigation via closures — **never imports UIKit**:

```swift
func start() {
    let viewModel = ProductListViewModel(service: networkService)
    viewModel.onProductSelected = { [weak self] product in self?.showProductDetail(product) }
    let vc = ProductListViewController(viewModel: viewModel)
    navigationController.pushViewController(vc, animated: true)
}
```

## Tab Coordinator

Each tab gets its own `UINavigationController` and child coordinator:

```swift
func start() {
    let homeNav = UINavigationController()
    let homeCoordinator = HomeCoordinator(navigationController: homeNav)
    // ... repeat for each tab
    tabBarController.viewControllers = [homeNav, searchNav, profileNav]
    childCoordinators.forEach { $0.start() }
}
```

## Deep Linking

Route enum for type-safe navigation: `enum DeepLink { case profile(userId: String), product(productId: String) }` with `init?(url: URL)` parser. AppCoordinator routes through coordinator hierarchy.

## ❌ Anti-Patterns

- **VC pushing other VCs directly** — tight coupling, untestable
- **Storyboard segue spaghetti** — stringly-typed, force casts
- **Forgetting child coordinator cleanup** — memory leaks
