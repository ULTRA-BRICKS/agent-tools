# Layout Approaches for UIKit

## Programmatic Auto Layout — The Enterprise Standard

```swift
label.translatesAutoresizingMaskIntoConstraints = false  // CRITICAL
view.addSubview(label)
NSLayoutConstraint.activate([...])  // pin with anchors
```

**Why enterprise teams prefer programmatic**: Git diffs are readable. All UI changes visible in PRs. No string identifiers. No XML merge conflicts. Consistent with SwiftUI migration path.

## SnapKit — DSL for Auto Layout

Widely adopted in enterprise iOS projects. Eliminates boilerplate (`translatesAutoresizingMaskIntoConstraints`, `NSLayoutConstraint.activate`) with a chainable DSL. SnapKit sets `translatesAutoresizingMaskIntoConstraints = false` automatically.

```swift
// Native Auto Layout (verbose)
label.translatesAutoresizingMaskIntoConstraints = false
view.addSubview(label)
NSLayoutConstraint.activate([
    label.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: 16),
    label.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 16),
    label.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -16)
])

// SnapKit equivalent
view.addSubview(label)
label.snp.makeConstraints { make in
    make.top.equalTo(view.safeAreaLayoutGuide).offset(16)
    make.leading.trailing.equalToSuperview().inset(16)
}
```

**Key APIs:**

- `snp.makeConstraints` — create constraints (use once per view)
- `snp.updateConstraints` — update constants on existing constraints (animations)
- `snp.remakeConstraints` — tear down all and recreate (layout state changes)

**When to use**: If the project already uses SnapKit, follow the existing convention. For new projects, either SnapKit or native anchors are acceptable — pick one and stay consistent across the codebase.

## UIStackView — Reduces Constraint Count

Pin only the outermost stack; nested stacks handle internal arrangement:

```swift
let mainStack = UIStackView(arrangedSubviews: [headerStack, contentStack, footerView])
mainStack.axis = .vertical
mainStack.spacing = 16
// Only 4 constraints to pin entire layout
```

## Lazy View Properties Pattern

```swift
private lazy var tableView: UITableView = {
    let tv = UITableView(frame: .zero, style: .plain)
    tv.translatesAutoresizingMaskIntoConstraints = false
    tv.register(ItemCell.self, forCellReuseIdentifier: ItemCell.reuseId)
    // configure rowHeight, estimatedRowHeight...
    return tv
}()
// Same pattern for activityIndicator, errorLabel, etc.
```

## XIBs: Reusable Cells and Simple Views Only

Use for self-contained, single-developer components (cells, custom controls). **Caution**: Renaming an outlet in code silently breaks the XIB connection → runtime crash.

## Storyboards: Prototyping Only at Scale

Problems at scale: unresolvable XML merge conflicts, segue spaghetti, slow compile times, silent outlet breaks, no constructor injection (pre-iOS 13).

## Decision Matrix

| Criterion | Programmatic (native) | SnapKit | XIBs | Storyboards |
| --- | --- | --- | --- | --- |
| Merge conflicts | Clean diffs | Clean diffs | Rare | Nightmare |
| Code review | Full visibility | Full visibility | Partial | None (XML) |
| DI support | Constructor injection | Constructor injection | Property injection | Limited |
| Boilerplate | Moderate | Low (DSL) | Low | Low |
| External dependency | None | SPM/CocoaPods | None | None |
| Team scaling | Excellent | Excellent | Good | Poor |

**Recommendation**: Programmatic (native or SnapKit) for all new code. Follow existing project convention. XIBs for standalone cells. Storyboards only for prototyping.

## Common Layout Patterns

- **Pin to edges**: `topAnchor`, `leadingAnchor`, `trailingAnchor`, `bottomAnchor` to superview
- **Center with size**: `centerXAnchor`, `centerYAnchor` + `widthAnchor`, `heightAnchor`
- **Scroll view**: pin scrollView to superview → contentView to `contentLayoutGuide` → contentView width to `frameLayoutGuide` width

## Layout Anti-Patterns

- **Missing `translatesAutoresizingMaskIntoConstraints = false`** — constraints silently conflict with autoresizing mask
- **Constraint activation in a loop** — use `NSLayoutConstraint.activate(constraints)` for batch activation
