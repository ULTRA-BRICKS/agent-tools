# Navigation — Tree-Based, Stack-Based & Dismissal Patterns

## Tree-Based Navigation (Modals, Sheets, Alerts)

Uses optionals and enums. `nil` = dismissed, non-nil = presented.

### Reducer Pattern

```swift
@Reducer
struct ParentFeature {
  @ObservableState
  struct State: Equatable {
    @Presents var destination: Destination.State?
  }

  enum Action {
    case destination(PresentationAction<Destination.Action>)
    case addButtonTapped
  }

  @Reducer
  enum Destination {
    case addItem(AddItemFeature)
    case editItem(EditItemFeature)
    case alert(AlertState<Alert>)
  }

  var body: some ReducerOf<Self> {
    Reduce<State, Action> { state, action in
      switch action {
      case .addButtonTapped:
        state.destination = .addItem(AddItemFeature.State())
        return .none
      case .destination:
        return .none
      }
    }
    .ifLet(\.$destination, action: \.destination)
  }
}
```

**Note:** `ifLet` uses `\.$destination` (projected value via `@Presents`) — note the `$` prefix.

### View Integration (Modern)

```swift
struct ParentView: View {
  @Bindable var store: StoreOf<ParentFeature>

  var body: some View {
    List { ... }
    .sheet(item: $store.scope(state: \.destination?.addItem, action: \.destination.addItem)) { store in
      AddItemView(store: store)
    }
    .alert($store.scope(state: \.destination?.alert, action: \.destination.alert))
  }
}
```

### Mutually Exclusive vs. Concurrent Presentations

**Mutually exclusive (only one at a time):** Single `@Presents var destination: Destination.State?` with enum.

**Can coexist (alert + sheet simultaneously):** Separate properties:
```swift
@Presents var addItem: AddItemFeature.State?
@Presents var alert: AlertState<AlertAction>?
@Presents var confirmation: ConfirmationDialogState<ConfAction>?
```

## Stack-Based Navigation (NavigationStack)

### Reducer Pattern

```swift
@Reducer
struct RootFeature {
  @ObservableState
  struct State: Equatable {
    var path = StackState<Path.State>()
  }

  enum Action {
    case path(StackAction<Path.State, Path.Action>)
  }

  @Reducer
  enum Path {
    case screenA(ScreenAFeature)
    case screenB(ScreenBFeature)
  }

  var body: some ReducerOf<Self> {
    Reduce<State, Action> { state, action in
      switch action {
      case .path(.element(id: _, action: .screenA(.delegate(.goToB)))):
        state.path.append(.screenB(ScreenBFeature.State()))
        return .none
      case .path:
        return .none
      }
    }
    .forEach(\.path, action: \.path)
  }
}
```

**Note:** `StackAction` is generic over TWO types (state + action), unlike `PresentationAction` (one type).

### View Integration (Modern)

```swift
struct RootView: View {
  @Bindable var store: StoreOf<RootFeature>

  var body: some View {
    NavigationStack(path: $store.scope(state: \.path, action: \.path)) {
      ContentView(store: store)
    } destination: { store in
      switch store.case {
      case .screenA(let store): ScreenAView(store: store)
      case .screenB(let store): ScreenBView(store: store)
      }
    }
  }
}
```

### Programmatic Push/Pop

```swift
state.path.append(.screenB(ScreenBFeature.State(id: id)))  // Push
state.path.removeLast()                                      // Pop last
state.path.removeAll()                                       // Pop to root
```

### Deep Linking

```swift
case .deepLinkReceived(let url):
    let parsed = parseDeepLink(url)
    state.path.removeAll()
    state.path.append(.screenA(ScreenAFeature.State(id: parsed.aId)))
    state.path.append(.screenB(ScreenBFeature.State(id: parsed.bId)))
    return .none
```

## Dismissal Patterns

### Method 1: Child-initiated via @Dependency(\.dismiss)

```swift
@Dependency(\.dismiss) var dismiss

case .closeButtonTapped:
    return .run { _ in await self.dismiss() }
```

### Method 2: Delegate actions (parent-initiated)

```swift
// Child sends delegate:
case .saveButtonTapped:
    return .send(.delegate(.didSave(state.item)))

// Parent handles and dismisses:
case .destination(.presented(.addItem(.delegate(.didSave(let item))))):
    state.items.append(item)
    state.destination = nil
    return .none
```

### Method 3: Direct state nil-out

```swift
state.destination = nil
```

**Caveat:** When setting `state.destination = nil` programmatically, the `.dismiss` PresentationAction is NOT automatically sent (unlike SwiftUI's swipe-to-dismiss).

## Navigation Anti-Patterns

1. **Single Destination enum for independent presentations** — can't show alert AND sheet simultaneously. Use separate `@Presents` properties.
2. **NavigationStacks cannot be nested** — each tab owns its own `StackState`.
3. **`navigationDestination` inside `ForEach`/`List`** — causes dismiss fights. Apply outside lazy containers.
4. **Shared StackState across tabs** — each tab must own its own path.
5. **`NavigationLink(state:)` from child modules** — creates circular dependency. Use `Button` + delegate action instead.
6. **Deep linking with `navigationDestination` for 3+ levels** — known SwiftUI bug. Use `NavigationStack(path:)` binding.

## Tab-Based Apps

Each tab owns its own `StackState`:

```swift
@Reducer
struct AppFeature {
  @ObservableState
  struct State: Equatable {
    var feedTab = FeedTabFeature.State()   // owns its own path
    var searchTab = SearchTabFeature.State() // owns its own path
  }
}
```

`NavigationStack` goes inside each tab's view, never wrapping `TabView`.
