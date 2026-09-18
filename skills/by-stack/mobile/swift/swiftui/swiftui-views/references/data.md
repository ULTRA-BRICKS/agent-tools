# Data & State Flow

How data flows through a SwiftUI app determines which views invalidate and when. `@State` owns view-local state. `@Observable` model objects carry data that's shared across a subtree, with per-property tracking that scopes invalidation to the exact views that read what changed. `Binding` lets a child edit state owned by a parent. `@Environment` and `@FocusedValue` propagate values implicitly down a subtree. This reference covers what shape of data to hand each view, which property wrapper to reach for, how to set up `@Observable` models so views invalidate as narrowly as possible, and the environment-specific performance traps.

## Property wrapper selection

| Wrapper | Use when | Notes |
|---|---|---|
| `@State` | View-local state, or a view *owns* an `@Observable` model it created | Always `private`. For an owned `@Observable` model, `@State` (not `let`) is required — without it SwiftUI may recreate the instance on a parent redraw, losing accumulated state, and `@State` also provides bindings directly (no need for `@Bindable`). |
| `@Binding` | Child needs to **modify** a parent's state | Don't use for read-only access — it adds overhead and implies a write contract that doesn't exist. |
| `@Bindable` | View receives an `@Observable` object from outside and needs bindings into it | For an object the view doesn't own. |
| `let` | Read-only value passed from a parent | Simplest option; use for any value the child only displays. |
| `var` | Read-only value the view reacts to via `.onChange(of:)` | Only when the view needs to observe the transition, not just display the current value. |
| `@Environment` / `@Observable` via `.environment()` | Value provided implicitly by an ancestor | See "Environment & focused values" below for performance pitfalls. |

**Legacy (pre-iOS 17), avoid in new code:**

| Wrapper | Use when | Notes |
|---|---|---|
| `@StateObject` | View owns an `ObservableObject` instance | Prefer `@State` with `@Observable` instead. |
| `@ObservedObject` | View receives an `ObservableObject` from outside | Never construct one inline — it recreates the instance on every view update. Prefer `@Observable` + `@Bindable`/`let`. |
| `@EnvironmentObject` | `ObservableObject` injected via `.environmentObject()` | Prefer `@Observable` + `.environment(_:)` / `@Environment(Model.self)`. |

## Owning state: @State

Always mark `@State` properties `private`. If you encounter one that already has a different access level, recommend changing it to `private` but don't change it yourself (to avoid breaking the build) unless instructed to. Marking state `private` makes clear what's created by the view versus what's passed in, and prevents it from appearing in the generated initializer — which prevents the "don't pass values as @State" bug below.

```swift
@State private var isAnimating = false
@State private var selectedTab = 0
```

### Owning an @Observable model

Use `@Observable` (not `ObservableObject`) for classes that provide data to views, and own it with `@State`, not `@StateObject`:

```swift
@Observable
@MainActor  // always mark @Observable classes with @MainActor — see below
final class DataModel {
    var name = "Some Name"
    var count = 0
}

struct MyView: View {
    @State private var model = DataModel()  // @State, not @StateObject

    var body: some View {
        VStack {
            TextField("Name", text: $model.name)
            Stepper("Count: \(model.count)", value: $model.count)
        }
    }
}
```

### Never declare passed-in values as @State

`@State` and `@StateObject` only accept an *initial* value from their default expression; they ignore subsequent updates from a parent. Declaring a passed-in value this way silently freezes it at whatever it was on first appearance.

```swift
// WRONG - child ignores parent updates after the first render
struct ChildView: View {
    @State var item: Item  // shows the initial value forever
    var body: some View { Text(item.name) }
}

// CORRECT - child receives updates
struct ChildView: View {
    let item: Item  // or @Binding if the child needs to modify it
    var body: some View { Text(item.name) }
}
```

Keeping `@State`/`@StateObject` `private` is the mechanical guard: a private property wrapper can't appear in the memberwise initializer, so a caller can never try to pass a value into it in the first place.

### let vs var for passed-in values

Use `let` for values the view only displays:

```swift
struct ProfileHeader: View {
    let username: String
    let avatarURL: URL

    var body: some View {
        HStack {
            AsyncImage(url: avatarURL)
            Text(username)
        }
    }
}
```

Use `var` only when the view needs to react to the value's *transitions* via `.onChange(of:)`, not just render its current value:

```swift
struct ReactiveView: View {
    var externalValue: Int  // watched with .onChange()
    @State private var displayText = ""

    var body: some View {
        Text(displayText)
            .onChange(of: externalValue) { oldValue, newValue in
                displayText = "Changed from \(oldValue) to \(newValue)"
            }
    }
}
```

## Modifying parent state: @Binding

Use `@Binding` only when a child view needs to **modify** state owned by a parent. If the child only reads the value, use `let` instead.

```swift
struct ParentView: View {
    @State private var isSelected = false

    var body: some View {
        ChildView(isSelected: $isSelected)
    }
}

struct ChildView: View {
    @Binding var isSelected: Bool

    var body: some View {
        Button("Toggle") { isSelected.toggle() }
    }
}
```

### Prefer KeyPath/subscript bindings over closure bindings

A hand-written `Binding(get:set:)` closure allocates a new closure on every `body` evaluation and can't be reliably compared, which can trigger unnecessary invalidations. Prefer a KeyPath/subscript-based binding through the model instead.

```swift
@Observable
final class ScoreboardModel {
    private(set) var scores: [String: Int] = [
        "Alice": 42, "Bob": 17, "Carol": 99,
    ]

    let players = ["Alice", "Bob", "Carol"]

    // A subscript with a labeled argument can be used as a functional
    // 'projection' into the underlying model if given a Binding to it.
    subscript(scoreFor player: String) -> Int {
        get { scores[player, default: 0] }
        set { scores[player] = newValue }
    }
}

struct PlayerScoreRow: View {
    var player: String
    @Binding var score: Int

    var body: some View {
        HStack {
            Text(player).frame(width: 80, alignment: .leading)
            Stepper("\(score) pts", value: $score, in: 0...999)
        }
    }
}

struct ScoreboardView: View {
    @State private var model = ScoreboardModel()

    var body: some View {
        NavigationStack {
            List(model.players, id: \.self) { player in
                // BAD: a closure binding heap-allocates on every body pass
                // and can trigger unnecessary invalidations.
                let badModelBinding = Binding(
                    get: { model[scoreFor: player] },
                    set: { model[scoreFor: player] = $0 }
                )
                PlayerScoreRow(player: player, score: badModelBinding)

                // GOOD: project through a subscript with @Bindable.
                @Bindable var model = model
                PlayerScoreRow(player: player, score: $model[scoreFor: player])
            }
            .navigationTitle("Scoreboard")
        }
    }
}
```

If no suitable subscript exists on the model, add one — a labeled subscript reads as a clean projection into the model. Reserve closure bindings for transforms no key path or subscript can express.

## @Bindable

Use when a view receives an `@Observable` object from outside and needs bindings into its properties:

```swift
@Observable
final class UserModel {
    var name = ""
    var email = ""
}

struct ParentView: View {
    @State private var user = UserModel()

    var body: some View {
        EditUserView(user: user)
    }
}

struct EditUserView: View {
    @Bindable var user: UserModel  // received from parent, needs bindings

    var body: some View {
        Form {
            TextField("Name", text: $user.name)
            TextField("Email", text: $user.email)
        }
    }
}
```

## Legacy: @StateObject vs @ObservedObject (pre-iOS 17)

Always prefer `@Observable` with `@State` for iOS 17+. When working with legacy `ObservableObject` code, the distinction is **ownership**: `@StateObject` when the view creates and owns the object; `@ObservedObject` when the view receives it from outside.

```swift
// View creates it → @StateObject
@StateObject private var viewModel = MyViewModel()

// View receives it → @ObservedObject
@ObservedObject var viewModel: MyViewModel
```

Never create an `ObservableObject` inline with `@ObservedObject` — it recreates the instance on every view update.

If you must create a `@StateObject` inside a custom `init`, pass the expression directly to `StateObject(wrappedValue:)` so the `@autoclosure` defers creation — assigning to a local first defeats it:

```swift
// WRONG — assigning to a local first defeats @autoclosure
let vm = MovieDetailsViewModel(movie: movie)
_viewModel = StateObject(wrappedValue: vm)

// CORRECT — inline expression defers creation
_viewModel = StateObject(wrappedValue: MovieDetailsViewModel(movie: movie))
```

SwiftUI can't track changes through *nested* `ObservableObject` properties — this limitation is specific to `ObservableObject`; `@Observable` supports nesting automatically. The workaround for legacy code is to pass the nested object directly to child views as its own `@ObservedObject`.

## Passing data into views

A view's input shape determines its invalidation surface for value-type inputs. SwiftUI compares value types field by field; if any field changed, the view's body runs. A view declared with `let user: User` (a struct) invalidates whenever *any* property of `User` is replaced — even properties this view never reads. A view declared with `let name: String` invalidates only when the name changes.

Reference types behave differently: SwiftUI compares class instances by pointer identity, not field by field — a view holding a class reference re-invalidates only when the parent hands it a different instance. For `@Observable` class models, the observation system layers on top of that and tracks which properties each view reads during `body`, invalidating only the views that read the specific property that changed (see "Model objects with @Observable" below). So the narrow-inputs rule below is critical for value-type inputs and largely doesn't apply to reference-type inputs.

### Pass views only the data they read

This applies to every view, not just subviews extracted from a larger parent. A screen that takes a whole struct just to display one field invalidates on every unrelated change to that struct.

```swift
// AVOID: Taking the whole `User` struct (a value type) when the view
// reads only one field. SwiftUI compares `User` field by field, so
// `AvatarBadge` invalidates on any `User` change — bio edit, follower
// count tick, preferences toggle — even though it only displays
// `avatarURL`.
struct User {
    var name: String
    var bio: String
    var avatarURL: URL
    var followerCount: Int
    // ... more fields
}

struct AvatarBadge: View {
    let user: User
    var body: some View { AsyncImage(url: user.avatarURL) }
}
```

```swift
// PREFER: Take only the field the view actually reads.
struct AvatarBadge: View {
    let avatarURL: URL
    var body: some View { AsyncImage(url: avatarURL) }
}
```

"Reads" includes "forwards to a subview." A view that takes `let avatarURL: URL` and passes it to `AvatarBadge(avatarURL: avatarURL)` is using `avatarURL` even though it never appears directly in a `Text(...)` or modifier. The rule targets fields a view *truly* never touches — not fields the view consumes by constructing children that render them. A parent that takes five fields and forwards each to the right subview is correctly factored, not "holding data it doesn't read."

### Watch the cost of large value-type inputs

Field-by-field comparison isn't free: every input check walks every field. For small structs (a few primitives, a URL) the cost is negligible. For a struct decoded from a large JSON payload — nested arrays, dictionaries, dozens of fields — it adds up. Every body evaluation in the parent deep-compares the entire payload to decide whether the child changed, and every subview that takes the payload as input pays the same cost. The "narrow inputs" rule above already mitigates this: a subview that takes `let title: String` does one string comparison, not a tree walk over a decoded response.

```swift
// AVOID: Passing a large value-type payload through the view tree.
struct Article {
    let id: UUID
    let title: String
    let author: String
    let body: String                  // can be 50KB+
    let comments: [Comment]           // can be hundreds
    let related: [RelatedArticle]
    let editorialNotes: [Note]
}

struct ArticleRow: View {
    let article: Article
    var body: some View { Text(article.title) }
}
```

```swift
// PREFER: The full payload doesn't live on any view — it's owned by
// the model layer — and views see only the narrow values they render.
struct ArticleRow: View {
    let title: String
    var body: some View { Text(title) }
}
```

**Break the payload into per-view structs.** When every field of a large struct really is consumed somewhere in the view tree, don't pass it whole anyway — break it into discrete structs that each belong to a specific view, so each view's comparison surface is bounded by what it actually displays. Don't make the app's entire value-type data model the input to every view in the hierarchy.

**Or hold the payload in an @Observable model.** If you don't want to split a large value type — typically because it maps cleanly to a server payload and reshaping it would ripple through decoding — put it inside an `@Observable` model and pass the model instead. Reference comparison is cheap (pointer identity), and the observation system invalidates only views that read individually-tracked properties. But take care with compound stored properties on the model: a view that reads an entire `Array`, `Dictionary`, or `Set` establishes a dependency on the *whole collection*. See "Per-property dependency granularity" below for the mitigation.

## Model objects with @Observable

Use `@Observable` (not `ObservableObject`) for classes that provide data to views. The macro generates per-property observation tracking that scopes invalidation to the exact views that read the changed property — far cheaper than `ObservableObject`'s coarse `objectWillChange` broadcasts.

Mark `@Observable` classes with `@MainActor` unless the project has Main Actor default actor isolation (typically set via `SWIFT_DEFAULT_ACTOR_ISOLATION` in build settings). Views read the model on the main actor during body evaluation; without `@MainActor` the model's properties are reachable from any thread, and writes from background tasks can race with view reads — Swift 6 strict concurrency flags this. `@Observable` is not supported on `actor` types.

```swift
// AVOID: @Observable class without @MainActor. Properties are reachable
// from any thread, but views read them on the main actor — background
// writes can race with main-actor reads, and strict concurrency will
// flag the model.
@Observable
final class OrderModel {
    var status: DeliveryStatus = .placed
}
```

```swift
// PREFER: @MainActor on the @Observable class. Reads and writes are
// confined to the main actor, matching how views consume the model.
// Background work hops to the main actor
// (e.g. `await MainActor.run { model.status = .shipped }`).
@MainActor
@Observable
final class OrderModel {
    var status: DeliveryStatus = .placed
}
```

### Property wrappers inside @Observable classes

The `@Observable` macro transforms stored properties to add observation tracking. Other property wrappers (`@AppStorage`, `@SceneStorage`, `@Query`, and similar) also transform properties with their own storage — the two transformations conflict and produce a compiler error unless the property is marked `@ObservationIgnored`.

```swift
@Observable
@MainActor
final class SettingsModel {
    // WRONG - compiler error: property wrappers conflict with @Observable
    // @AppStorage("username") var username = ""

    // CORRECT - @ObservationIgnored prevents the conflict
    @ObservationIgnored @AppStorage("username") var username = ""
    @ObservationIgnored @AppStorage("isDarkMode") var isDarkMode = false

    // Regular stored properties work fine with @Observable
    var isLoading = false
}
```

This applies to **any** property wrapper used inside an `@Observable` class, including but not limited to `@AppStorage`, `@SceneStorage`, and SwiftData's `@Query`. `@ObservationIgnored` disables Observation-framework tracking for that property, but wrappers like `@AppStorage` already notify SwiftUI through their own mechanism (e.g. UserDefaults KVO), so views still update correctly. Never remove `@ObservationIgnored` from these properties — doing so causes a compiler error.

### Make @Observable property types Equatable

The `@Observable` macro generates a setter that skips invalidation when the new value equals the current one — but only when it can compare them, i.e. only when the property's type is `Equatable`. Without that conformance, every assignment notifies observing views, even when the value is identical. This is an easy win for properties written frequently with the same value (polling, streaming updates, timers). The check is emitted into the generated setter as user code, so it applies on every OS release that supports `@Observable`, built with current Xcode.

```swift
// AVOID: DeliveryStatus is not Equatable.
// Every assignment to `status` invalidates observing views, even if the
// value hasn't actually changed.
enum DeliveryStatus {
    case placed, preparing, shipped, delivered
}
```

```swift
// PREFER: Making DeliveryStatus Equatable lets the @Observable setter
// short-circuit redundant invalidations when the same status is set again.
enum DeliveryStatus: Equatable {
    case placed, preparing, shipped, delivered
}
```

This applies to collection properties too: an `Array`/`Set`/`Dictionary` is only `Equatable` when its element type is. A non-`Equatable` element type defeats the short-circuit for the whole collection, so assigning the same contents always invalidates.

```swift
// AVOID: Ingredient is not Equatable, so assigning the same array of
// ingredients to `recipe.ingredients` always invalidates observing views.
struct Ingredient {
    var name: String
    var quantity: Double
    var unit: String
}
```

```swift
// PREFER: Making Ingredient Equatable allows Array's built-in Equatable
// conformance to compare element-wise, so the setter skips redundant
// invalidations when the same ingredients are set again.
struct Ingredient: Equatable, Identifiable {
    var name: String
    var quantity: Double
    var unit: String
}
```

This is distinct from `Equatable` *views* (a conformance that lets SwiftUI skip re-diffing a view's body — see `performance.md` in this skill): this conformance lets the *model* skip notifying observers in the first place.

### Per-property dependency granularity

When a view reads a property of an `@Observable` model, the observation system records a dependency on that exact property and invalidates the view only when *that* property changes. A view that reads `model.title` invalidates on `title` changes but not on `model.description` changes — this per-property tracking is the main reason `@Observable` is so much cheaper than `ObservableObject` for granular updates.

The subtlety is that "property" is the granularity, not "field within a property." A property whose type is itself compound — a struct, an `Array`, a `Dictionary`, a `Set` — creates a dependency on the *entire value*. Reading any field of a stored struct, or any element of a stored collection, establishes a dependency on the whole stored property. Three common traps:

**A computed property establishes dependencies transitively.** `var currentUser: User? { users.first { $0.id == currentID } }` reads `users` inside its body, so any view reading `currentUser` ends up depending on the *entire `users` array*, not just the one user. Renaming the access doesn't change what observation tracks — wrapping the lookup in a computed property only *looks* like it narrows the dependency.

```swift
// AVOID: A view that needs only one item, but reaches it through the
// whole collection. Every change to `users` — add, remove, edit any
// field of any user — invalidates `CurrentUserBadge`.
@MainActor
@Observable
final class AppState {
    var users: [User] = []
    var currentUserID: User.ID?
}

struct CurrentUserBadge: View {
    let state: AppState

    var body: some View {
        if let id = state.currentUserID,
           let user = state.users.first(where: { $0.id == id }) {
            Text(user.name)
        }
    }
}
```

```swift
// AVOID (attempted fix that doesn't work): The computed body still
// reads `users` — so `state.currentUser` establishes a dependency on
// the whole array transitively.
@MainActor
@Observable
final class AppState {
    var users: [User] = []
    var currentUserID: User.ID?

    var currentUser: User? {
        users.first { $0.id == currentUserID }
    }
}
```

```swift
// PREFER: Cache the derived value as its own stored property and keep
// it up to date in didSet. Views read the prepared property and
// invalidate only when *it* changes — not on every change to `users`.
@MainActor
@Observable
final class AppState {
    var users: [User] = [] {
        didSet { recomputeCurrentUser() }
    }
    var currentUserID: User.ID? {
        didSet { recomputeCurrentUser() }
    }

    private(set) var currentUser: User?

    private func recomputeCurrentUser() {
        currentUser = users.first { $0.id == currentUserID }
    }
}

struct CurrentUserBadge: View {
    let state: AppState

    var body: some View {
        if let user = state.currentUser {
            Text(user.name)
        }
    }
}
```

**A struct-typed stored property drags the whole struct.** A view reading `session.user.name` depends on `session.user` as a whole — mutating any *other* field of `user`, or replacing it with a new `User` value, invalidates that view too.

```swift
// AVOID: User struct held as a single property on the @Observable
// model. `ProfileBadge` reads `session.user.name`, `session.user.email`,
// `session.user.avatarURL` — every one of those reads establishes a
// dependency on `session.user`. Editing `preferences` (or any other
// field of `user`) also invalidates the view.
struct User {
    var name: String
    var email: String
    var avatarURL: URL
    var preferences: Preferences
}

@MainActor
@Observable
final class UserSession {
    var user: User
    init(user: User) { self.user = user }
}

struct ProfileBadge: View {
    let session: UserSession

    var body: some View {
        HStack {
            AsyncImage(url: session.user.avatarURL)
                .frame(width: 32, height: 32)
                .clipShape(Circle())
            VStack(alignment: .leading) {
                Text(session.user.name).font(.headline)
                Text(session.user.email).font(.caption)
            }
        }
    }
}
```

The fix is to expose the struct's fields as individual properties on the `@Observable` model. The observation system tracks each field separately, so a view that reads only `userName` invalidates only when `userName` changes:

```swift
// PREFER: Flatten the struct's fields onto the model. Each field is
// tracked independently. `ProfileBadge` depends on `userName`,
// `userEmail`, and `avatarURL` — not on `preferences` — so editing
// preferences no longer invalidates it.
@MainActor
@Observable
final class UserSession {
    var userName: String
    var userEmail: String
    var avatarURL: URL
    var preferences: Preferences

    init(user: User) {
        self.userName = user.name
        self.userEmail = user.email
        self.avatarURL = user.avatarURL
        self.preferences = user.preferences
    }
}

struct ProfileBadge: View {
    let session: UserSession

    var body: some View {
        HStack {
            AsyncImage(url: session.avatarURL)
                .frame(width: 32, height: 32)
                .clipShape(Circle())
            VStack(alignment: .leading) {
                Text(session.userName).font(.headline)
                Text(session.userEmail).font(.caption)
            }
        }
    }
}
```

If the struct needs to be round-tripped (re-encoded into a payload, sent back to a server) and you don't want to lose its shape, keep both: a `var user: User` for round-tripping and individual properties for view consumption, kept in sync via `didSet` on `user`.

**An array/collection read drags the whole collection.** Reading one element establishes a dependency on the entire stored collection — see "Pass @Observable collection elements to row views" below for the fix.

**Multiple individual property reads are fine.** A view that reads several individual (already-narrow) properties from one `@Observable` model is *not* over-subscribed and doesn't need to be split. Per-property tracking already scopes the view's invalidation to exactly those properties; carving the model into per-property subviews adds indirection without changing what re-runs when. The traps above are about *single* reads that pull in too much — a struct-typed field that drags the whole struct, an array access that drags the whole collection, a computed property that proxies the same wide read — not about views that legitimately read several already-narrow properties.

### Extract a smaller @Observable when many views share data

When a piece of data is read by many independent views — or by views that should be invalidation-isolated from each other — pull it into its own `@Observable` model and hand each view that smaller model rather than the larger one. The view's dependency surface is then bounded by the smaller model, and the larger model can change without rippling through.

### Pass @Observable collection elements directly to row views

When iterating a collection from an `@Observable` model, the list view that holds the `ForEach` legitimately depends on the collection — it needs to re-run when elements are inserted, removed, or reordered. The row view shouldn't reach back into the model to look up its element by index or key, though: doing so makes every row depend on the whole collection, so editing one element invalidates every row. Pass the element value directly into the row.

**Single-field rows: pass the field.**

```swift
// AVOID: Row reaches back into the model by index. Every UserRow's
// body reads `state.users`, so any edit to any user invalidates every
// row — not just the one whose data changed.
struct UserList: View {
    let state: AppState

    var body: some View {
        ForEach(state.users.indices, id: \.self) { index in
            UserRow(state: state, index: index)
        }
    }
}

struct UserRow: View {
    let state: AppState
    let index: Int

    var body: some View { Text(state.users[index].name) }
}
```

```swift
// PREFER: Pass the row only the field it displays. `UserList` depends
// on `state.users` (correct — the list shape depends on it), but each
// `UserRow` takes just the name it renders. Editing one user's email
// doesn't re-run any row's body; editing one user's name re-runs only
// that row.
struct UserList: View {
    let state: AppState

    var body: some View {
        ForEach(state.users) { user in
            UserRow(name: user.name)
        }
    }
}

struct UserRow: View {
    let name: String
    var body: some View { Text(name) }
}
```

**Multi-field rows: pass a persisted @Observable instance.** When each row genuinely observes several fields of its element, model each element as its own `@Observable` and have the parent **persist** the instances. The list view still depends on the array of references (so it re-runs on inserts, removes, reorders), but each row's dependencies are scoped to its own model — a row can observe multiple properties of its user without depending on the whole collection or the whole struct, and editing one field of one user invalidates only the row that displays that user.

The instances must be persisted. Vending a freshly-constructed `@Observable` on every read hands each row a new reference on every parent body evaluation; stored references compare unequal each time, every row's body re-runs, and nothing has actually changed.

```swift
// PREFER (multi-field rows): Per-element @Observable models that the
// parent stores and reuses. `UserRow` observes its specific user
// directly, so editing one field of one user invalidates only that
// row — and the row gets to read multiple fields without paying the
// whole-collection cost.
@MainActor
@Observable
final class User: Identifiable {
    let id: UUID
    var name: String
    var email: String
    var avatarURL: URL

    init(id: UUID = UUID(), name: String, email: String, avatarURL: URL) {
        self.id = id
        self.name = name
        self.email = email
        self.avatarURL = avatarURL
    }
}

@MainActor
@Observable
final class AppState {
    var users: [User] = []  // persisted; each User's identity is stable
    // ... mutations modify existing User instances in place
}

struct UserList: View {
    let state: AppState

    var body: some View {
        ForEach(state.users) { user in
            UserRow(user: user)
        }
    }
}

struct UserRow: View {
    let user: User

    var body: some View {
        HStack {
            AsyncImage(url: user.avatarURL)
                .frame(width: 32, height: 32)
                .clipShape(Circle())
            VStack(alignment: .leading) {
                Text(user.name).font(.headline)
                Text(user.email).font(.caption)
            }
        }
    }
}
```

For the equivalent problem in scroll-position-driven visibility (per-item view models keyed off frequently-changing derived state), see "Rapidly updating environment values" below and `performance.md` in this skill.

## Side effects in views: isolating onChange(of:)

When a view uses `.onChange(of:)` to react to a dependency (an `@Environment` value, a `@Binding`, or a property from an `@Observable` object), that dependency is read in the view's body scope. This creates a dependency on the value: the view's body re-evaluates every time the dependency changes, even if the dependency is never used for rendering. If the view's body is expensive (deep hierarchy, many children), this causes unnecessary work.

Extract the `.onChange` and the dependency it observes into a separate `ViewModifier` dedicated to the side effect. Only the lightweight side-effect modifier re-evaluates when the value changes; the host view's dependency surface doesn't include the value at all.

```swift
// AVOID: ContentView reads `counter` from the environment solely for
// .onChange. Every change to `counter` creates a dependency and
// re-evaluates the expensive ScrollView hierarchy.
struct ContentView: View {
    @State private var model = Model()
    @Environment(\.counter) private var counter

    var body: some View {
        ScrollView {
            // ... expensive view hierarchy ...
        }
        .onChange(of: counter) {
            model.counter = counter
        }
    }
}
```

```swift
// PREFER: Extract the dependency and .onChange into a ViewModifier.
// The modifier owns the read of `counter` — when counter changes, only
// the modifier's body re-runs, not ContentView's.
struct CounterSyncModifier: ViewModifier {
    let model: Model
    @Environment(\.counter) private var counter

    func body(content: Content) -> some View {
        content
            .onChange(of: counter) {
                model.counter = counter
            }
    }
}

extension View {
    func counterSync(model: Model) -> some View {
        modifier(CounterSyncModifier(model: model))
    }
}

struct ContentView: View {
    @State private var model = Model()

    var body: some View {
        ScrollView {
            // ... expensive view hierarchy ...
        }
        .counterSync(model: model)
    }
}
```

The same principle applies to any dependency type — `@Binding`, `@Observable` properties, or combinations of several, each isolated behind one modifier:

```swift
struct EditorChangesModifier: ViewModifier {
    var document: DocumentModel
    @Binding var isActive: Bool
    let model: EditorModel

    func body(content: Content) -> some View {
        content
            .onChange(of: document.wordCount) {
                model.updateStatistics(wordCount: document.wordCount)
            }
            .onChange(of: isActive) {
                model.setActive(isActive)
            }
    }
}

extension View {
    func editorChanges(
        document: DocumentModel,
        isActive: Binding<Bool>,
        model: EditorModel
    ) -> some View {
        modifier(EditorChangesModifier(document: document, isActive: isActive, model: model))
    }
}
```

Apply this pattern when both hold: a dependency is read only for a side effect (not for rendering), and the parent view's body is non-trivial. Skip it when the dependency is also used directly in rendering output (the view invalidates regardless — isolation gives no benefit) or when the body is already trivial (the extra view isn't justified).

## @Entry macro

When defining custom environment, transaction, container, or focused values, always prefer `@Entry` over a manual `EnvironmentKey`/`ContainerValuesKey`/`FocusedValueKey` conformance — it reduces boilerplate and avoids mistakes.

```swift
extension EnvironmentValues {
    @Entry var myCustomValue: String = "Default value"
    @Entry var anotherCustomValue = true
}

extension Transaction {
    @Entry var myCustomValue: String = "Default value"
}

extension ContainerValues {
    @Entry var myCustomValue: String = "Default value"
}
```

Since `FocusedValues`' default is always `nil`, its `@Entry` declarations must be `Optional`:

```swift
extension FocusedValues {
    @Entry var myCustomValue: String?
}
```

`@Entry` requires a **stable** default — one whose expression returns the same result on every read. See "Unstable environment default values" below for the full rule, the unstable shapes to avoid, and the three fix options; the same rule applies to `@Entry` on `Transaction`, `ContainerValues`, and `FocusedValues`. Stable shapes that need no fix: literals (`"home"`, `0`, `true`), enum cases with no associated values (`.standard`), `nil` for an optional, and references to a stable instance (a `static let`-backed value).

When reviewing code that defines custom values via manual `EnvironmentKey`/`ContainerValuesKey`/`FocusedValueKey` conformances and a hand-written `get`/`set` extension property, surface the `@Entry` refactor as a top-line finding, not an aside — the manual form is older boilerplate `@Entry` was specifically designed to replace. `@Entry` requires iOS 18 / macOS 15 / Xcode 16; when the deployment target isn't specified, recommend the refactor without a defensive hedge.

## Environment & focused values

`EnvironmentValues`/`@Environment` and `FocusedValues`/`@FocusedValue` share one comparison model, so every rule below applies to both (substitute `FocusedValues`/`@FocusedValue` for `EnvironmentValues`/`@Environment` in any example).

### How environment comparison works

When an environment value propagates, SwiftUI compares the old and new value to decide whether each reader needs to re-evaluate:

- **Structs compare field-by-field.** A non-`Equatable` struct whose fields all look equal compares as equal — `Equatable` is a fast path, not a prerequisite.
- **Class references compare by identity.** Two references to the same instance are equal; reassigning to a freshly-allocated instance is not.
- **Function values (closures) can't be compared reliably.** SwiftUI treats each re-read as changed, and every reader in the subtree invalidates.
- **Every environment write propagates to the whole subtree.** When any key changes, readers re-read their keys. A reader that falls back to its *default* gets that default re-evaluated on every pass — so an unstable default invalidates on every unrelated env write.

### Never store closures in custom environment/focus keys

This rule is about **custom** keys you define. Framework-provided action types — `OpenURLAction`, `DismissAction`, `RefreshAction`, `\.openURL`, `\.dismiss`, `\.refresh`, and similar — are designed to wrap a closure and pair with framework-provided keys. Passing a closure to one of these is the intended API, not the anti-pattern below; don't propose defunctionalizing them.

For your own keys: never store closures or function values. Closures can't be reliably compared, so views reading that key may invalidate even when nothing changed — comparison heuristics vary by optimization level, signature, and captures, and the rule is unconditional (a closure that happens to compare equal today gives you no guarantee about future writer sites adding captures).

**Not a fix: wrapping the closure in a struct.** The struct's closure property still can't be compared, and every body evaluation constructs a new struct with a freshly-allocated closure — the reader still invalidates on every write.

**Not a fix: hoisting the closure to a stored property on the View.** SwiftUI re-instantiates `View` structs freely, so a `private let action: () -> Void = { ... }` initializer re-runs and produces a fresh closure each time the struct is constructed; even when the pointer happens to be stable, closure comparison heuristics still treat them as unequal under some optimization levels.

**Closure with no captures — example and fix:**

```swift
// AVOID
extension EnvironmentValues {
    @Entry var submitAction: (String) -> Void = { _ in }
}

struct FormContainer: View {
    var body: some View {
        FormFields()
            .environment(\.submitAction) { draft in print("Submit: \(draft)") }
    }
}

struct FormFields: View {
    // Always invalidated: SwiftUI cannot compare the closure.
    @Environment(\.submitAction) private var submit
    var body: some View { Button("Submit") { submit("hello") } }
}
```

Option A — defunctionalize into a struct with `callAsFunction` (keeps call-site ergonomics; SwiftUI compares the struct's stored properties):

```swift
struct SubmitAction {
    func callAsFunction(_ draft: String) { print("Submit: \(draft)") }
}

extension EnvironmentValues {
    @Entry var submitAction = SubmitAction()
}

struct FormContainer: View {
    var body: some View {
        FormFields().environment(\.submitAction, SubmitAction())
    }
}

struct FormFields: View {
    @Environment(\.submitAction) private var submit
    var body: some View { Button("Submit") { submit("hello") } }  // reads like a closure call
}
```

Option B — use an `@Observable` model (reference comparison by identity keeps the environment value stable):

```swift
@MainActor
@Observable
final class FormHandler {
    func submit(_ draft: String) { print("Submit: \(draft)") }
}

struct FormContainer: View {
    @State private var handler = FormHandler()
    var body: some View { FormFields().environment(handler) }
}

struct FormFields: View {
    @Environment(FormHandler.self) private var handler
    var body: some View { Button("Submit") { handler.submit("hello") } }
}
```

Prefer Option A when the action is stateless and self-contained; prefer Option B when the handler needs to coordinate with other state on a shared model, or you want to reuse the model for related functionality.

**Closure with captures** follows the same shape: Option A stores the previously-captured state as a property on the defunctionalized struct (mark the `@Entry` optional if the action is invalid without it); Option B moves the captured state onto the `@Observable` model as an observable property. Prefer A when the captured state is small, view-local, and not shared; prefer B when the state naturally belongs outside the view (multiple readers/writers, external mutation, or you want per-property tracking across the subtree).

**Advanced case — a closure whose implementation differs entirely by injection site** (e.g. one `appearanceHandler` env key used by both a form view and a shopping-cart view, each tracking different metrics): defunctionalize into **separate structs conforming to a shared protocol**, and type the `@Entry` as the protocol:

```swift
protocol AppearanceHandler { func callAsFunction() }

extension EnvironmentValues {
    @Entry var appearanceHandler: AppearanceHandler?
}

struct FormAppearanceHandler: AppearanceHandler {
    let tracker: MetricsTracker
    let name: String
    func callAsFunction() { tracker.trackForm(name: name) }
}

struct CartAppearanceHandler: AppearanceHandler {
    let tracker: MetricsTracker
    let itemCount: Int
    func callAsFunction() { tracker.trackCart(itemCount: itemCount) }
}
```

Choose `callAsFunction` when replacing an existing closure call site and preserving `handler(x)` ergonomics; choose a named method (`handleURL(_:)`, `submit(_:)`) when the protocol describes a specific, nameable operation and the call site reads better named.

Alternatively, **unify related state and logic into a shared `@Observable` model** instead of an open-ended protocol — this is often simpler when the handler kinds share state (a common tracker) and the set of kinds is closed:

```swift
@MainActor
@Observable
final class Model {
    private let tracker = MetricsTracker()
    var formName: String = "Form1"
    var cartItemCount: Int = 0
    func trackFormAppearance() { tracker.trackForm(name: formName) }
    func trackCartAppearance() { tracker.trackCart(itemCount: cartItemCount) }
}
```

Prefer the protocol form when handler kinds are independent and the set is open (e.g. third parties may add new handlers); prefer the unified model when the set is closed and handlers share state — it avoids the existential and usually shrinks the code.

### Rapidly updating environment values

Every update to an environment key incurs a cost for **every view that reads any key** from the environment in the affected subtree, even keys that aren't being updated — SwiftUI must check whether each view's value changed. Avoid placing high-frequency values in the environment: scroll offset (`scrollPosition`/`onScrollGeometryChange`), window/container size (`GeometryReader`/`onGeometryChange`), drag translation (`DragGesture().onChanged`), per-frame animation progress (`TimelineView`, display-link-driven values), timer-driven state, pointer/cursor/hover location.

Instead, store the frequently-updated value in an `@Observable` model and expose **coarsened thresholds** rather than the raw value. `@Observable` tracks per-property access, so only views that read the specific derived property invalidate.

```swift
// AVOID: Propagating a rapidly-changing CGFloat through the environment.
// Every pixel of a window resize incurs a comparison cost for all
// environment-reading views in the subtree.
extension EnvironmentValues {
    @Entry var windowWidth: CGFloat = 0
}
```

```swift
// PREFER: Hold geometry in an @Observable model and expose coarsened
// thresholds. Views only invalidate when crossing a meaningful
// boundary, not on every pixel.
@MainActor
@Observable
final class ViewportModel {
    var width: CGFloat = 0 { didSet { isWide = width > 600 } }
    private(set) var isWide: Bool = false
}

struct RootView: View {
    @State private var viewport = ViewportModel()

    var body: some View {
        ContentView()
            .environment(viewport)
            .onGeometryChange(for: CGFloat.self) { proxy in
                proxy.size.width
            } action: { newWidth in
                viewport.width = newWidth
            }
    }
}

struct ContentView: View {
    @Environment(ViewportModel.self) private var viewport
    var body: some View {
        Text(viewport.isWide ? "Wide layout" : "Compact layout")  // invalidates only on threshold crossing
    }
}
```

**Per-item coarsening in lists is a distinct step.** When each row's appearance depends on scroll position, moving a shared offset onto an `@Observable` model and having rows read it raw does *not* reduce invalidations — every row still depends on `offset`, so all visible rows invalidate every frame, just routed through the model instead of the environment. A common intermediate — a shared `Set<Int>` of visible indices, with each row calling `.contains(index)` — is a real improvement (fires only on boundary crosses, not every frame), but Observation tracks at the property level: mutating the set invalidates *every* row that read it, not just the 1–2 rows whose visibility changed.

True per-item isolation needs the work done **at the model**: give each item its own `@Observable` object whose properties track only that item's derived state.

```swift
@MainActor
@Observable
final class FeedModel {
    private(set) var items: [ItemModel] = []

    func updateOffset(_ offset: CGFloat) {
        let visible = Set(computeVisibleIndices(for: offset))
        for (i, item) in items.enumerated() {
            item.isVisible = visible.contains(i)
        }
    }

    private func computeVisibleIndices(for offset: CGFloat) -> [Int] {
        // ... derive visible indices from offset, item height, viewport height.
    }
}

@MainActor
@Observable
final class ItemModel {
    let index: Int
    var isVisible = false
    init(index: Int) { self.index = index }
}

struct FeedItemView: View {
    @Environment(ItemModel.self) private var item

    var body: some View {
        Text("Item \(item.index)")
            .opacity(item.isVisible ? 1 : 0.3)  // invalidates at most twice: enter + leave
    }
}

struct FeedView: View {
    @State private var feedModel = FeedModel()

    var body: some View {
        ScrollView {
            LazyVStack {
                ForEach(feedModel.items) { item in
                    FeedItemView().environment(item)
                }
            }
        }
    }
}
```

The discriminating question is *"what's the granularity of the value the view actually reads?"* — not "is the value held in `@Observable`?" `@Observable` is a precondition for per-property tracking; coarsening is what actually reduces the per-frame invalidation count.

For purely visual effects driven by scroll position (opacity, scale, rotation tied to viewport position), `scrollTransition` and `visualEffect(in:)` push the per-frame work to the renderer and skip body re-evaluation entirely — the right tool when nothing outside the row's own styling depends on scroll position. They don't replace the `@Observable` + coarsening pattern when scroll-derived state needs to drive non-rendering logic (model updates, prefetches, network calls, sibling-view state). Rule of thumb: if you'd otherwise propagate the value via `@State`/`@Environment` to drive logic, use the coarsened model; if you only need a view modifier, use the framework modifier.

### Unstable environment default values

An environment key's `defaultValue` is re-evaluated on every read that falls back to it whenever it's declared as a computed property. Two common ways to hit this:

- `@Entry` always wraps the default expression in a computed getter (for concurrency safety — the default doesn't need to be `Sendable`), so `@Entry var model = Model()` re-allocates `Model()` on every fallback read.
- A manual `EnvironmentKey` with a computed default — `static var defaultValue: T { Model() }` — re-runs the expression on every access for the same reason.

This is a problem for **all reference types** (each call allocates a new heap instance, so reference equality fails) and for **any default expression that can return a different result between calls**, even value types like `Date()`, `UUID()`, or random numbers. Any ancestor write to *any* environment key causes descendants to re-read theirs; a reader falling back to an unstable default gets a different value than before and invalidates, even though nothing relevant to it changed.

`Equatable` is a fast path, not a prerequisite — a value-typed default is stable as long as each stored property resolves to the same value on every call (enum cases, `nil`, fixed literals, and references to the same instance across calls all qualify). What breaks stability is a stored property that differs between calls: a fresh reference allocation (`struct Foo { let model = Model() }` — every `Foo()` gets a new `Model` pointer) or a captured runtime value (`Date()`, `UUID()`). The operative test is *"does the expression return a different result between calls,"* not *"does the type conform to Equatable."*

Stable defaults that need no fix: a fixed literal, a `nil` optional default, or a `let`-backed value (an `@Entry` backed by a `static let`, or a manual key with `static let defaultValue`). A struct field that holds a reference is not itself a red flag — what matters is whether the *source* of the reference is stable (a module-level `let`, a `static let`, or a dependency-injected instance held by the caller all produce the same pointer on every call). A struct constructed inline in `@Entry` with deterministic argument values (enum cases, `nil`, literals, stable references) is likewise fine and doesn't need `Equatable` conformance either — SwiftUI compares field-by-field.

The invalidation only materializes when a reader actually falls back to the default. If every reader has a value injected upstream via `.environment(\.key, …)`, the unstable default is *latent* — fixing it is still correct (a future maintainer adding a reader without upstream injection would silently surface the problem), but it's a regression guard rather than a current-cost recovery. Distinguish the two when reviewing: a live issue has readers falling back and paying invalidation now; a latent one has every reader currently covered by an upstream injection.

```swift
@Observable class Model {}

extension EnvironmentValues {
    @Entry var model = Model()   // unstable: re-allocates on every fallback read
    @Entry var counter = 0
}

struct ContentView: View {
    @State private var counter = 0
    var body: some View {
        VStack {
            Button("++") { counter += 1 }
            RowContent()
        }
        .environment(\.counter, counter)
    }
}

struct RowContent: View {
    @Environment(\.model) private var model
    var body: some View {
        // Every "++" invalidates this view because `model`'s default
        // getter constructs a new `Model()` on every read.
        Text("Row Content")
    }
}
```

**Not a fix: conforming the default type to Equatable.** A trivial or degenerate `==` can suppress the invalidation *symptom*, but the default expression still re-evaluates on every read — a new instance is allocated each time, any initializer side effects still fire, and two readers that both fall back to the default end up with two different instances (so observation changes on one don't propagate to the other).

**Not a fix: defensive memoization of already-stable defaults.** If a default already satisfies the operative test (every field resolves to the same value across calls), leave it alone — don't add `static let` backing, an `Optional` wrap, or `Equatable` conformance "for safety." That's noise implying a bug where there isn't one.

Three fix options, all applicable to reference-type and fresh-value (`Date()`, `UUID()`) cases alike:

**Option A — back the default with a stable property:**

```swift
@Observable class Model {}

extension EnvironmentValues {
    @Entry var model = _defaultModel
    private static let _defaultModel = Model()
    @Entry var counter = 0
}
```

**Option B — declare the `EnvironmentKey` manually**, with `static let defaultValue` (a stored constant, evaluated once) rather than `static var defaultValue: T { … }` (a computed property that re-evaluates every read):

```swift
private struct ModelKey: EnvironmentKey {
    static let defaultValue = Model()
}

extension EnvironmentValues {
    var model: Model {
        get { self[ModelKey.self] }
        set { self[ModelKey.self] = newValue }
    }
}
```

**Option C — use an optional with a `nil` default:**

```swift
extension EnvironmentValues {
    @Entry var model: Model?
}
```

**Diagnostic — sentinel values in readers signal Option C.** If a reader checks for an "empty"/"default" state with something like `value.id.isEmpty`, `value.count == 0`, `value == .none`, or a comparison against the same default the `@Entry` constructs, that check is an absence test in disguise — the honest expression is `Optional` + `if let`, not a sentinel field on a real instance:

```swift
// Before: unstable default, sentinel-as-absence in reader.
@Observable final class EditingSession {
    var documentId: String
    init(documentId: String) { self.documentId = documentId }
}

extension EnvironmentValues {
    @Entry var editingSession = EditingSession(documentId: "")  // unstable + sentinel default
}

struct DocumentArea: View {
    @Environment(\.editingSession) private var session
    var body: some View {
        if session.documentId.isEmpty { Text("No document open") }  // sentinel-as-absence
        else { Text("Editing: \(session.documentId)") }
    }
}

// After: Option C — absence becomes an Optional, sentinel disappears.
extension EnvironmentValues {
    @Entry var editingSession: EditingSession?
}

struct DocumentArea: View {
    @Environment(\.editingSession) private var session
    var body: some View {
        if let session { Text("Editing: \(session.documentId)") }  // honest absence test
        else { Text("No document open") }
    }
}
```

**Choosing:** run the sentinel diagnostic first. If readers contain a sentinel check, pick Option C and rewrite readers to use `if let` — this fixes the unstable default *and* removes the sentinel design. If readers always use the value as a real instance with no absence checks, the default is semantically a real value: pick Option A to keep `@Entry` syntax with a short default expression, or Option B when the manual `EnvironmentKey` reads more clearly (complex default, used from multiple places, or belongs on the key type).

### Unused @Environment / @FocusedValue reads

Declaring `@Environment(\.someKey)` subscribes the view to changes in `\.someKey` even if `body` never references the wrapped value — an unreferenced KeyPath-form declaration is pure re-evaluation overhead on every write to that key. The same applies to `@FocusedValue`.

The **type-based** form — `@Environment(Model.self)`, used with `@Observable` models — behaves differently: Observation tracks reads at the property level, so declaring `@Environment(Model.self) var model` without reading any property of `model` registers no property-level dependency, and changes to `model`'s properties don't re-evaluate the view. An unused type-form declaration carries no live invalidation cost, unless the env entry for that model has an unstable default (in which case the unstable-default section above applies, not a read-site problem).

When reviewing, walk each view's `@Environment`/`@FocusedValue` declarations and check whether the wrapped property is referenced in the body (directly, via the `_propertyName` projected form, or through a computed property/method the body calls). If nothing references it, delete the declaration. For the KeyPath form, removing it is an active perf fix (every ancestor write to `\.key` is currently invalidating the view); for the type form, removing it is dead-code cleanup with no live perf cost.

```swift
// AVOID: declared but never read in body
struct BadgeView: View {
    @Environment(\.theme) private var theme   // never referenced below
    let label: String
    var body: some View { Text(label) }
}

// PREFER: remove the unused subscription
struct BadgeView: View {
    let label: String
    var body: some View { Text(label) }
}
```

### Custom environment values and @Environment/@EnvironmentObject basics

Access framework-provided environment values with `@Environment`:

```swift
struct MyView: View {
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        Button("Done") { dismiss() }
            .foregroundStyle(colorScheme == .dark ? .white : .black)
    }
}
```

Share an `@Observable` model through the environment by injecting the instance and reading it with the type form — this is the preferred pattern for iOS 17+ (over the legacy `@EnvironmentObject`, which uses `.environmentObject(AppState())` / `@EnvironmentObject var appState: AppState` and should be migrated when touched):

```swift
@Observable
@MainActor
final class AppState {
    var isLoggedIn = false
}

ContentView().environment(AppState())   // inject

struct ChildView: View {
    @Environment(AppState.self) private var appState   // access
}
```

## Decision flowchart

```
Is this value owned by this view?
├─ YES: Is it a simple value type?
│       ├─ YES → @State private var
│       └─ NO (class):
│           ├─ Use @Observable → @State private var (mark class @MainActor)
│           └─ Legacy ObservableObject → @StateObject private var
│
└─ NO (passed from parent):
    ├─ Does child need to MODIFY it?
    │   ├─ YES → @Binding var
    │   └─ NO: Does child need BINDINGS to its properties?
    │       ├─ YES (@Observable) → @Bindable var
    │       └─ NO: Does child react to changes?
    │           ├─ YES → var + .onChange()
    │           └─ NO → let
    │
    └─ Is it a legacy ObservableObject from parent?
        └─ YES → @ObservedObject var (consider migrating to @Observable)
```

## State privacy rules

All view-owned state should be `private`; values passed from a parent should not be:

```swift
struct MyView: View {
    // Created by view - private
    @State private var isExpanded = false
    @State private var viewModel = ViewModel()
    @AppStorage("theme") private var theme = "light"
    @Environment(\.colorScheme) private var colorScheme

    // Passed from parent - not private
    let title: String
    @Binding var isSelected: Bool
    @Bindable var user: User

    var body: some View {
        // ...
    }
}
```

This makes dependencies explicit and improves code completion for the generated initializer — and, as noted above, keeps parent-supplied values out of the generated init so they can never be accidentally routed into a `@State`/`@StateObject`.

`@FocusState` follows the same privacy rule: always mark it `private`.

## Key principles

1. Always prefer `@Observable` over `ObservableObject` for new code.
2. Mark `@Observable` classes `@MainActor` for thread safety, unless the module uses default actor isolation.
3. Own an `@Observable` instance with `@State` (not `@StateObject`); receive one from outside with `@Bindable` or `let`.
4. Always mark `@State`, `@StateObject`, and `@FocusState` `private`; never declare a passed-in value as `@State`/`@StateObject`.
5. Add `@ObservationIgnored` to property wrappers (`@AppStorage`, `@SceneStorage`, `@Query`) inside `@Observable` classes.
6. Prefer `Equatable` types for frequently-written `@Observable` properties, so the generated setter skips redundant invalidations.
7. Observation tracks at the **property** level, not the field level: watch for computed properties, struct-typed properties, and collection properties that drag in more than a view actually reads; cache derived values, flatten struct fields, and pass row views their element directly (or a persisted per-element `@Observable`).
8. Pass every view only the value-type fields it reads (including fields it forwards to children) — SwiftUI's field-by-field struct comparison charges for fields nobody uses.
9. Isolate `.onChange(of:)` side effects that don't affect rendering into a dedicated `ViewModifier` when the host view's body is expensive.
10. Prefer KeyPath/subscript bindings over `Binding(get:set:)` closures.
11. With `@Observable`, nested objects work fine; with legacy `ObservableObject`, pass nested objects directly to child views as their own `@ObservedObject`.
12. Never store closures in custom `@Environment`/`@FocusedValue` keys; keep `@Entry` defaults stable (no `Model()`/`Date()`/`UUID()` expressions); keep high-frequency values (scroll offset, drag position, per-frame geometry) out of the environment — coarsen them in an `@Observable` model instead; delete `@Environment`/`@FocusedValue` declarations nothing in the body reads.

## See also

- `performance.md` in this skill — `Equatable` views, per-item view model patterns for scroll-driven visibility, and other rendering-cost topics referenced above.
- `ios-accessibility` skill — VoiceOver, Dynamic Type, and other accessibility-specific state concerns.
- `ios-localization` skill — localized string and pluralization data flow beyond `Text`/`LocalizedStringKey` construction.
