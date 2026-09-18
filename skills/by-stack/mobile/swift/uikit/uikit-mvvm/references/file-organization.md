# File Organization & Splitting Patterns

## File Size Guidelines

Smaller files are easier to read, review, and maintain. The thresholds below are **guidelines for new code** — not a hard gate that blocks every PR.

**Trigger thresholds:**

| Lines | Verdict | Action |
|-------|---------|--------|
| ≤ 400 | ✅ Fine | No action needed |
| 400–700 | 🟡 Review | Check for multiple concerns; plan a split if the file is still growing |
| 700–1000 | 🟠 Plan split | Identify logical boundaries and schedule an extraction |
| > 1000 | 🔴 Must address | File is a strong candidate for splitting; prioritise in the next cycle |

> Extension files under 30 lines are too granular — merge back or combine.

### New Code vs Refactoring

**New features** — aim for ≤ 400 lines per file from the start. Splitting at creation time is cheap.

**Refactoring / tech-debt** — do NOT force a file split inside a refactoring PR just to hit a line count. Splitting a 900-line legacy massive ViewController is itself a large refactoring; it deserves its own dedicated task in the feature's `refactoring/` plan. Forcing it into an unrelated migration PR creates cascading changes, bloats the diff, and defeats the purpose of small focused PRs.

## Decision Tree: How to Split

```text
File exceeds 400 lines (or growing toward it in new code)?
├── It's a ViewModel
│   ├── Has distinct feature groups (search, filtering, CRUD)?
│   │   └── Split by EXTENSION files: `MyVM+Search.swift`
│   ├── Has a child concern with its own lifecycle (form, detail panel)?
│   │   └── Extract CHILD VIEWMODEL: `FormViewModel` owned by parent
│   └── Has reusable business logic (validation, formatting)?
│       └── Extract HELPER / SERVICE class
├── It's a ViewController
│   ├── Has identifiable sub-sections (header, list, footer)?
│   │   └── Extract CHILD VIEWCONTROLLER: `HeaderViewController`, `ListViewController`
│   ├── Has complex layout code or repetitive UI configurations?
│   │   └── Extract custom `UIView` subclasses: `CustomHeaderView.swift`
│   └── Has long table/collection view data sources or delegates?
│       └── Adopt DiffableDataSource, or extract delegates into separate objects
```

---

## Splitting ViewModels by Extension

Use Swift file-level extensions to group related functionality while keeping a single ViewModel class.

### File: `ItemListViewModel.swift` (core)

```swift
import Foundation
import Combine

final class ItemListViewModel {
    // MARK: - State
    @Published private(set) var state: ViewState<[Item]> = .idle
    @Published private(set) var selectedFilter: ItemFilter = .all
    @Published var searchQuery = ""

    // MARK: - Dependencies
    private let repository: ItemRepositoryProtocol
    
    // MARK: - Init
    init(repository: ItemRepositoryProtocol) { /* ... */ }
    
    // MARK: - Core Actions
    func loadItems() { /* ... */ }
}
```

### File: `ItemListViewModel+Search.swift`

```swift
extension ItemListViewModel {
    // MARK: - Search & Filtering
    var filteredItems: [Item] { /* ... */ }
    func applyFilter(_ items: [Item]) -> [Item] { /* ... */ }
    func updateFilter(_ filter: ItemFilter) { /* ... */ }
}
```

### Naming Convention for Extension Files

```text
{ClassName}+{FeatureGroup}.swift
```

| File | Purpose |
|------|---------|
| `ProfileViewModel.swift` | Core state, init, dependencies |
| `ProfileViewModel+Validation.swift` | Form validation logic |
| `ProfileViewModel+Settings.swift` | User preferences management |

---

## Child ViewModel Pattern

When a concern has its own distinct lifecycle (modal form, expandable detail panel, filter sidebar), extract a **child ViewModel**.

```swift
// Parent: OrderListViewModel.swift
final class OrderListViewModel {
    @Published private(set) var state: ViewState<[Order]> = .idle
    private(set) var activeFilter: OrderFilterViewModel  // child VM
    
    init(repository: OrderRepositoryProtocol) { 
        self.activeFilter = OrderFilterViewModel()
    }
}

// Child: OrderFilterViewModel.swift
final class OrderFilterViewModel {
    @Published var dateRange: DateRange = .lastMonth
    @Published var statusFilter: OrderStatus? = nil
    
    func apply(to orders: [Order]) -> [Order] { /* ... */ }
}
```

---

## Splitting ViewControllers into Child ViewControllers

Large ViewControllers should be split into smaller, focused child ViewControllers using ViewController containment. This pattern separates concerns and reuses UI components.

### File: `ItemListViewController.swift` — Screen container

```swift
final class ItemListViewController: UIViewController {
    private let viewModel: ItemListViewModel
    private let headerVC: ItemListHeaderViewController
    private let bodyVC: ItemListBodyViewController

    init(viewModel: ItemListViewModel) {
        self.viewModel = viewModel
        self.headerVC = ItemListHeaderViewController(viewModel: viewModel)
        self.bodyVC = ItemListBodyViewController(viewModel: viewModel)
        super.init(nibName: nil, bundle: nil)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override func viewDidLoad() {
        super.viewDidLoad()
        setupChildViewControllers()
    }

    private func setupChildViewControllers() {
        addChild(headerVC)
        view.addSubview(headerVC.view)
        headerVC.didMove(toParent: self)
        
        addChild(bodyVC)
        view.addSubview(bodyVC.view)
        bodyVC.didMove(toParent: self)
        
        // setup Auto Layout constraints for child views
    }
}
```

---

## Splitting Views into Reusable Components

Large ViewControllers often contain hundreds of lines of programmatic Auto Layout setup. Extract these into custom `UIView` subclasses.

### File: `ItemHeaderView.swift`

```swift
final class ItemHeaderView: UIView {
    private let titleLabel = UILabel()
    private let subtitleLabel = UILabel()

    override init(frame: CGRect) {
        super.init(frame: frame)
        setupUI()
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    private func setupUI() {
        // configure subviews and layout constraints
    }

    func configure(title: String, subtitle: String) {
        titleLabel.text = title
        subtitleLabel.text = subtitle
    }
}
```

---

## When NOT to Split

- **File is under 200 lines** — splitting adds navigational overhead with no readability gain
- **All methods are tightly coupled** — splitting would require passing many parameters between extensions
- **It's a simple container/Coordinator** — a 150-line Coordinator handling routing logic is perfectly fine as-is
