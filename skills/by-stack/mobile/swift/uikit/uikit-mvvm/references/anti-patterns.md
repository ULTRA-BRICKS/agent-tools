# Anti-Patterns Detection & Fixes

## How to Use

When reviewing existing UIKit code, check each section below. For every violation found, add it to the feature's `refactoring/` plan with severity and recommended fix.

## Severity Levels

- **🔴 Critical**: Bugs, crashes, memory leaks. Fix immediately.
- **🟡 High**: Performance, testability blockers. Fix in next sprint.
- **🟢 Medium**: Code quality, maintainability. Fix opportunistically.

---

<critical_anti_patterns>
## 🔴 Critical

### C1: Massive ViewController

**Problem**: VC handles networking, validation, formatting, AND rendering. 500+ lines, untestable. **Fix**: Extract ViewModel with `@Published` state. VC only binds and renders.

### C2: ViewModel holding reference to ViewController

**Problem**: ViewModel must never import UIKit or hold VC reference. **Fix**: ViewModel publishes state, VC observes via Combine/closures.

### C3: Retain cycles in Combine subscriptions

**Problem**: Missing `[weak self]` in `sink` closures. `assign(to:on:)` always retains strongly.

```swift
// ❌ viewModel.$data.sink { data in self.render(data) }.store(in: &cancellables)
// ✅ viewModel.$data.sink { [weak self] data in self?.render(data) }.store(in: &cancellables)
```

### C4: Missing Task cancellation in deinit

**Problem**: UIKit does NOT auto-cancel Tasks. Uncancelled Tasks retain captured objects.

```swift
// ❌ Task { await viewModel.load() } — never cancelled
// ✅ loadTask = Task { [weak self] in await self?.viewModel.load() }; deinit { loadTask?.cancel() }
```

### C5: Force-unwrapped dependencies

**Problem**: `var service: NetworkService!` — runtime crash. **Fix**: Constructor injection with protocol.

### C6: UI updates from background thread (GCD)

**Problem**: Calling UIKit APIs from background `DispatchQueue` — purple warnings, crashes.

```swift
// ❌ service.fetch { [weak self] products in self?.tableView.reloadData() }
// ✅ service.fetch { [weak self] products in DispatchQueue.main.async { self?.tableView.reloadData() } }
```

### C7: Retain cycles in GCD completion handlers

**Problem**: Strong `self` capture in long-lived GCD closures. Unlike Combine, GCD closures run to completion — keeping `self` alive.

```swift
// ❌ DispatchQueue.global().async { self.process() }
// ✅ DispatchQueue.global().async { [weak self] in self?.process() }
```

### C8: DispatchSemaphore blocking main thread

**Problem**: `semaphore.wait()` on main thread freezes UI. Also blocks cooperative thread pool in async context. **Fix**: Use completion handlers, Combine, or async/await.

### C9: Using `assign(to:on:)` instead of `sink`

**Problem**: `assign(to:on:)` holds a **strong reference** to the target object. When `self` is the target AND owns `cancellables`, you get a retain cycle that never deallocates. AI assistants frequently generate this as "cleaner" syntax.

```swift
// ❌ MEMORY LEAK — assign(to:on:) retains self strongly, self retains cancellables
viewModel.$title
    .assign(to: \.text, on: titleLabel)  // retains titleLabel's owner forever
    .store(in: &cancellables)

// ✅ SAFE — sink with [weak self]
viewModel.$title
    .receive(on: DispatchQueue.main)
    .sink { [weak self] title in self?.titleLabel.text = title }
    .store(in: &cancellables)
```

### C10: Missing `.receive(on: DispatchQueue.main)` in Combine pipeline

**Problem**: `@Published` emits on whatever thread the property was set. If ViewModel sets state from a background queue, `sink` delivers on that thread. UI updates off main thread → purple warnings, crashes.

```swift
// ❌ Sink fires on background thread if ViewModel publishes from background
viewModel.$state
    .sink { [weak self] state in self?.render(state) }
    .store(in: &cancellables)

// ✅ Always add .receive(on:) before UI updates
viewModel.$state
    .receive(on: DispatchQueue.main)
    .sink { [weak self] state in self?.render(state) }
    .store(in: &cancellables)
```

### C11: Strong `self` in DiffableDataSource cellProvider

**Problem**: `cellProvider` closure is retained by the data source, which is retained by the VC. Capturing `self` strongly → retain cycle.

```swift
// ❌ self → dataSource → cellProvider → self
dataSource = UITableViewDiffableDataSource(tableView: tableView) { tableView, indexPath, item in
    self.configure(cell, with: item)  // strong capture
}

// ✅ Capture [weak self] or [unowned self]
dataSource = UITableViewDiffableDataSource(tableView: tableView) { [weak self] tableView, indexPath, item in
    // configure cell...
}
```
</critical_anti_patterns>

---

## 🟡 High

### H1: Separate isLoading + error + data booleans

**Problem**: Allows impossible states. **Fix**: `ViewState<T>` enum.

### H2: Oversized File (> 1000 lines)

**Problem**: Unmaintainable. **Fix**: Split VC into child VCs, ViewModel into extensions. Log in the feature's `refactoring/` plan.

### H3: VC instantiating other VCs

**Problem**: Tight coupling, untestable navigation. **Fix**: Coordinator pattern.

### H4: Storyboard segue spaghetti

**Problem**: Massive `prepare(for:sender:)` switch, stringly-typed, force casts. **Fix**: Coordinators.

### H5: Networking directly in VC

**Problem**: Untestable. **Fix**: Service/repository via ViewModel.

### H6: Singleton abuse

**Problem**: `Singleton.shared` everywhere — hidden, untestable. **Fix**: Constructor injection.

### H7: Massive ViewModel

**Problem**: Moving ALL logic from VC to VM without extracting layers. Creates "Massive ViewModel" — same problem, different file. **Fix**: VM handles only presentation logic (formatting, state). Extract networking → Repository, business rules → Use Case/Service, persistence → Store.

### H8: Child Coordinator not removed from parent

**Problem**: Parent holds `[Coordinator]` array. When user taps back button or swipes to dismiss, child coordinator stays in array forever — leaked. **Fix**: Implement `UINavigationControllerDelegate` to detect pop, call `didFinish` delegate to remove child from parent's array.

### H9: Mixing async/await and Combine without cancellation bridge

**Problem**: Wrapping Combine publishers in `async` (via `AsyncSequence` or continuations) without propagating cancellation. The Task cancels but the Combine subscription keeps running. **Fix**: Store `AnyCancellable` and cancel in continuation's `onTermination`, or stick to one paradigm per layer.

---

## 🟢 Medium

- **M1**: Missing `private(set)` on ViewModel state — breaks unidirectional flow
- **M2**: No MARK sections — hard to navigate
- **M3**: Manual `reloadData` instead of DiffableDataSource — crash-prone
- **M4**: NotificationCenter without cleanup — wasted memory
- **M5**: Force-unwrapping IBOutlets in programmatic code — crashes
- **M6**: `reloadData()` instead of `reconfigureItems` (iOS 15+) — inefficient
- **M7**: AppDelegate as service locator — thread unsafe, unavailable in extensions

---

<detection_checklist>
## Detection Checklist

1. [ ] **ViewModel UIKit-free**: imports only Foundation + Combine?
2. [ ] **No Massive VC**: under 500 lines? Business logic in ViewModel?
3. [ ] **No Massive VM**: ViewModel handles only presentation logic? Networking/business rules extracted?
4. [ ] **ViewState enum**: no separate booleans?
5. [ ] **Combine memory**: `[weak self]` in all sinks? No `assign(to:on:)`?
6. [ ] **Combine threading**: `.receive(on: DispatchQueue.main)` before every UI update in sink?
7. [ ] **GCD memory**: `[weak self]` in DispatchQueue closures? No semaphore on main?
8. [ ] **Main thread UI**: all UI updates on main (GCD and Combine)?
9. [ ] **Task management**: stored and cancelled in deinit?
10. [ ] **DI via constructor**: no singletons?
11. [ ] **Protocol abstractions**: mockable?
12. [ ] **Coordinator navigation**: VCs don't push VCs?
13. [ ] **Coordinator cleanup**: child coordinators removed from parent on back/dismiss?
14. [ ] **DiffableDataSource**: no manual reloadData? No strong self in cellProvider?
15. [ ] **Async/Combine bridge**: cancellation propagated when mixing paradigms?
16. [ ] **File size**: new ≤ 400, existing > 1000 flagged?
17. [ ] **Tests exist**: ViewModel has test file?
</detection_checklist>
