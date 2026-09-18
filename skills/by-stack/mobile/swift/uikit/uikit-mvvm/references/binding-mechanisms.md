# Binding Mechanisms for UIKit MVVM

## The Core Problem

UIKit has no built-in binding system like SwiftUI's `@Observable`. ViewControllers must manually observe ViewModel state changes and update the UI. The binding mechanism you choose determines how reactive, testable, and maintainable your code is.

## Combine: @Published + sink (iOS 13+, Production Standard)

```swift
// ── ViewModel ──
final class PostsViewModel: ObservableObject {
    @Published private(set) var posts: [Post] = []
    @Published private(set) var isLoading = false
    let showAlert = PassthroughSubject<AlertMessage, Never>()  // One-shot events
    let navigate = PassthroughSubject<Route, Never>()
    func fetch() { /* ... */ }
}

// ── ViewController ──
private func setupBindings() {
    viewModel.$posts
        .receive(on: DispatchQueue.main)
        .sink { [weak self] posts in self?.renderPosts(posts) }
        .store(in: &cancellables)

    viewModel.showAlert
        .receive(on: DispatchQueue.main)
        .sink { [weak self] alert in self?.presentAlert(alert) }
        .store(in: &cancellables)
}
```

## PassthroughSubject vs CurrentValueSubject vs @Published

| Feature | PassthroughSubject | CurrentValueSubject | @Published |
|---|---|---|---|
| Has current value | No | Yes (`.value`) | Yes (direct) |
| Emits on subscribe | No | Yes | Yes |
| Fires `objectWillChange` | No | No | Yes |
| Usable in protocols | Yes | Yes | No |
| Fires timing | `send()` | `send()` / `.value =` | **willSet** |

**Critical**: `@Published` fires on **willSet** (before value changes). A subscriber reading the property during callback sees the **old** value.

**Use PassthroughSubject** for one-shot events (navigation, alerts). **Use @Published** for ViewModel state (default). **Use CurrentValueSubject** when you need `.value` access or protocol declarations.

## AnyCancellable Lifecycle

`Set<AnyCancellable>` ties subscription lifetime to the VC. When VC deallocates → all subscriptions cancel. For single-replacement: `private var cancellable: AnyCancellable?` — setting new value auto-cancels previous.

## Retain Cycle Rules

**Always `[weak self]` in `sink` when stored in `cancellables`.** Cycle: `self → cancellables → AnyCancellable → closure → self`.

```swift
// ❌ RETAIN CYCLE
publisher.sink { result in self.handle(result) }.store(in: &cancellables)
// ✅ SAFE
publisher.sink { [weak self] result in self?.handle(result) }.store(in: &cancellables)
```

**`assign(to:on:)` always retains strongly** — use `weakAssign` extension or `assign(to: &$property)` (iOS 14+).

## Input/Output Transform Pattern (Advanced)

```swift
final class SearchViewModel {
    struct Input { let search: AnyPublisher<String, Never> }
    enum Output { case idle, loading, success([Movie]), failure(Error) }

    func transform(input: Input) -> AnyPublisher<Output, Never> {
        input.search
            .debounce(for: .seconds(0.3), scheduler: DispatchQueue.main)
            .flatMap { [unowned self] query in self.searchMovies(query: query) /* ... */ }
            .eraseToAnyPublisher()
    }
}
```

## GCD + Completion Handlers (Pre-Combine Legacy Pattern)

Most legacy UIKit codebases use GCD as the primary concurrency mechanism. When extracting ViewModels from MVC, you'll encounter these patterns extensively.

### ViewModel with GCD-based networking

```swift
class ProductListViewModel {
    var onStateChanged: ((ViewState<[Product]>) -> Void)?
    private(set) var state: ViewState<[Product]> = .idle { didSet { onStateChanged?(state) } }

    func fetch() {
        state = .loading
        service.fetchProducts { [weak self] result in
            DispatchQueue.main.async {
                switch result {
                case .success(let products): self?.state = .loaded(products)
                case .failure(let error): self?.state = .failed(error)
                }
            }
        }
    }
}
```

### DispatchGroup for coordinating multiple requests

```swift
func loadDashboard() {
    let group = DispatchGroup()
    var profile: Profile?; var orders: [Order] = []

    group.enter()
    profileService.fetch { result in profile = try? result.get(); group.leave() }
    group.enter()
    orderService.fetch { result in orders = (try? result.get()) ?? []; group.leave() }

    group.notify(queue: .main) { [weak self] in /* combine results */ }
}
```

### Serial queue for thread-safe state

```swift
class CartViewModel {
    private let serialQueue = DispatchQueue(label: "com.app.cart.viewmodel")
    private var _items: [CartItem] = []
    var items: [CartItem] { serialQueue.sync { _items } }

    func addItem(_ item: CartItem) {
        serialQueue.async { [weak self] in
            self?._items.append(item)
            let copy = self?._items ?? []
            DispatchQueue.main.async { self?.onItemsChanged?(copy) }
        }
    }
}
```

### DispatchWorkItem for cancellable search

```swift
private var currentSearch: DispatchWorkItem?

func search(query: String) {
    currentSearch?.cancel()
    let item = DispatchWorkItem { [weak self] in
        self?.service.search(query: query) { results in
            DispatchQueue.main.async { self?.onResults?(results) }
        }
    }
    currentSearch = item
    DispatchQueue.global().asyncAfter(deadline: .now() + 0.3, execute: item)
}
```

**Limitation**: `cancel()` only prevents execution if not started — does NOT interrupt in-progress work.

### When GCD is sufficient vs when to upgrade

**GCD sufficient**: iOS < 13, simple one-shot flows, team unfamiliar with Combine, gradual migration phase.
**Upgrade to Combine**: multiple subscribers, nested completion handlers, debounce/throttle, combining streams, declarative error propagation.

## Closures / Bindable\<T\>

```swift
// Simple closure callback (one-to-one only)
class ProfileViewModel {
    var onStateChanged: ((ViewState) -> Void)?
    private(set) var state: ViewState = .idle { didSet { onStateChanged?(state) } }
}

// Reusable observable wrapper (no Combine)
class Bindable<T> {
    var value: T { didSet { observer?(value) } }
    var observer: ((T) -> Void)?
    init(_ value: T) { self.value = value }
    func bind(observer: @escaping (T) -> Void) { self.observer = observer; observer(value) }
}
```

**Closures sufficient**: one-to-one flow, iOS < 13, quick prototyping. **Key limitation**: if two UI elements observe same property, only last closure wins.

## async/await with UIKit (iOS 15+)

```swift
@MainActor
final class ProfileViewModel: ObservableObject {
    @Published private(set) var profile: Profile?
    func loadProfile() async {
        isLoading = true
        defer { isLoading = false }
        do { profile = try await service.fetchProfile() }
        catch { /* handle */ }
    }
}

// ViewController — MUST store and cancel Tasks manually
private var loadTask: Task<Void, Never>?
override func viewDidLoad() {
    super.viewDidLoad()
    loadTask = Task { [weak self] in await self?.viewModel.loadProfile() }
}
deinit { loadTask?.cancel() }
```

**Best practice (iOS 15+)**: async/await for data fetching + Combine for UI binding.

## Decision Matrix

| Criterion | GCD + Closures | Bindable\<T\> | Combine | async/await |
|---|---|---|---|---|
| Min iOS | Any | Any | 13+ | 15+ |
| Typical in legacy | **Very common** | Common | Growing | New code |
| One-shot request | Good | Good | Good | Best |
| Multiple subscribers | No | No | Yes | No |
| Cancellation | DispatchWorkItem (pre-start) | Manual | AnyCancellable | Task.cancel() |
| Thread safety | Serial queues | Manual | `.receive(on:)` | @MainActor |
| Debounce/throttle | asyncAfter (brittle) | No | Built-in | No |
| Parallel coordination | DispatchGroup | No | combineLatest/zip | TaskGroup |
| Debugging | Easy (stack traces) | Easy | Hard (chains) | Medium |

### Recommended adoption path for legacy projects

```text
Step 1: Extract ViewModel from MVC → keep GCD, use closures/Bindable<T>
Step 2: Add Combine for UI binding → @Published + sink, keep GCD in services
Step 3: Adopt async/await (iOS 15+) → replace completion handlers, keep Combine for UI
```
