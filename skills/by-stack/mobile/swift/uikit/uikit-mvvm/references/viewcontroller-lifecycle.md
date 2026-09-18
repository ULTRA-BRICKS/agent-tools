# ViewController Lifecycle and ViewModel Interaction

## Bind in viewDidLoad, Never in init

At `viewDidLoad`, all outlets are connected and view hierarchy is loaded. During `init`, views don't exist — accessing outlets crashes.

## ViewController Template

```swift
final class ItemListViewController: UIViewController {
    // MARK: - Properties
    private let viewModel: ItemListViewModel
    private var cancellables = Set<AnyCancellable>()

    // MARK: - UI Elements
    private lazy var tableView: UITableView = { /* configure, set translatesAutoresizingMaskIntoConstraints = false */ }()
    private lazy var activityIndicator: UIActivityIndicatorView = { /* configure */ }()

    // MARK: - Init
    init(viewModel: ItemListViewModel) {
        self.viewModel = viewModel
        super.init(nibName: nil, bundle: nil)
    }
    required init?(coder: NSCoder) { fatalError("Use init(viewModel:)") }

    // MARK: - Lifecycle
    override func viewDidLoad() {
        super.viewDidLoad()
        configureUI()
        setupBindings()
        viewModel.fetch()
    }

    // MARK: - Bindings
    private func setupBindings() {
        viewModel.$state
            .receive(on: DispatchQueue.main)
            .sink { [weak self] state in self?.render(state) }
            .store(in: &cancellables)
    }
}
```

## ViewState Enum

```swift
enum ViewState<T> {
    case idle, loading, loaded(T), failed(Error), empty
    var value: T? { /* ... */ }
    var isLoading: Bool { /* ... */ }
}
```

**Deterministic rendering** — hide all, then switch on state:

```swift
private func render(_ state: ViewState<[User]>) {
    [tableView, activityIndicator, errorView, emptyView].forEach { $0.isHidden = true }
    switch state {
    case .idle: break
    case .loading: activityIndicator.startAnimating(); activityIndicator.isHidden = false
    case .loaded(let users): tableView.isHidden = false; applySnapshot(users)
    case .failed(let error): errorView.isHidden = false; errorLabel.text = error.localizedDescription
    case .empty: emptyView.isHidden = false
    }
}
```

## DiffableDataSource with MVVM (iOS 13+)

Eliminates "Invalid update: invalid number of items" crashes:

```swift
private func makeDataSource() -> UITableViewDiffableDataSource<Section, ItemCellViewModel> {
    .init(tableView: tableView) { tableView, indexPath, item in
        let cell = tableView.dequeueReusableCell(withIdentifier: ItemCell.reuseId, for: indexPath) as? ItemCell
        cell?.configure(with: item)
        return cell
    }
}

private func applySnapshot(_ items: [ItemCellViewModel]) {
    var snapshot = NSDiffableDataSourceSnapshot<Section, ItemCellViewModel>()
    snapshot.appendSections([.main]); snapshot.appendItems(items)
    dataSource.apply(snapshot, animatingDifferences: true)
}
```

**CellViewModel** — Hashable value type: `struct ItemCellViewModel: Hashable { let id: UUID; let title: String }` — hash/equate by `id`.

**Modern Cell Registration (iOS 14+)** — `UICollectionView.CellRegistration` eliminates string-based reuse identifiers.

**WWDC tips**: store identifiers not full objects. Use `reconfigureItems` (iOS 15) for efficient visible cell updates.

## ViewController Containment

Strict three-step sequence:

- **Add**: `addChild(child)` → `container.addSubview(child.view)` → `child.didMove(toParent: self)`
- **Remove**: `child.willMove(toParent: nil)` → `child.view.removeFromSuperview()` → `child.removeFromParent()`

Skipping any step causes bugs (missing appearance callbacks).

## Keyboard & Trait Collection

View-layer concerns — stay in VC, not ViewModel. Use Combine publishers for keyboard notifications:

```swift
NotificationCenter.default.publisher(for: UIResponder.keyboardWillShowNotification)
    .compactMap { $0.userInfo?[UIResponder.keyboardFrameEndUserInfoKey] as? CGRect }
    .receive(on: DispatchQueue.main)
    .sink { [weak self] frame in /* adjust bottomConstraint */ }
    .store(in: &cancellables)
```
