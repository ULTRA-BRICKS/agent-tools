# Reducer Architecture — @Reducer Macro, Decomposition & State Design

## @Reducer Macro (TCA 1.4+)

Always use the `@Reducer` macro. It auto-generates `@CasePathable` for Actions and handles composition boilerplate.

```swift
@Reducer
struct Feature {
  @ObservableState
  struct State: Equatable {
    var count = 0
    @Presents var destination: Destination.State?
  }

  enum Action {
    case incrementButtonTapped
    case destination(PresentationAction<Destination.Action>)
  }

  var body: some ReducerOf<Self> {
    Reduce<State, Action> { state, action in
      switch action {
      case .incrementButtonTapped:
        state.count += 1
        return .none
      case .destination:
        return .none
      }
    }
    .ifLet(\.$destination, action: \.destination)
  }
}
```

### @Reducer on Enums (TCA 1.8+)

For Destination/Path reducers, use `@Reducer` directly on an enum to eliminate boilerplate:

```swift
// ✅ Modern — 4 lines instead of 24
@Reducer
enum Destination {
  case add(FormFeature)
  case detail(DetailFeature)
}
```

This auto-generates the State enum, Action enum, and Scope-based body.

## Composition Operators

| Operator | Use Case |
|----------|----------|
| `Scope(state: \.child, action: \.child)` | Child that always exists |
| `.ifLet(\.$destination, action: \.destination)` | Optional/presented child |
| `.forEach(\.items, action: \.items)` | IdentifiedArray collection |
| `.forEach(\.path, action: \.path)` | StackState navigation |

### Parent→Child Composition Pattern

```swift
@Reducer
struct ParentFeature {
  @ObservableState
  struct State: Equatable {
    var child: ChildFeature.State
    @Presents var destination: Destination.State?
    var path = StackState<Path.State>()
  }

  enum Action {
    case child(ChildFeature.Action)
    case destination(PresentationAction<Destination.Action>)
    case path(StackAction<Path.State, Path.Action>)
  }

  var body: some ReducerOf<Self> {
    // Child reducers BEFORE parent Reduce block
    Scope(state: \.child, action: \.child) {
      ChildFeature()
    }
    Reduce<State, Action> { state, action in
      // Parent's own logic here
    }
    .ifLet(\.$destination, action: \.destination)
    .forEach(\.path, action: \.path)
  }
}
```

**Key rules:**
- Child reducers in `body` run BEFORE the parent `Reduce` block
- Parent CAN read child state: `state.child.someProperty`
- Child CANNOT modify parent state — use delegate actions
- Don't send actions between sibling reducers — delegate up to parent

## State Design

### Value Semantics

State MUST be a value type (struct). TCA uses value semantics for copying, comparing, and testing.

### @ObservableState Registrar

`@ObservableState` embeds a hidden registrar (reference type). This is why you must NEVER capture whole state in effect closures — the registrar creates main-actor violations and potential memory corruption.

### @Shared State

```swift
@Shared(.appStorage("count")) var count = 0           // UserDefaults-backed
@Shared(.fileStorage(.syncUps)) var syncUps: IdentifiedArrayOf<SyncUp> = []  // File-backed
@Shared(.inMemory("data")) var data: SomeType          // In-memory global
```

Pass references using projected value: `ChildFeature.State(count: state.$count)`.

**Testing caveat:** When using @Shared with delegate actions, TestStore may show state mutations at unexpected points (on `send` instead of `receive`).

**Performance caveat:** @Shared can cause over-observation. Before TCA 1.7, equality checks prevented this automatically.

### When NOT to Use TCA State

Not everything needs to be in TCA state:
- **Hover/UI-only state** — if you won't write a test for it, consider `@State` in the view
- **Presentation animations** — SwiftUI `@State` is fine for purely visual state
- **Scroll position** — unless needed for state restoration

## Action Naming & Categories

### Three-Category Pattern (Recommended)

```swift
enum Action {
  case view(ViewAction)           // What the user did
  case `internal`(InternalAction) // Results of effects
  case delegate(DelegateAction)   // Communication to parent

  enum ViewAction {
    case didAppear
    case didTapLoginButton
  }
  enum InternalAction {
    case listResult(Result<[Todo], TodoError>)
  }
  enum DelegateAction {
    case userLoggedIn
  }
}
```

Parent MUST only observe child's `.delegate(...)` actions — never internal or view actions.

## When to Split vs. Combine

**Split when:**
- Feature has its own screen/view
- Feature has its own testable logic
- Feature could be extracted to an SPM module
- Feature's state is optional (use `ifLet` to avoid unnecessary work)

**Don't over-split:**
- Don't create a feature for every tiny UI component
- A child reducer should own meaningful logic, not just forward actions
