# View Construction Reference

Core reference for building SwiftUI views: how to factor bodies into types, how `ForEach`/`List`/`Table` identity works, layout without `GeometryReader`, navigation/sheets/scrolling/focus mechanics, `Text` construction, and `#Preview` authoring. For state-flow (`@State`/`@Observable`/environment) see `dataflow.md`; for measured performance work (Instruments, `_printChanges`, hitching) see `performance.md`; for soft-deprecated API replacements see `deprecations.md`.

## View Structure: Structs vs. Computed Properties

A view is SwiftUI's unit of invalidation. When something changes, SwiftUI re-runs the `body` of the smallest enclosing **view type** that depends on it — every conditional, modifier chain, and string interpolation in that body, even if only one leaf actually depends on what changed.

**A computed property or `@ViewBuilder` function is inlined into the parent's body.** It does not introduce its own invalidation boundary and does not reduce update cost — it only reorganizes code. **A separate `View` type with narrow, explicit inputs becomes its own boundary** and re-runs only when its own inputs change. This is why "split your body for readability" is also a performance tool, but only when you split into real `View` types.

```swift
// BAD - re-executes complexSection() on every tap; it shares ParentView's
// invalidation boundary even though it looks "extracted".
struct ParentView: View {
    @State private var count = 0

    var body: some View {
        VStack {
            Button("Tap: \(count)") { count += 1 }
            complexSection()  // Re-executes every tap!
        }
    }

    @ViewBuilder
    func complexSection() -> some View {
        ForEach(0..<100) { i in
            HStack {
                Image(systemName: "star")
                Text("Item \(i)")
                Spacer()
                Text("Detail")
            }
        }
    }
}

// GOOD - ComplexSection's body is SKIPPED when its inputs don't change,
// because SwiftUI diffs the ComplexSection value itself first.
struct ParentView: View {
    @State private var count = 0

    var body: some View {
        VStack {
            Button("Tap: \(count)") { count += 1 }
            ComplexSection()  // Body skipped during re-evaluation
        }
    }
}

struct ComplexSection: View {
    var body: some View {
        ForEach(0..<100) { i in
            HStack {
                Image(systemName: "star")
                Text("Item \(i)")
                Spacer()
                Text("Detail")
            }
        }
    }
}
```

Pass each subview only the data it actually reads — the same "narrow inputs" rule that governs `@Observable` model access (see `dataflow.md`).

### Multi-section detail views: the most common place this rule gets dropped

A prompt like "build a `ProductDetailView` with a header, gallery, description, and reviews" has a training-data shape of one `View` with `private var header: some View`, `private var gallery: some View`, etc. **That shape is wrong** — it shares one invalidation boundary, so a change affecting one section re-evaluates all of them. Factor each named section into its own `View` type with only the fields it renders, and keep the parent thin:

```swift
struct ProductDetailView: View {
    let product: Product

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                ProductHeader(name: product.name, price: product.price)
                ProductGallery(images: product.imageURLs)
                ProductDescription(text: product.descriptionText)
                ProductReviews(
                    averageStars: product.averageStars,
                    reviewCount: product.reviewCount
                )
            }
            .padding()
        }
    }
}

struct ProductHeader: View {
    let name: String
    let price: Decimal

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(name).font(.largeTitle).fontWeight(.bold)
            Text(price, format: .currency(code: "USD"))
                .font(.title2)
                .foregroundStyle(.secondary)
        }
    }
}

struct ProductGallery: View {
    let images: [URL]

    var body: some View {
        ScrollView(.horizontal) {
            HStack {
                ForEach(images, id: \.self) { url in
                    AsyncImage(url: url) { image in
                        image.resizable().scaledToFill()
                    } placeholder: {
                        Color.secondary.opacity(0.2)
                    }
                    .frame(width: 120, height: 120)
                    .clipShape(RoundedRectangle(cornerRadius: 12))
                }
            }
        }
    }
}

struct ProductDescription: View {
    let text: String
    var body: some View { Text(text).font(.body) }
}

struct ProductReviews: View {
    let averageStars: Double
    let reviewCount: Int

    var body: some View {
        HStack {
            Label("\(averageStars, specifier: "%.1f")", systemImage: "star.fill")
            Text("(\(reviewCount) reviews)").foregroundStyle(.secondary)
        }
        .font(.subheadline)
    }
}
```

This generalizes to every `*DetailView`: `MovieDetailView`, `RecipeDetailView`, `ArticleDetailView`, `EpisodeDetailView` — one `View` type per section, narrow inputs each, a thin parent that composes them. Don't reach for `private var header: some View` on the parent.

Small `@ViewBuilder` fragments reused two or three times **within the same body**, with no independent invalidation story, are still fine — the rule targets factoring done for organization or to manage body length, where a real `View` type does the right thing. `@ViewBuilder` is required specifically when a function or computed property returns different concrete view types conditionally (`if`/`else`, `switch`); if every branch returns the same type, it's unnecessary.

### Decision rule: struct, function, or computed property?

- View is reusable across screens, or holds any `@State`/`@Binding`/`@ObservedObject`/`@Environment`/`@StateObject` → a separate `struct`, always.
- View takes parameters that change per call site (e.g. inside a `ForEach`, where each call receives a different item) → a separate `struct`, so SwiftUI can diff inputs and skip body evaluation. A method call with those same varying parameters cannot be skipped.
- View is simple, static, used once, and has no independent invalidation story → a computed property is acceptable.
- View needs stable parameters and is used once, non-reactively → a method returning `some View` is acceptable.

### Keep view `init` cheap

`init` runs every time the parent re-evaluates its body — potentially many times per second inside `List`, `LazyVStack`, scroll containers, or animated parents. Treat `init` as a constant-time copy of inputs into stored properties. Never decode JSON, build a `DateFormatter`, touch the file system, or allocate large structures there.

```swift
// AVOID: every parent body pass re-decodes JSON and rebuilds a formatter,
// even when the inputs haven't changed.
struct WeatherCard: View {
    let summary: WeatherSummary
    let formattedDate: String

    init(rawJSON: Data, date: Date) {
        self.summary = try! JSONDecoder().decode(WeatherSummary.self, from: rawJSON)
        let formatter = DateFormatter()
        formatter.dateStyle = .medium
        self.formattedDate = formatter.string(from: date)
    }

    var body: some View {
        VStack { Text(summary.headline); Text(formattedDate) }
    }
}

// PREFER: inputs are already-prepared values; formatting uses SwiftUI's
// built-in Text(_:format:), which is cached and locale-aware.
struct WeatherCard: View {
    let summary: WeatherSummary
    let date: Date

    var body: some View {
        VStack {
            Text(summary.headline)
            Text(date, format: .dateTime.day().month().year())
        }
    }
}
```

If a derived value genuinely needs to be computed once and cached for the view's lifetime, store it on a `@State`-owned `@Observable` model or compute it asynchronously in `.task` (see `dataflow.md`) — `init` is not a one-time setup hook; it runs as often as the parent's body does. The same rule applies to `UIViewRepresentable`/`NSViewRepresentable` structs: the representable struct itself is recreated on every SwiftUI redraw, so keep heavy work out of its `init` too (`makeUIView` runs once; `updateUIView` runs on every redraw to sync state; use a `Coordinator` for delegate callbacks).

### Single-child `Group` is not free

`Group { SomeView() }` — a `Group` with exactly one concrete child — wraps the view in an extra `Group<SomeView>` type for no visual effect. Every modifier chained after it (`.onChange`, `.background`, `.frame`, …) is type-checked against that wrapper instead of the underlying view's type, adding avoidable type-checking overhead in long chains.

```swift
// AVOID
Group { Text(status) }
    .padding(.horizontal, 8)
    .background(.thinMaterial, in: Capsule())

// PREFER: chain directly on the child
Text(status)
    .padding(.horizontal, 8)
    .background(.thinMaterial, in: Capsule())
```

The rule is specifically about **one concrete view**. A `Group` whose content is a `ForEach`, a `TupleView` of sibling views, or an `if`/`else` (which produces `_ConditionalContent`) is doing real work and is fine:

```swift
// PREFER: multiple siblings — Group applies a modifier to each as a unit
// without a stack container that would change layout.
Group {
    Button("Save", action: onSave)
    Button("Cancel", action: onCancel)
    Button("Delete", role: .destructive, action: onDelete)
}
.buttonStyle(.borderedProminent)
.controlSize(.large)

// PREFER: wrapping if/else so a shared modifier applies to both branches —
// the content is `_ConditionalContent<...>`, not a single concrete view.
Group {
    if let label {
        Text(label).padding(4).background(.thinMaterial, in: Capsule())
    } else {
        Color.clear
    }
}
.accessibilityHidden(label == nil)
```

### Keep `body` simple — avoid high-cost operations inside it

`body` can be called multiple times during a single layout pass. Filtering, sorting, mapping, or allocating a formatter inline makes each call more expensive than necessary:

```swift
// AVOID
var body: some View {
    List {
        ForEach(model.values.filter { $0 > 0 }, id: \.self) {
            Text(String($0)).padding()
        }
    }
}

// PREFER: prepare the sequence once, outside body
struct FilteredListView: View {
    private let filteredValues: [Int]

    init(values: [Int]) {
        self.filteredValues = values.filter { $0 > 0 }
    }

    var body: some View {
        List { ForEach(filteredValues, id: \.self) { Text(String($0)).padding() } }
    }
}
```

General guidance: avoid filtering/sorting/mapping inline in `body`; avoid constructing expensive formatters in `body`; avoid heavy branching in large view trees; move data preparation into the model layer. (See the "Don't sort or filter inline in `ForEach`" section below for the `ForEach`-specific version of this rule, including where the cached collection should live.)

### Container views: `@ViewBuilder` property, not a closure

Closures can't be compared, so a container that stores `content: () -> Content` forces SwiftUI to call it — and re-render its contents — on every parent update, defeating diffing:

```swift
// BAD - closure prevents SwiftUI from skipping updates
struct MyContainer<Content: View>: View {
    let content: () -> Content
    var body: some View { VStack { Text("Header"); content() } }
}

// GOOD - the view value itself can be compared and skipped if unchanged
struct MyContainer<Content: View>: View {
    @ViewBuilder let content: Content
    var body: some View { VStack { Text("Header"); content } }
}
```

### Split state-driven parts into separate view types

If a single view's body reads several independent state sources, any one of them changing re-evaluates the whole body. Split by state ownership, not just by section:

```swift
struct BigAndComplicatedView: View {
    @State private var counter = 0
    @State private var isToggled = false
    @StateObject private var viewModel = SomeViewModel()

    var body: some View {
        VStack {
            titleView
            CounterView(counter: $counter)
            ToggleView(isToggled: $isToggled)
            ViewModelDataView(data: viewModel.data) { viewModel.updateData() }
                .equatable()
        }
    }

    private var titleView: some View {
        Text("Big and Complicated View").font(.largeTitle)
    }
}
```

Changing `counter` now only affects `CounterView`; toggling only affects `ToggleView`; model updates only affect `ViewModelDataView`. `Equatable` conformance on a view (`.equatable()`) is not a universal best practice — reach for it only when the input is small and well-defined, the comparison is meaningful, and you want to reduce body evaluation for a specific subtree. Don't apply it as a blanket optimization.

### File organization, business logic, and action handlers

- Each type (struct, class, enum) belongs in its own file; flag files with multiple type definitions.
- Property ordering inside a view (environment, then state, then passed-in properties, then `init`, `body`, helper subviews with `// MARK:` sections) has no effect on correctness or performance — a readability convention, not a rule. Adopt it only if the team wants the consistency; never reorder existing code solely to match it.
- Keep business logic in services/view models, not inline in `body`, `.task`, or `.onAppear` — this makes it independently testable without instantiating a view. Button actions should be extracted into methods rather than inlined as multi-line closures:

```swift
// PREFER: action references a method
Button("Publish Project", action: publishService.handlePublish)

// AVOID: multi-line logic mixed into the view body
Button("Publish Project") {
    isLoading = true
    apiService.publish(project) { result in /* ... */ }
}
```

When a button action can be passed directly as an `action:` parameter, prefer that over a trailing closure: `Button("Label", systemImage: "plus", action: myAction)` over `Button("Label", systemImage: "plus") { action() }`.

### Debugging view invalidation

`Self._printChanges()` (or `_logChanges()`) logs why a view's body is being re-evaluated — useful when a body runs more often than expected:

```swift
struct ContentView: View {
    @State private var counter: Int = 99

    var body: some View {
        let _ = Self._printChanges()
        VStack {
            Text("Counter: \(counter)")
            Button("Counter +1") { counter += 1 }.buttonStyle(.borderedProminent)
        }
        .padding()
    }
}
```

Use these only for debugging; remove them from production code. For deeper invalidation/hitching diagnosis (Instruments traces, signposts), see `performance.md`.

If the compiler reports "unable to type-check this expression in reasonable time," it's usually an overly complex view expression, not a real type error. Fix it by breaking large expressions into smaller computed values, extracting subviews, splitting long modifier chains, simplifying nested generics, or avoiding huge inline closures — not by restructuring the underlying logic.

### Animating views

Prefer the `@Animatable` macro over hand-writing `animatableData`: it synthesizes `Animatable` conformance and the correct `animatableData` property. Mark properties that shouldn't animate (booleans, integers, etc.) `@AnimatableIgnored`.

Never call the bare `.animation(_ animation: Animation?)` overload; always animate a specific value: `.animation(.bouncy, value: score)`. Chain animations using a `completion` closure on `withAnimation`, not nested/delayed `withAnimation` calls:

```swift
Button("Animate Me") {
    withAnimation {
        scale = 2
    } completion: {
        withAnimation { scale = 1 }
    }
}
```

## Conditional View Modifiers

**Never write a `.if` conditional-modifier extension** that uses `@ViewBuilder` to switch between `transform(self)` and `self`:

```swift
// AVOID: destroys structural identity every time `condition` toggles
extension View {
    @ViewBuilder
    func `if`<Content: View>(_ condition: Bool, transform: (Self) -> Content) -> some View {
        if condition { transform(self) } else { self }
    }
}

Text("Hello").if(isHighlighted) { $0.foregroundStyle(.red) }
```

Why it's problematic:

1. **View identity loss** — the branches produce different types, so toggling the condition looks to SwiftUI like an entirely different view, not a modified version of the same one.
2. **State reset** — any `@State` in the view or descendants resets because SwiftUI treats the branches as distinct views.
3. **Broken animations** — instead of smoothly animating a property, SwiftUI removes one view and inserts another, producing an abrupt transition.

Fix: apply the modifier unconditionally with a ternary, so the view identity and return type never change:

```swift
// PREFER
Text("Hello").foregroundStyle(isHighlighted ? .red : .primary)
Text("Hello").opacity(isHighlighted ? 1 : 0.5)
```

If you encounter an existing `.if` extension in a codebase, don't remove or refactor it as a drive-by fix — that can change behavior — but flag the identity/animation risk and point to the ternary alternative.

**The same principle applies one level up: prefer a "no-effect" modifier over conditionally including a view**, when you're really representing two states of the same view rather than two different views:

```swift
// PREFER — same view, different states
SomeView().opacity(isVisible ? 1 : 0)

// AVOID — creates/destroys view identity
if isVisible { SomeView() }
```

Use a real conditional only when the branches are genuinely different views, not different states of one view:

```swift
// CORRECT — fundamentally different views
if isLoggedIn { DashboardView() } else { LoginView() }

// CORRECT — optional content
if let user { UserProfileView(user: user) }
```

## ForEach, List, Table, and OutlineGroup: Element Identity

`ForEach` uses identity to match elements across body evaluations. When a parent's `body` re-runs, SwiftUI diffs the previous collection of identifiers against the new one to determine what was inserted, removed, moved, or merely updated. Stable, correct identity is what lets SwiftUI:

- Preserve `@State`, focus, selection, and scroll position for a row that moved or whose content changed.
- Animate insertions, removals, and reorders correctly — a moved row keeps its on-screen presence; a new row fades/slides in; a removed row transitions out.
- Avoid rebuilding subtrees unnecessarily by reusing the existing view for an element whose data changed.

If identity is unstable, none of this works: state resets, animations degrade to abrupt replacements, and performance suffers from unnecessary subtree rebuilds. The rule: identity must be **stable** (the same element has the same id across body evaluations, even if its position changes) and **unique** (no two distinct elements share an id within one `ForEach`).

### Applies to every data-driven container, not just `ForEach`

Any SwiftUI API that takes a `RandomAccessCollection` plus an `id:` key path (or `Identifiable` elements) and behaves like `ForEach` internally is subject to every rule in this section:

- `List(_:id:rowContent:)` / `List(_:rowContent:)` (the `Identifiable` overload) and selection-aware `List` overloads.
- `Table(_:)` / `Table(_:selection:)` and their `id:` overloads.
- `OutlineGroup(_:id:children:content:)` and `List(_:children:rowContent:)` (outline variants).
- `Picker` overloads that iterate a data collection (typically via `ForEach` inside).
- `DisclosureGroup` paired with `ForEach` in its content.

### Anti-pattern: collection indices as identity

Indices describe a position, not an element. As soon as the collection is reordered, inserted into, or filtered, the same index refers to a different element and SwiftUI has no way to tell.

```swift
// AVOID: inserting/reordering `items` makes every id from the insertion
// point onward map to a different element. SwiftUI reads this as "the
// element at id 3 changed" rather than "element B moved from 3 to 4" —
// row state resets and moves animate as replacements. Can also crash on
// removal if a stale index outlives the array shrinking.
ForEach(items.indices, id: \.self) { index in
    ItemRow(item: items[index])
}

// PREFER: identify by a property that travels with the element
ForEach(items, id: \.id) { item in
    ItemRow(item: item)
}
```

Seeing `.indices`, `\.offset`, or `id: \.self` on anything that isn't genuinely identity-like (e.g. a `String` that's already a unique key) signals identity derived from position.

**`.enumerated()` itself is fine — the index just must not be the id.** It's a reasonable way to get the index alongside the element (e.g. to display a row's position); the anti-pattern is using the offset as identity:

```swift
// AVOID: same failure mode as `.indices` — the id is the position
ForEach(items.enumerated(), id: \.offset) { index, item in
    ItemRow(number: index + 1, item: item)
}

// PREFER: id comes from the element; the index is just row data
ForEach(items.enumerated(), id: \.element.id) { index, item in
    ItemRow(number: index + 1, item: item)
}
```

As of Swift 6.1, `.enumerated()`'s result conditionally conforms to `RandomAccessCollection` when the base collection does, so `ForEach` accepts it directly with no `Array(...)` wrapper — favor the direct form; it avoids an eager copy of the collection on every body evaluation. On earlier toolchains, the `Array(...)` wrapper is still required.

### Anti-pattern: synthesizing a fresh id on every body evaluation

An `Identifiable` type whose `id` is generated fresh each time `body` runs looks identity-bearing but produces a brand-new id set every update, which `ForEach` reads as "the whole collection was replaced":

```swift
// AVOID: constructing Item values inside body — every call to Item(title:)
// initializes a new UUID, so ForEach sees a full replacement every update:
// state resets, rows flicker, animations degenerate to full replacement.
struct Item: Identifiable {
    let id = UUID()
    var title: String
}

struct ContentView: View {
    let titles: [String]
    var body: some View {
        List { ForEach(titles.map { Item(title: $0) }) { item in Text(item.title) } }
    }
}
```

`let id = UUID()` as a default is fine as long as the value is stored somewhere durable (`@State`, an `@Observable` model, a database row) — it becomes a bug only when the value is reconstructed on every body pass. Fix: derive identity from a naturally immutable property, or synthesize the id once in storage that outlives `body`:

```swift
// PREFER: identity from a naturally immutable property
struct Document: Identifiable {
    let url: URL              // assigned at creation
    var displayName: String   // user-editable
    var id: URL { url }
}

// PREFER: create the UUID once, in the model, and keep it across updates
@MainActor @Observable
final class ItemStore {
    var items: [Item] = []
    func add(title: String) { items.append(Item(id: UUID(), title: title)) }
}

struct Item: Identifiable {
    let id: UUID
    var title: String
}
```

### Prefer `Identifiable` conformance

`ForEach` accepts an explicit `id:` key path, but conforming the element to `Identifiable` is idiomatic when it has a natural identity — it lets callers write `ForEach(items)`, documents identity at the type level, and works with every other API expecting `Identifiable` (`List`, `sheet(item:)`, `confirmationDialog(..., presenting:)`, navigation value types, …):

```swift
struct Item: Identifiable {
    let id: UUID
    var title: String
}
ForEach(items) { item in ItemRow(item: item) }

// Acceptable when the element type isn't yours to change, or the id
// lives on a different type:
ForEach(items, id: \.serverID) { item in ItemRow(item: item) }
```

Don't conform a type to `Identifiable` just to satisfy `ForEach` when there's no meaningful notion of identity — pass an explicit key path instead. Classes get a default `ObjectIdentifier`-based `id` when conforming without providing one; this is only unique for the object's lifetime and can be recycled after deallocation.

### Keep the id cheap to hash

`ForEach` hashes and compares ids on every diff, i.e. on every body evaluation of the enclosing view. Using the entire element as the id (`id: \.self` on a large `Hashable` struct) means the compiler-synthesized `Hashable` conformance feeds every stored property into the hasher on every row, every update:

```swift
// AVOID: hashing every row walks every field — long strings, nested
// arrays, everything. Cost scales with collection size AND field count.
struct Article: Hashable {
    let title: String
    let body: String
    let tags: [String]
    let author: Author
    let publishedAt: Date
}
ForEach(articles, id: \.self) { article in ArticleRow(article: article) }

// PREFER: id is a small, cheap-to-hash property; the full struct still
// reaches the row view, but only the id is hashed during diffing.
struct Article: Identifiable, Hashable {
    let id: UUID
    let title: String
    let body: String
    let tags: [String]
    let author: Author
    let publishedAt: Date
}
ForEach(articles) { article in ArticleRow(article: article) }
```

Good ids are small primitives — `UUID`, `Int`, a short `String` key, a `URL` — that hash in constant time regardless of the element's size. Fix the id choice, not the `Hashable` conformance: it may be used elsewhere (selection, sets, dictionary keys, navigation values), and removing it is unrelated to the diffing cost.

### Identity must outlive the view, and must not change on edit

`ForEach` assumes an element's identity is stable for as long as the enclosing view is on screen. If an id changes while that view is alive, SwiftUI reads it as "old element removed, new one inserted" — state, focus, and selection for that row are lost. The common trap is deriving `id` from a mutable property:

```swift
// AVOID: typing in the row's text field renames the item, which changes
// its id, which makes ForEach think the row was removed and re-inserted.
// The text field loses focus on every keystroke.
struct Item: Identifiable {
    var id: String { title }
    var title: String
}

// PREFER: id is independent of any mutable content
struct Item: Identifiable {
    let id: UUID
    var title: String
}
```

Ask: "If I edit this element in place, does its id change?" If yes, identity is tied to content and will break on every edit.

### IDs must be truly unique

Non-unique ids cause SwiftUI to treat different items as identical — duplicate rendering or missing views:

```swift
// Bug: two articles with the same URL show identical content
struct Article: Identifiable {
    let title: String
    let url: URL
    var id: String { url.absoluteString }  // Not unique if URLs repeat!
}

// Fix: use a genuinely unique identifier
struct Article: Identifiable {
    let id: UUID
    let title: String
    let url: URL
}
```

### Don't sort or filter inline in `ForEach`

The collection passed to `ForEach` is evaluated on every body run of the enclosing view. A non-trivial transformation there — `sorted`, `filter`, a `map` that rebuilds elements, grouping, deduplication — repeats on every invalidation, even ones unrelated to the list contents (a parent state change, an environment update, a window resize):

```swift
// AVOID: every body evaluation re-runs filter + sorted over the full
// array, even when the invalidating change has nothing to do with items.
struct ItemList: View {
    let items: [Item]
    let searchText: String
    var body: some View {
        List {
            ForEach(
                items
                    .filter { $0.title.localizedCaseInsensitiveContains(searchText) }
                    .sorted { $0.title < $1.title }
            ) { item in ItemRow(item: item) }
        }
    }
}
```

Cache the derived collection and recompute only when an input actually changes. An `@Observable` model is the natural home — recompute in the mutating entry points or a `didSet`, and let the view read the already-prepared array:

```swift
@MainActor @Observable
final class ItemListModel {
    var items: [Item] = [] { didSet { recomputeVisibleItems() } }
    var searchText: String = "" { didSet { recomputeVisibleItems() } }
    private(set) var visibleItems: [Item] = []

    private func recomputeVisibleItems() {
        visibleItems = items
            .filter { $0.title.localizedCaseInsensitiveContains(searchText) }
            .sorted { $0.title < $1.title }
    }
}

struct ItemList: View {
    let model: ItemListModel
    var body: some View {
        List { ForEach(model.visibleItems) { item in ItemRow(item: item) } }
    }
}
```

If the derived collection is genuinely view-local (e.g. a local filter box that doesn't belong in the model), cache it in `@State` and update it via `.onChange(of:)` instead of recomputing in `body`. Cheap transformations — a small slice, `prefix(n)`, reading an already-prepared array, a trivial map to a struct — are fine inline; the rule targets work whose cost scales with the collection, or that allocates new elements.

### Unary vs. multi rows: keeping `List` fast

`List` needs the identity of every row up front to diff against the previous update. When each row's body produces a **single top-level view** ("unary"), SwiftUI can template the row id from the `ForEach` element's id alone, without running each row's `body`. When the body branches between different top-level shapes — a bare top-level `switch`, a top-level `if` without `else`, or an `AnyView` — structural identity varies per row, and SwiftUI falls back to evaluating every row's body just to compute ids. That cost scales with the number of rows.

```swift
// AVOID: the row is "multi" — a top-level switch makes structural identity
// depend on which case ran. Computing ids requires evaluating every row.
struct ItemRow: View {
    var item: Item
    var body: some View {
        switch item.kind {
        case .plain:       Text(item.title)
        case .highlighted: Text(item.title).bold()
        case .disabled:    Text(item.title).foregroundStyle(.secondary)
        }
    }
}

// PREFER: wrap branching content in a single-root container so the row
// is always exactly one top-level view, regardless of which case ran.
struct ItemRow: View {
    var item: Item
    var body: some View {
        VStack {
            switch item.kind {
            case .plain:       Text(item.title)
            case .highlighted: Text(item.title).bold()
            case .disabled:    Text(item.title).foregroundStyle(.secondary)
            }
        }
    }
}
```

Any single-root container works — `VStack`, `HStack`, `ZStack`, or a custom wrapper. Don't "fix" a multi row by flattening the branches into one shape with conditional modifiers (e.g. `Text(item.title).bold(item.kind == .highlighted)`); that happens to make this specific row unary only because all cases produced the same top-level shape, and it breaks the moment cases produce structurally different views (`Text` vs `Image` vs `Divider`). Wrap the switch in a container instead.

**A view is unary when its `body` produces a single top-level view; it's multi when body produces more than one top-level view, or branches between different top-level shapes.** `Group` and `ForEach` are passthroughs, not containers — `Group { A(); B(); C() }` contributes the same three top-level views as writing `A(); B(); C()` directly.

A top-level `if` **without** `else` is also multi — it produces 0 or 1 views depending on the condition, so the row builder does not produce a constant number of views per the `ForEach` documentation's own framing:

```swift
// AVOID: 0 or 1 view depending on name length — not constant
ForEach(namedFonts) { namedFont in
    if namedFont.name.count != 2 { Text(namedFont.name) }
}

// PREFER: wrap so the row is always exactly one top-level view
ForEach(namedFonts) { namedFont in
    VStack { if namedFont.name.count != 2 { Text(namedFont.name) } }
}
```

If the real intent is "skip this element entirely," filter the collection before it reaches `ForEach` rather than producing a zero-view row.

**`AnyView` in a row erases structural identity as well as type** — SwiftUI can no longer tell from the type alone which shape a row produced, defeating the same fast path:

```swift
// AVOID
ForEach(items) { item in rowView(for: item) }  // returns AnyView
func rowView(for item: Item) -> AnyView {
    switch item.kind {
    case .plain:       return AnyView(Text(item.title))
    case .highlighted: return AnyView(Text(item.title).bold())
    case .disabled:    return AnyView(Text(item.title).foregroundStyle(.secondary))
    }
}

// PREFER: a concrete row view whose static shape is visible to SwiftUI
struct ItemRow: View {
    var item: Item
    var body: some View {
        VStack {
            switch item.kind {
            case .plain:       Text(item.title)
            case .highlighted: Text(item.title).bold()
            case .disabled:    Text(item.title).foregroundStyle(.secondary)
            }
        }
    }
}
ForEach(items) { item in ItemRow(item: item) }
```

Don't "fix" this by replacing `AnyView` with a `@ViewBuilder` helper returning `some View` — the helper body is still a bare `switch` producing a `_ConditionalContent` tree, so the row remains multi-shape and the fast path is still defeated. Removing type erasure is only half the fix; wrap the branching content in a concrete row view with a single-root container too.

To find non-constant row builders in an existing app, launch with `-LogForEachSlowPath YES`; SwiftUI logs each `ForEach` inside a lazy container (`List`, `LazyVStack`, and similar) whose row body produces a non-constant number of views, pointing at the offending call sites.

### `List` extras

```swift
// Custom styling: remove default background/separators
List(items) { item in
    ItemRow(item: item)
        .listRowInsets(EdgeInsets(top: 8, leading: 16, bottom: 8, trailing: 16))
        .listRowSeparator(.hidden)
}
.listStyle(.plain)
.scrollContentBackground(.hidden)   // required before a custom .background() has any visible effect
.background(Color.customBackground)
.environment(\.defaultMinListRowHeight, 1)  // allows custom row heights

// Pull-to-refresh
List(items) { item in ItemRow(item: item) }
    .refreshable { await loadItems() }
```

`ContentUnavailableView` (iOS 17+) is the standard empty/missing-data state — prefer it over a custom design. The `.search` variant auto-localizes and auto-includes the search term (no need to pass `text: searchText)` yourself):

```swift
List {
    ForEach(searchResults) { item in ItemRow(item: item) }
}
.overlay {
    if searchResults.isEmpty, !searchText.isEmpty {
        ContentUnavailableView.search
    }
}

// Non-search empty state
ContentUnavailableView(
    "No Articles", systemImage: "doc.richtext.fill",
    description: Text("Articles you save will appear here.")
)
```

### `Table` (iOS 16+, iPadOS 16+, visionOS 1.0+)

A multi-column data container for `Identifiable` rows with sortable, selectable columns. On compact size classes (iPhone, iPad Slide Over), columns after the first are automatically hidden.

```swift
struct Person: Identifiable {
    let givenName: String
    let familyName: String
    let emailAddress: String
    let id = UUID()
    var fullName: String { givenName + " " + familyName }
}

struct PeopleTable: View {
    @State private var people: [Person] = [ /* ... */ ]
    var body: some View {
        Table(people) {
            TableColumn("Given Name", value: \.givenName)
            TableColumn("Family Name", value: \.familyName)
            TableColumn("E-Mail Address", value: \.emailAddress)
        }
    }
}
```

**Selection** — bind a single `ID` for single-selection, or a `Set<ID>` for multi-selection:

```swift
struct SelectableTable: View {
    @State private var people: [Person] = [ /* ... */ ]
    @State private var selectedPeople = Set<Person.ID>()
    var body: some View {
        Table(people, selection: $selectedPeople) {
            TableColumn("Given Name", value: \.givenName)
            TableColumn("Family Name", value: \.familyName)
            TableColumn("E-Mail Address", value: \.emailAddress)
        }
        Text("\(selectedPeople.count) people selected")
    }
}
```

**Sorting** — `Table` does **not** sort data itself. Provide a binding to `[KeyPathComparator]` and re-sort in `.onChange(of:)`:

```swift
struct SortableTable: View {
    @State private var people: [Person] = [ /* ... */ ]
    @State private var sortOrder = [KeyPathComparator(\Person.givenName)]
    var body: some View {
        Table(people, sortOrder: $sortOrder) {
            TableColumn("Given Name", value: \.givenName)
            TableColumn("Family Name", value: \.familyName)
            TableColumn("E-Mail Address", value: \.emailAddress)
        }
        .onChange(of: sortOrder) { _, newOrder in people.sort(using: newOrder) }
    }
}
```

**Adaptive compact layout** — handle the collapsed first-column case by showing combined info, so the transition into/out of Slide Over feels seamless:

```swift
struct AdaptiveTable: View {
    @Environment(\.horizontalSizeClass) private var horizontalSizeClass
    private var isCompact: Bool { horizontalSizeClass == .compact }
    @State private var people: [Person] = [ /* ... */ ]
    @State private var sortOrder = [KeyPathComparator(\Person.givenName)]

    var body: some View {
        Table(people, sortOrder: $sortOrder) {
            TableColumn("Given Name", value: \.givenName) { person in
                VStack(alignment: .leading) {
                    Text(isCompact ? person.fullName : person.givenName)
                    if isCompact {
                        Text(person.emailAddress).foregroundStyle(.secondary)
                    }
                }
            }
            TableColumn("Family Name", value: \.familyName)
            TableColumn("E-Mail Address", value: \.emailAddress)
        }
        .onChange(of: sortOrder) { _, newOrder in people.sort(using: newOrder) }
    }
}
```

**Static rows** known at compile time use `init(of:columns:rows:)`:

```swift
struct Purchase: Identifiable {
    let price: Decimal
    let id = UUID()
}

struct TipTable: View {
    let currencyStyle = Decimal.FormatStyle.Currency(code: "USD")
    var body: some View {
        Table(of: Purchase.self) {
            TableColumn("Base price") { purchase in Text(purchase.price, format: currencyStyle) }
            TableColumn("With 15% tip") { purchase in Text(purchase.price * 1.15, format: currencyStyle) }
            TableColumn("With 20% tip") { purchase in Text(purchase.price * 1.2, format: currencyStyle) }
        } rows: {
            TableRow(Purchase(price: 20))
            TableRow(Purchase(price: 50))
            TableRow(Purchase(price: 75))
        }
    }
}
```

**Dynamic column count** (iOS 17.4+, macOS 14.4+, visionOS 1.1+) — use `TableColumnForEach` when columns aren't known at compile time; the element type must be `Identifiable` or you must supply an `id:` parameter. Mixable with static `TableColumn`s:

```swift
struct AudioChannel: Identifiable { let name: String; let id: UUID }
struct AudioSample: Identifiable {
    let id: UUID
    let timestamp: TimeInterval
    func level(channel: AudioChannel.ID) -> Double { 1 }
}

@Observable
class AudioSampleTrack {
    let channels: [AudioChannel]
    var samples: [AudioSample]
}

struct ContentView: View {
    var track: AudioSampleTrack
    var body: some View {
        Table(track.samples) {
            TableColumn("Timestamp (ms)") { sample in
                Text(sample.timestamp, format: .number.scale(1000)).monospacedDigit()
            }
            TableColumnForEach(track.channels) { channel in
                TableColumn(channel.name) { sample in
                    Text(sample.level(channel: channel.id),
                         format: .number.precision(.fractionLength(2)))
                        .monospacedDigit()
                }
                .width(ideal: 70)
                .alignment(.numeric)
            }
        }
    }
}
```

**Styles**: `.tableStyle(.inset)` (no borders); `.tableColumnHeaders(.hidden)` to hide headers.

| Platform | Behavior |
|---|---|
| iPadOS (regular) | Full multi-column layout; headers and all columns visible |
| iPadOS (compact) | Only the first column shown; headers hidden |
| iPhone (all sizes) | Only the first column shown; headers hidden; list-like appearance |

## Layout: `GeometryReader` Alternatives and Structural Performance

**Use dynamic layout over hard-coded constants.** Magic-number frames don't adapt to different screens, orientations, or dynamic content (e.g. a status bar appearing during a call):

```swift
// AVOID: doesn't adapt to different screens
VStack { HeaderView().frame(height: 150); ContentView() }

// PREFER: relative to actual available space
GeometryReader { geometry in
    VStack {
        HeaderView().frame(height: geometry.size.height * 0.2)
        ContentView()
    }
}
```

**Views should work in any context** — never assume a specific presentation style or screen size (full screen, modal, sheet, popover, or embedded). Never read `UIScreen.main.bounds`:

```swift
// AVOID: assumes full screen
struct ProfileCard: View {
    let user: User
    var body: some View {
        VStack {
            Image(user.avatar).frame(width: UIScreen.main.bounds.width)  // Wrong!
            Text(user.name)
        }
    }
}

// PREFER: adapts to whatever space it's given
struct ProfileCard: View {
    let user: User
    var body: some View {
        VStack {
            Image(user.avatar).resizable().aspectRatio(contentMode: .fit)
            Text(user.name)
            Spacer()
        }
        .padding()
    }
}
```

**Own your container: custom views should own static containers, but not lazy/repeatable ones.** A reusable view that renders a fixed group of children should wrap them in its own `HStack`/`VStack` — callers shouldn't have to. A view that renders a repeating collection should let the *caller* choose and own the lazy container (`LazyVStack`, `List`, etc.), since only the caller knows the surrounding scroll/layout context:

```swift
// GOOD: owns its own static container
struct HeaderView: View {
    var body: some View {
        HStack { Image(systemName: "star"); Text("Title"); Spacer() }
    }
}

// GOOD: the caller owns the lazy container
struct FeedView: View {
    let items: [Item]
    var body: some View {
        LazyVStack { ForEach(items) { item in ItemRow(item: item) } }
    }
}
```

### Avoiding layout thrash

Minimize deep view hierarchies and excessive layout dependencies — each extra nesting level is an extra layout pass:

```swift
// BAD: deep nesting, excessive layout passes
VStack { HStack { VStack { HStack { VStack { Text("Deep") } } } } }

// GOOD: flatter hierarchy
VStack { Text("Shallow"); Text("Structure") }
```

**Avoid excessive `GeometryReader` and preference-key chains** — nesting multiple `GeometryReader`s causes layout to recalculate multiple times:

```swift
// BAD
GeometryReader { outerGeometry in
    VStack { GeometryReader { innerGeometry in /* recalculates repeatedly */ } }
}

// GOOD: prefer a single GeometryReader, or an iOS 17+ alternative
containerRelativeFrame(.horizontal) { width, _ in width * 0.8 }
```

Never use `UIScreen.main.bounds` to read available space — prefer `containerRelativeFrame()`, `visualEffect()` where a geometry-driven visual effect (not a layout size) is the actual goal, or `GeometryReader` only when there is truly no alternative.

**Gate frequent geometry/preference updates by a threshold** rather than writing state on every pixel change:

```swift
// BAD: updates on every pixel change
.onPreferenceChange(ViewSizeKey.self) { size in currentSize = size }

// GOOD: only update on a meaningful change
.onPreferenceChange(ViewSizeKey.self) { size in
    let difference = abs(size.width - currentSize.width)
    if difference > 10 { currentSize = size }
}
```

**Full-width views**: use `.frame(maxWidth: .infinity, alignment:)` instead of an `HStack` + `Spacer` when a single view needs to fill available width — it's one modifier expressing intent, versus an extra container for no layout benefit:

```swift
// PREFER
Text("Hello").frame(maxWidth: .infinity, alignment: .leading)

// AVOID: unnecessary stack and spacer
HStack { Text("Hello"); Spacer() }
```

### `ZStack` vs. `overlay`/`background`

Use `ZStack` to **compose multiple peer views** that should be layered together and jointly define layout. Prefer `overlay`/`background` when **decorating a primary view** — not just because they don't affect layout size, but because they express intent and keep the modified view as the clear layout anchor.

Key difference in size-proposal behavior: in `overlay`/`background`, the child implicitly adopts the size proposed to the parent when it has no size of its own, which feels natural for decorative attachments. In a `ZStack`, each child participates independently in layout with no implicit size inheritance — better for peer composition, less intuitive for simple decoration. Reach for `ZStack` (or another container) when the "decoration" must explicitly participate in layout sizing — reserving space, extending tappable/visible bounds, or preventing overlap with neighbors.

```swift
// GOOD: decoration via overlay — layout stays anchored to the button
Button("Continue") { }
    .overlay(alignment: .trailing) { Image(systemName: "lock.fill").padding(.trailing, 8) }

// BAD: ZStack when overlay suffices — layout no longer anchored to the button
ZStack(alignment: .trailing) {
    Button("Continue") { }
    Image(systemName: "lock.fill").padding(.trailing, 8)
}

// GOOD: a decorative background shape takes the parent's size
HStack(spacing: 12) { Text("Inbox"); Text("Next") }
    .background { Capsule().strokeBorder(.blue, lineWidth: 2) }
```

### `.compositingGroup()` before `.clipShape()`

**Always add `.compositingGroup()` before `.clipShape()` when clipping layered views** (`.overlay`/`.background`). Without it, each layer is antialiased separately before compositing, producing visible color fringes where antialiased edges overlap — typically at rounded corners:

```swift
let shape = RoundedRectangle(cornerRadius: 16)

// BAD: each layer antialiased separately — color fringes at corners
Color.red.overlay(.white, in: shape).clipShape(shape).frame(width: 200, height: 150)

// GOOD: layers composited first; antialiasing applied once, at the clip
Color.red
    .overlay(.white, in: .rect)
    .compositingGroup()
    .clipShape(shape)
    .frame(width: 200, height: 150)
```

`.compositingGroup()` forces child layers into a single offscreen buffer before the clip is applied, so antialiasing happens once on the final composited result.

### Lazy containers for large data sets

Prefer `LazyVStack`/`LazyHStack`/`LazyVGrid`/`LazyHGrid` when rendering large collections — they load views only as they approach the viewport, reducing memory and improving scroll performance:

```swift
struct ContentView: View {
    let items = Array(0..<1000)
    var body: some View {
        ScrollView {
            LazyVStack { ForEach(items, id: \.self) { item in Text("Item \(item)") } }
        }
    }
}
```

Favor lazy containers when: rendering large collections; row views are non-trivial; memory usage matters; the content lives inside a `ScrollView`.

## Navigation, Sheets, Alerts, and Inspector

### `NavigationStack` and type-safe destinations

Register `navigationDestination(for:)` **once per data type**; flag duplicate registrations. Never mix `navigationDestination(for:)` and the old `NavigationLink(destination:)` pattern in the same navigation hierarchy — it causes significant, hard-to-diagnose problems. `NavigationView` is deprecated; flag any remaining use.

```swift
struct ContentView: View {
    var body: some View {
        NavigationStack {
            List {
                NavigationLink("Profile", value: Route.profile)
                NavigationLink("Settings", value: Route.settings)
            }
            .navigationDestination(for: Route.self) { route in
                switch route {
                case .profile: ProfileView()
                case .settings: SettingsView()
                }
            }
        }
    }
}

enum Route: Hashable { case profile, settings }
```

**Programmatic navigation** with `NavigationPath`:

```swift
struct ContentView: View {
    @State private var navigationPath = NavigationPath()
    var body: some View {
        NavigationStack(path: $navigationPath) {
            List {
                Button("Go to Detail") { navigationPath.append(DetailRoute.item(id: 1)) }
            }
            .navigationDestination(for: DetailRoute.self) { route in
                switch route { case .item(let id): ItemDetailView(id: id) }
            }
        }
    }
}

enum DetailRoute: Hashable { case item(id: Int) }
```

### `NavigationSplitView` (iOS 16+, macOS 13+, tvOS 16+, watchOS 9+)

Two-column, sidebar-driven layout:

```swift
struct ContentView: View {
    @State private var selectedItem: Item.ID?
    var body: some View {
        NavigationSplitView {
            List(items, selection: $selectedItem) { item in Text(item.name) }
                .navigationTitle("Items")
        } detail: {
            if let selectedItem, let item = items.first(where: { $0.id == selectedItem }) {
                ItemDetailView(item: item)
            } else {
                ContentUnavailableView("Select an Item", systemImage: "doc")
            }
        }
    }
}
```

Three-column layout adds a `content:` closure between sidebar and detail:

```swift
struct ContentView: View {
    @State private var departmentId: Department.ID?
    @State private var employeeIds = Set<Employee.ID>()
    var body: some View {
        NavigationSplitView {
            List(model.departments, selection: $departmentId) { dept in Text(dept.name) }
        } content: {
            if let department = model.department(id: departmentId) {
                List(department.employees, selection: $employeeIds) { emp in Text(emp.name) }
            } else {
                Text("Select a department")
            }
        } detail: {
            EmployeeDetails(for: employeeIds)
        }
    }
}
```

Configuration: `NavigationSplitView(columnVisibility: $visibility)` with `NavigationSplitViewVisibility` (`.detailOnly`, `.doubleColumn`, `.all`); `.navigationSplitViewColumnWidth(min:ideal:max:)` per column; `NavigationSplitView(preferredCompactColumn: $column)` to control which column shows on narrow devices; `.navigationSplitViewStyle(.balanced)` or `.prominentDetail` (default).

| Platform | Behavior |
|---|---|
| macOS | Columns always visible side-by-side; translucent sidebar; variable-width resize by dragging |
| iPadOS (regular) | Sidebar can overlay or push detail; column-visibility toggle in toolbar |
| iOS / iPadOS (compact) | Collapses into a single `NavigationStack`; sidebar rows show disclosure chevrons; back button navigates columns |
| iPhone (all sizes) | Always collapsed into a stack; sidebar is the root list; selection pushes detail |
| watchOS / tvOS | Collapses into a single stack |

### Inspector (iOS 17+, macOS 14+)

A trailing-edge panel for supplementary information. Appears as a **trailing column** on wider size classes (macOS, iPad landscape); **adapts to a sheet** automatically on compact size classes (iPhone).

```swift
struct ShapeEditor: View {
    @State private var showInspector = false
    var body: some View {
        MyEditorView()
            .inspector(isPresented: $showInspector) { InspectorContent() }
            .toolbar {
                ToolbarItem {
                    Button { showInspector.toggle() } label: { Label("Inspector", systemImage: "info.circle") }
                }
            }
    }
}

// Column width
MyEditorView()
    .inspector(isPresented: $showInspector) {
        InspectorContent().inspectorColumnWidth(min: 200, ideal: 250, max: 400)
    }

// Fixed width
MyEditorView()
    .inspector(isPresented: $showInspector) { InspectorContent().inspectorColumnWidth(300) }
```

| Platform | Behavior |
|---|---|
| macOS | Trailing-edge sidebar panel; resizable by dragging edge; integrates with window toolbar |
| iPadOS (regular) | Trailing column alongside content; toggleable via toolbar button |
| iOS / iPadOS (compact) | Adapts to a sheet; swipe-to-dismiss supported |
| iPhone (all sizes) | Always a sheet (no trailing column) |

Use `InspectorCommands` in the app's `.commands` to include the default inspector-toggle keyboard shortcut.

### Sheets

**Prefer `.sheet(item:)` over `.sheet(isPresented:)`** when presenting model-based content — it eliminates a second boolean flag and avoids optional-unwrapping inside the sheet body:

```swift
// GOOD: item-driven
@State private var selectedItem: Item?
List(items) { item in Button(item.name) { selectedItem = item } }
    .sheet(item: $selectedItem) { item in ItemDetailSheet(item: item) }

// AVOID: boolean flag requires separate state and an optional unwrap inside
@State private var showSheet = false
@State private var selectedItem: Item?
List(items) { item in Button(item.name) { selectedItem = item; showSheet = true } }
    .sheet(isPresented: $showSheet) {
        if let selectedItem { ItemDetailSheet(item: selectedItem) }
    }
```

When a sheet's view accepts the item as its only initializer parameter, prefer the initializer reference over a closure: `sheet(item: $someItem, content: SomeView.init)` over `sheet(item: $someItem) { someItem in SomeView(item: someItem) }`.

**Sheets should own their dismiss and actions internally** via `@Environment(\.dismiss)`. Avoid `onSave`/`onCancel` closures passed from the parent — that's callback prop-drilling and reduces reusability:

```swift
struct EditItemSheet: View {
    @Environment(\.dismiss) private var dismiss
    let item: Item
    @State private var name: String

    init(item: Item) {
        self.item = item
        _name = State(initialValue: item.name)
    }

    var body: some View {
        NavigationStack {
            Form { TextField("Name", text: $name) }
                .navigationTitle("Edit Item")
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                    ToolbarItem(placement: .confirmationAction) { Button("Save") { /* save and dismiss */ } }
                }
        }
    }
}
```

**Enum-based sheet management** when a screen presents several different sheets — one `@State` and one `.sheet(item:)` replaces N booleans and N sheet modifiers, and prevents only-one-sheet-at-a-time conflicts:

```swift
struct ArticlesView: View {
    enum Sheet: Identifiable {
        case add, edit(Article), categories
        var id: String {
            switch self {
            case .add: "add"
            case .edit(let a): "edit-\(a.id)"
            case .categories: "categories"
            }
        }
    }

    @State private var presentedSheet: Sheet?

    var body: some View {
        List { /* ... */ }
            .toolbar { Button("Add") { presentedSheet = .add } }
            .sheet(item: $presentedSheet) { sheet in
                switch sheet {
                case .add: AddArticleView()
                case .edit(let article): EditArticleView(article: article)
                case .categories: CategoriesView()
                }
            }
    }
}
```

### Alerts and confirmation dialogs

Always attach `.confirmationDialog()` to the UI element that triggers it — this lets Liquid Glass animations originate from the correct source. If an alert has only a single "OK" button that does nothing but dismiss, omit the button entirely: `.alert("Dismiss Me", isPresented: $isShowingAlert) { }`.

### Full screen cover and popover

```swift
struct ContentView: View {
    @State private var showFullScreen = false
    var body: some View {
        Button("Show Full Screen") { showFullScreen = true }
            .fullScreenCover(isPresented: $showFullScreen) { FullScreenView() }
    }
}

struct ContentView: View {
    @State private var showPopover = false
    var body: some View {
        Button("Show Popover") { showPopover = true }
            .popover(isPresented: $showPopover) {
                PopoverContentView()
                    .presentationCompactAdaptation(.popover)  // don't adapt to a sheet on iPhone
            }
    }
}
```

### `TabView` selection type

When using `TabView(selection:)`, bind to a property that stores an **enum**, not an integer or string: `Tab("Home", systemImage: "house", value: .home)` is preferred over `Tab("Home", systemImage: "house", value: 0)` — it's exhaustive and immune to reordering bugs.

## ScrollView: Programmatic Scrolling and Effects

### `ScrollViewReader`

Use for scroll-to-top, scroll-to-bottom, and anchor-based jumps. Always pair `scrollTo()` with an explicit `withAnimation` and a stable id:

```swift
struct ChatView: View {
    @State private var messages: [Message] = []
    private let bottomID = "bottom"

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack {
                    ForEach(messages) { message in
                        MessageRow(message: message).id(message.id)
                    }
                    Color.clear.frame(height: 1).id(bottomID)
                }
            }
            .onChange(of: messages.count) { _, _ in
                withAnimation { proxy.scrollTo(bottomID, anchor: .bottom) }
            }
            .onAppear { proxy.scrollTo(bottomID, anchor: .bottom) }
        }
    }
}
```

Scroll-to-top pattern (a marker view at the head of the stack, triggered by a boolean):

```swift
struct FeedView: View {
    @State private var items: [Item] = []
    @State private var scrollToTop = false
    private let topID = "top"

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack {
                    Color.clear.frame(height: 1).id(topID)
                    ForEach(items) { item in ItemRow(item: item) }
                }
            }
            .onChange(of: scrollToTop) { _, shouldScroll in
                if shouldScroll {
                    withAnimation { proxy.scrollTo(topID, anchor: .top) }
                    scrollToTop = false
                }
            }
        }
    }
}
```

### Scroll position tracking

Storing a raw scroll offset directly in `@State` triggers a view update on every scroll frame:

```swift
// AVOID: causes unnecessary re-renders on every scroll frame
struct ContentView: View {
    @State private var scrollPosition: CGFloat = 0
    var body: some View {
        ScrollView {
            content.background(
                GeometryReader { geometry in
                    Color.clear.preference(
                        key: ScrollOffsetPreferenceKey.self,
                        value: geometry.frame(in: .named("scroll")).minY
                    )
                }
            )
        }
        .coordinateSpace(name: "scroll")
        .onPreferenceChange(ScrollOffsetPreferenceKey.self) { value in scrollPosition = value }
    }
}
```

Prefer checking the position against a threshold and updating a flag, not the raw value, for smoother and cheaper scrolling:

```swift
struct ContentView: View {
    @State private var startAnimation: Bool = false
    var body: some View {
        ScrollView {
            content.background(
                GeometryReader { geometry in
                    Color.clear.preference(
                        key: ScrollOffsetPreferenceKey.self,
                        value: geometry.frame(in: .named("scroll")).minY
                    )
                }
            )
        }
        .coordinateSpace(name: "scroll")
        .onPreferenceChange(ScrollOffsetPreferenceKey.self) { value in
            startAnimation = value < -100
        }
    }
}

struct ScrollOffsetPreferenceKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = nextValue() }
}
```

Scroll-based header visibility follows the same threshold-gating shape:

```swift
struct ContentView: View {
    @State private var showHeader = true
    var body: some View {
        VStack(spacing: 0) {
            if showHeader { HeaderView().transition(.move(edge: .top)) }
            ScrollView {
                content.background(
                    GeometryReader { geometry in
                        Color.clear.preference(
                            key: ScrollOffsetPreferenceKey.self,
                            value: geometry.frame(in: .named("scroll")).minY
                        )
                    }
                )
            }
            .coordinateSpace(name: "scroll")
            .onPreferenceChange(ScrollOffsetPreferenceKey.self) { offset in
                if offset < -50 { withAnimation { showHeader = false } }
                else if offset > 50 { withAnimation { showHeader = true } }
            }
        }
    }
}
```

### Scroll transitions and effects (iOS 17+)

`.visualEffect` reads live scroll geometry without triggering a state write, so it's cheap even at 120fps:

```swift
// Scroll-based opacity
ScrollView {
    LazyVStack(spacing: 20) {
        ForEach(items) { item in
            ItemCard(item: item)
                .visualEffect { content, geometry in
                    let frame = geometry.frame(in: .scrollView)
                    let distance = min(0, frame.minY)
                    return content.opacity(1 + distance / 200)
                }
        }
    }
}

// Parallax header
ScrollView {
    VStack(spacing: 0) {
        Image("hero")
            .resizable()
            .aspectRatio(contentMode: .fill)
            .frame(height: 300)
            .visualEffect { content, geometry in
                let offset = geometry.frame(in: .scrollView).minY
                return content.offset(y: offset > 0 ? -offset * 0.5 : 0)
            }
            .clipped()
        ContentView()
    }
}
```

### Scroll target behavior (iOS 17+)

```swift
// Paging
ScrollView(.horizontal) {
    LazyHStack(spacing: 0) {
        ForEach(pages) { page in PageView(page: page).containerRelativeFrame(.horizontal) }
    }
    .scrollTargetLayout()
}
.scrollTargetBehavior(.paging)

// Snap to items
ScrollView(.horizontal) {
    LazyHStack(spacing: 16) {
        ForEach(items) { item in ItemCard(item: item).frame(width: 280) }
    }
    .scrollTargetLayout()
}
.scrollTargetBehavior(.viewAligned)
.contentMargins(.horizontal, 20)
```

## Focus: `FocusState`, Focusable Views, and Focused Values

### `@FocusState`

Always mark `@FocusState` `private`. Use `Bool` for a single field, an optional `Hashable` enum for multiple fields.

```swift
// Single field
@FocusState private var isFocused: Bool
TextField("Email", text: $email).focused($isFocused)

// Multiple fields
enum Field: Hashable { case name, email, password }
@FocusState private var focusedField: Field?
TextField("Name", text: $name).focused($focusedField, equals: .name)
TextField("Email", text: $email).focused($focusedField, equals: .email)
```

Set `focusedField = .email` to move focus programmatically; set `nil` to dismiss the keyboard.

**`.focused($bool)` vs `.focused($enum, equals:)` with nested views**: `.focused($bool)` reports `true` when the modified view *or any focusable descendant* has focus. `.focused($enum, equals:)` reports its value only when that specific view receives focus:

```swift
enum Focus: Hashable { case container, field }
@FocusState private var focus: Focus?

VStack {
    TextField("Name", text: $name).focused($focus, equals: .field)
}
.focusable()
.focused($focus, equals: .container)
```

With `focused(_:equals:)` and a single `@FocusState`, SwiftUI distinguishes the container *receiving* focus from the container merely *containing* focus.

The **`isFocused` environment value** is read-only and returns `true` when the nearest focusable ancestor has focus — useful for styling non-focusable child views:

```swift
struct HighlightWrapper: View {
    @Environment(\.isFocused) private var isFocused
    var body: some View {
        content.background(isFocused ? Color.accentColor.opacity(0.1) : .clear)
    }
}
```

### Making views focusable

`.focusable(_:)` makes a non-text-input view participate in the focus system so it can respond to `onKeyPress` and menu commands like Edit > Delete via `onDeleteCommand`:

```swift
struct SelectableCard: View {
    @FocusState private var isFocused: Bool
    var body: some View {
        CardContent()
            .focusable()
            .focused($isFocused)
            .border(isFocused ? Color.accentColor : .clear)
            .onDeleteCommand { deleteCard() }
    }
}
```

`.focusable(interactions:)` (iOS 17+) controls which focus-driven interactions are supported, via `FocusInteractions`: `.activate` (button-like — only focusable when system-wide keyboard navigation is on), `.edit` (captures keyboard/Digital Crown input), `.automatic` (platform default, both). Use `.activate` for custom button-like views that should match system keyboard-navigation behavior.

`TextField` and `SecureField` are implicitly focusable; custom views (stacks, shapes, images) are not — forgetting `.focusable()` means `.focused()` bindings have no effect and key-event handlers never fire.

### Focused values for commands and menus

Focused values let parent views (App, Scene, Commands) read state from whichever view currently has focus — the mechanism for enabling/disabling menu commands based on the focused document or selection.

```swift
// Declare with @Entry
extension FocusedValues {
    @Entry var selectedDocument: Binding<Document>?
}

// Publish from a view (view-scoped) or a scene (scene-scoped)
.focusedValue(\.selectedDocument, $document)
.focusedSceneValue(\.selectedDocument, $document)

// Consume in commands: @FocusedValue reads the value;
// @FocusedBinding unwraps a Binding automatically.
@main
struct MyApp: App {
    @FocusedBinding(\.selectedDocument) var document

    var body: some Scene {
        WindowGroup { ContentView() }
            .commands {
                CommandGroup(after: .pasteboard) {
                    Button("Duplicate") { document?.duplicate() }.disabled(document == nil)
                }
            }
    }
}
```

Focused values are typically optional (default `nil` when no view publishes them), but a non-optional entry with a sensible default is fine too.

`@FocusedObject` (iOS 16+) is the equivalent for `ObservableObject` types — the view invalidates when the focused object changes. Publish with `.focusedObject(myObservableModel)`, consume with `@FocusedObject var model: MyModel?`. Scene-scoped variant: `.focusedSceneObject(_:)`.

### Default focus

Prefer `.defaultFocus(_:_:priority:)` (iOS 17+, macOS 13+, tvOS 16+) over setting `@FocusState` in `.onAppear` for initial focus placement:

```swift
@FocusState private var focusedField: Field?

VStack {
    TextField("Name", text: $name).focused($focusedField, equals: .name)
    TextField("Email", text: $email).focused($focusedField, equals: .email)
}
.defaultFocus($focusedField, .email)
```

`priority`: `.automatic` (default) applies on window appearance and programmatic focus changes; `.userInitiated` also applies during user-driven focus navigation.

Related, platform-specific APIs: `prefersDefaultFocus(_:in:)` (macOS/tvOS/watchOS) marks a preferred target within a `.focusScope(_:)`-scoped region; the `resetFocus` environment action re-evaluates default focus within a namespace:

```swift
@Namespace var scopeID
@Environment(\.resetFocus) private var resetFocus
Button("Reset") { resetFocus(in: scopeID) }
```

### Focus scope and sections

`.focusScope(_:)` (macOS/tvOS/watchOS) limits default-focus preferences to a namespace; use with `prefersDefaultFocus`/`resetFocus`.

`.focusSection()` (macOS 13+, tvOS 15+) guides directional/sequential focus movement through a group of focusable descendants — useful when spatially separated focusable views would otherwise be skipped by directional navigation:

```swift
HStack {
    VStack { Button("1") {}; Button("2") {}; Spacer() }
    Spacer()
    VStack { Spacer(); Button("A") {}; Button("B") {} }
        .focusSection()
}
```

Without `.focusSection()`, swiping right from buttons 1/2 finds nothing; with it, the `VStack` receives directional focus and delivers it to its first focusable child.

### Focus effects and search focus

`.focusEffectDisabled(_:)` suppresses the system focus ring (macOS) or hover effect — use when providing custom focus visuals; `isFocusEffectEnabled` reads the current state:

```swift
MyCustomCard().focusable().focusEffectDisabled().overlay { customFocusRing }
```

`.searchFocused(_:)` / `.searchFocused(_:equals:)` bind focus to the search field associated with the nearest `.searchable` modifier, working like `.focused` but targeting the search bar:

```swift
@FocusState private var isSearchFocused: Bool
NavigationStack {
    ContentView().searchable(text: $query).searchFocused($isSearchFocused)
}
Button("Search") { isSearchFocused = true }  // programmatically focus the search bar
```

### Common focus pitfalls

- **Redundant `@FocusState` writes revoke focus.** `.focusable()` + `.focused()` already handles focus-on-click. A tap gesture that *also* writes to `@FocusState` triggers a second body evaluation that revokes focus — focus briefly appears then disappears, and `onDeleteCommand` stops working:

  ```swift
  // WRONG — remove the redundant write
  CardView().focusable().focused($isFocused).onTapGesture { isFocused = true }

  // CORRECT
  CardView().focusable().focused($isFocused)
  ```

- **Ambiguous focus bindings.** Binding the same enum case to multiple views is ambiguous — SwiftUI picks the first candidate and emits a runtime warning. Always use distinct cases per focusable view.
- **`.onAppear` focus timing.** Setting `@FocusState` in `.onAppear` can fail if the view tree hasn't settled. Prefer `.defaultFocus` (iOS 17+); if `.onAppear` is unavoidable, wrapping in `DispatchQueue.main.async` is a last resort. The same applies inside `#Preview` — prefer `.defaultFocus` over an `.onAppear` write when seeding initial focus for a preview.
- **Missing `.focusable()` on non-text views** silently no-ops `.focused()` bindings and key handlers.

## Text: Verbatim vs. Localized, and Input Controls

### Text initializer selection

**Default: always use `Text("…")`.** Use `Text(verbatim:)` only when a string literal must explicitly not be localized.

```swift
// Localized literal — "Save" is the localization key, looked up in
// Localizable.strings (only if one exists in the project)
Text("Save")

// String variable — bypasses localization automatically; no verbatim needed
let filename: String = model.exportFilename
Text(filename)

// Non-localized literal — verbatim only when the literal must not be localized
Text(verbatim: "pencil")
```

Decision flow:

```
Is the input a String variable or dynamic value?
└─ YES → Text(variable)          // bypasses localization automatically

Is the string literal intended for localization?
├─ YES → Text("…")               // default; key looked up in Localizable.strings
└─ NO  → Text(verbatim: "…")     // only when explicitly non-localized
```

For String Catalogs, `#bundle` in packages, `LocalizedStringResource`, locale-aware formatting, RTL layout, and translator comments, see the `ios-localization` skill — this section covers only the verbatim-vs-localized decision for a single `Text`.

### Text input controls

Unless a full-screen editing experience is required, prefer `TextField` with `axis: .vertical` over `TextEditor` — it allows placeholder text, which `TextEditor` does not support. If a specific minimum height is required, use `.lineLimit(5...)` rather than a fixed frame.

## Previews: `#Preview`, `@Previewable`, and Mock Data

### Basic usage and multiple states

`#Preview` (Swift 5.9+, Xcode 15+) is the modern way to declare previews; `PreviewProvider` still works but is more verbose — prefer `#Preview` for new code.

```swift
#Preview {
    ContentView()
}

#Preview("Dark Mode") {
    ContentView().preferredColorScheme(.dark)
}
```

Declare one `#Preview` per meaningful state so each renders independently in the canvas:

```swift
#Preview("Default") { SettingsRow(title: "Notifications", isOn: true) }
#Preview("Off State") { SettingsRow(title: "Notifications", isOn: false) }
#Preview("Long Title") { SettingsRow(title: "Enable Push Notifications for All Events", isOn: true) }
```

**Traits** configure the preview environment without modifying the view:

```swift
#Preview(traits: .fixedLayout(width: 300, height: 100)) { CompactBanner(message: "Welcome") }
#Preview(traits: .sizeThatFitsLayout) { BadgeView(count: 5) }
#Preview(traits: .landscapeLeft) { DashboardView() }
```

Wrap previewed destinations in their navigation container so toolbar items, titles, and back buttons render correctly:

```swift
#Preview {
    NavigationStack { DetailView(item: .sample) }
}
```

### Mock data

Previews must compile and render without external dependencies — live services, network calls, and disk I/O make them slow, flaky, or broken. Expose sample values as static properties on the model itself so any preview reuses them without reconstructing inline:

```swift
struct Item: Identifiable {
    let id: UUID
    var name: String
    var price: Double
}

extension Item {
    static let sample = Item(id: UUID(), name: "Widget", price: 9.99)
    static let samples: [Item] = [
        Item(id: UUID(), name: "Widget", price: 9.99),
        Item(id: UUID(), name: "Gadget", price: 19.99),
        Item(id: UUID(), name: "Doohickey", price: 4.99),
    ]
}

#Preview { ItemListView(items: Item.samples) }
```

For views driven by an `@Observable` model, expose pre-configured instances on the model (see `dataflow.md` for `@Observable` fundamentals):

```swift
@MainActor @Observable
final class CartModel {
    var items: [Item] = []
    var isLoading = false

    static var preview: CartModel { let m = CartModel(); m.items = Item.samples; return m }
    static var emptyPreview: CartModel { CartModel() }
    static var loadingPreview: CartModel { let m = CartModel(); m.isLoading = true; return m }
}

#Preview("With Items") { CartView().environment(CartModel.preview) }
#Preview("Empty") { CartView().environment(CartModel.emptyPreview) }
#Preview("Loading") { CartView().environment(CartModel.loadingPreview) }
```

Inject any environment values the view depends on so the preview reflects a realistic runtime context:

```swift
#Preview {
    OrderDetailView(order: .sample)
        .environment(CartModel.preview)
        .environment(\.locale, Locale(identifier: "ja_JP"))
        .environment(\.dynamicTypeSize, .xxxLarge)
}
```

When a view depends on a network or data service, give the dependency a protocol abstraction so previews can inject a synchronous mock returning sample data immediately (adapt to whatever DI pattern the codebase already uses):

```swift
protocol DataFetching { func fetchItems() async throws -> [Item] }

struct LiveDataFetcher: DataFetching {
    let url: URL
    func fetchItems() async throws -> [Item] {
        let (data, _) = try await URLSession.shared.data(from: url)
        return try JSONDecoder().decode([Item].self, from: data)
    }
}

struct MockDataFetcher: DataFetching {
    var result: Result<[Item], Error> = .success(Item.samples)
    func fetchItems() async throws -> [Item] { try result.get() }
}

#Preview { ItemListView(fetcher: MockDataFetcher()) }
#Preview("Error State") {
    ItemListView(fetcher: MockDataFetcher(result: .failure(URLError(.notConnectedToInternet))))
}
```

### `@Previewable` (iOS 18+, Xcode 16+)

Lets `@State`, `@FocusState`, and other property wrappers be used directly inside a `#Preview` block, removing the need for a wrapper view to host interactive state:

```swift
// With @Previewable — no wrapper view needed
#Preview {
    @Previewable @State var isOn = false
    Toggle("Notifications", isOn: $isOn)
}

// Without @Previewable — requires a wrapper view
struct TogglePreviewWrapper: View {
    @State private var isOn = false
    var body: some View { Toggle("Notifications", isOn: $isOn) }
}
#Preview { TogglePreviewWrapper() }
```

Multiple interactive controls in one preview:

```swift
#Preview {
    @Previewable @State var name = "Alice"
    @Previewable @State var age = 25.0

    VStack {
        TextField("Name", text: $name)
        Slider(value: $age, in: 0...100, step: 1) { Text("Age: \(Int(age))") }
        Text("Hello, \(name)! Age: \(Int(age))")
    }
    .padding()
}
```

`@Previewable @FocusState` combined with `.defaultFocus` (rather than an `.onAppear` write, which can race the initial render):

```swift
#Preview {
    @Previewable @FocusState var isFocused: Bool
    TextField("Search", text: .constant(""))
        .focused($isFocused)
        .defaultFocus($isFocused, true)
}
```

If the project's minimum deployment target is below iOS 18, `@Previewable` is unavailable — fall back to a wrapper view:

```swift
private struct SliderPreview: View {
    @State private var value = 0.5
    var body: some View { CustomSlider(value: $value) }
}
#Preview { SliderPreview() }
```

### Common preview diagnostics

| Symptom | Cause | Fix |
|---|---|---|
| `#Preview` body type mismatch | The closure's final expression isn't a `View` | Make sure the last expression conforms to `View` |
| `@Previewable` only available in iOS 18+ | Deployment target below iOS 18 | Use a wrapper view, or gate with `#available` |
| Preview crashes with "missing environment" | An `@Environment(SomeType.self)` value isn't injected | Add `.environment(SomeType.preview)` |
| Preview hangs or renders blank | View depends on async data that never resolves | Inject a mock that returns immediately |
| `@MainActor`-isolated model accessed off the main actor | A preview helper touches main-actor-only API from a non-isolated context | Mark the helper or preview body `@MainActor` |

## Apple HIG-Aligned Design Guidance

### Centralize design constants

Place standard fonts, sizes, colors, stack spacing, padding, corner radii, and animation timings into a shared enum of constants used by every view. This keeps an app's design uniform and lets it be adjusted from one place.

### Flexible, accessible layout

- Never read `UIScreen.main.bounds` for available space; prefer `containerRelativeFrame()`, `visualEffect()` where appropriate, or `GeometryReader` only when there's truly no alternative (see the Layout section above for the full rationale).
- Avoid fixed frames unless content is guaranteed to fit neatly inside them — fixed sizes break across device sizes and Dynamic Type settings. Give frames flexibility (`maxWidth`/`maxHeight`, not `width`/`height`) by default.
- Apple's minimum tap target on iOS is 44×44pt — enforce it strictly for every interactive control.

For the full VoiceOver/Dynamic Type/contrast audit surface (labels, traits, Dynamic Type scaling, reduce-motion, contrast), see the `ios-accessibility` skill — this section covers only the view-construction-level layout rules that support it.

### Standard system styling

- Prefer `ContentUnavailableView` over a custom design whenever data is missing or empty.
- With `.searchable()`, `ContentUnavailableView.search` auto-includes the user's search term — don't pass `text: searchText` yourself.
- Prefer `Label` over a manual `HStack` when pairing an icon with text side by side.
- Prefer system hierarchical styles (`.secondary`, `.tertiary`) over manual `.opacity()` so the system can adapt to the current context automatically.
- Inside `Form`, wrap controls like `Slider` in `LabeledContent` so the title and control lay out correctly. `LabeledContent` also works outside `Form` for any title-value display; define a custom `LabeledContentStyle` for consistent layout across views if needed.
- `RoundedRectangle`'s default rounding style is `.continuous` — no need to specify it explicitly.

### Typography and color

- Use `.bold()` instead of `.fontWeight(.bold)` — `bold()` lets the system choose the correct weight for the current context (Dynamic Type, accessibility bold text, etc.).
- Only reach for `.fontWeight()` at weights other than bold when there's a specific reason; scattering `.fontWeight(.medium)`/`.fontWeight(.semibold)` around a codebase is counterproductive.
- Avoid hard-coded padding/spacing values unless specifically requested — pull from the shared design constants instead.
- Avoid `UIColor` in SwiftUI code; use SwiftUI `Color` or asset-catalog colors.
- `.caption2` is extremely small and best avoided; even `.caption` is on the small side and should be used carefully.

For localized string formatting, plural rules, and translator workflow, see the `ios-localization` skill; the `Text` initializer mechanics above are the only localization-adjacent content that belongs here.
