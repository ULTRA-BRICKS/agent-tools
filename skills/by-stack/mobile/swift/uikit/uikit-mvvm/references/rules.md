# UIKit MVVM Architecture — Rules Quick Reference

## Do's — Always Follow

1. **Keep ViewModels free of UIKit imports** — ViewModel imports only Foundation. No UIView, UIColor, UIImage references. This ensures testability on any platform.
2. **Use `ViewState<T>` enum for async data** — prevents impossible states (loading AND error simultaneously). Never use separate `isLoading` + `error` + `data` booleans.
3. **Bind in `viewDidLoad`, never in `init`** — at `viewDidLoad`, all outlets are connected and the view hierarchy is loaded. During `init`, views don't exist yet.
4. **Inject dependencies via constructor with protocol types** — enables testing, prevents singleton coupling. `init(viewModel: ProfileViewModel)`.
5. **Use `private(set) var` for ViewModel state** — ViewController can observe but not write. Enforces unidirectional data flow.
6. **Always use `[weak self]` in `sink` closures** — the retain cycle path is `self → cancellables Set → AnyCancellable → closure → self`.
7. **Use Coordinator pattern for navigation** — ViewModels signal navigation intent through closures. VCs never instantiate other VCs.
8. **Cancel Tasks in `deinit`** — UIKit does NOT auto-cancel Tasks like SwiftUI's `.task` modifier. Store `Task` references and cancel in `deinit`.
9. **Keep new files ≤ 400 lines** — split large VCs into child VCs; split large ViewModels by extension files (`VM+Feature.swift`). For existing files over 400 lines, add a split task to the feature's `refactoring/` plan.

## Don'ts — Critical Anti-Patterns

### Never: ViewModel imports UIKit

```swift
// ❌ Couples ViewModel to platform, breaks unit testing
import UIKit
class BadVM { var icon: UIImage = UIImage(systemName: "star")! }

// ✅ Platform-agnostic types only
import Foundation
class GoodVM: ObservableObject { @Published private(set) var iconName: String = "star" }
```

### Never: Networking in ViewController

```swift
// ❌ Business logic in VC, untestable
override func viewDidLoad() {
    super.viewDidLoad()
    URLSession.shared.dataTask(with: url) { data, _, _ in
        DispatchQueue.main.async { self.tableView.reloadData() }
    }.resume()
}

// ✅ ViewModel handles data, VC observes
override func viewDidLoad() {
    super.viewDidLoad()
    setupBindings()
    viewModel.fetch()
}
```

### Never: Missing `[weak self]` in sink closures

```swift
// ❌ RETAIN CYCLE — self → cancellables → AnyCancellable → closure → self
viewModel.$posts
    .sink { posts in self.renderPosts(posts) }
    .store(in: &cancellables)

// ✅ SAFE
viewModel.$posts
    .receive(on: DispatchQueue.main)
    .sink { [weak self] posts in self?.renderPosts(posts) }
    .store(in: &cancellables)
```

### Never: Force-unwrapped dependencies

```swift
// ❌ Runtime crash when not set
var service: NetworkService!

// ✅ Constructor injection with protocol
private let service: NetworkServiceProtocol
init(service: NetworkServiceProtocol) { self.service = service }
```

### Never: ViewController instantiating other ViewControllers

VCs should not know about other VCs. Navigation is Coordinator's responsibility. This ensures screens are reusable in different flows.
