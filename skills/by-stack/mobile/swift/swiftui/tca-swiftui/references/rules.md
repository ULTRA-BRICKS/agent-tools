# TCA SwiftUI Architecture — Rules Quick Reference

## Do's — Always Follow

1. **Use `@Reducer` macro on all reducers** (TCA 1.4+) — never bare `struct Feature: Reducer` conformance.
2. **Use `@ObservableState` on all State types** — enables direct store observation without WithViewStore.
3. **Define State and Action INSIDE the `@Reducer` struct** — macros cannot access types in extensions.
4. **Conform State to `Equatable`** — required for TestStore equality assertions.
5. **Use `@Reducer enum` for Destination/Path reducers** (TCA 1.8+) — eliminates massive Scope/Action boilerplate.
6. **Name actions by "what happened"** — `incrementButtonTapped`, `onAppear`, `songsResponse(Result<...>)`. Never imperative names like `fetchSongs`.
7. **Use delegate actions for child→parent communication** — parent observes ONLY `.delegate(...)` cases, never child internals.
8. **Use `Reduce<State, Action> { }` with explicit generics** — fixes autocomplete issues in Xcode.
9. **Place `Scope` before `Reduce` in body** — child reducers run before parent (ReducerBuilder ordering).
10. **Use `@DependencyClient` struct-of-closures for dependencies** — not protocols. Auto-generates unimplemented test values.
11. **Capture only needed values before `.run` closures** — `let count = state.count` then use in closure.
12. **Use enum-with-cases for cancel IDs** — `enum CancelID { case request, timer }`. Never empty enums.
13. **Access store properties directly** — `store.count`, `store.send(.tapped)`, `@Bindable var store` for `$` bindings.
14. **Annotate test methods with `@MainActor`** — required for correct TestStore behavior.
15. **Guard app entry point** — `if !_XCTIsTesting { RootView(...) }` prevents test host from creating stores.

## Don'ts — Critical Anti-Patterns

### Never: WithViewStore (deprecated 1.7+)

```swift
// ❌ Deprecated — observes ALL state, unnecessary overhead
WithViewStore(store, observe: { $0 }) { viewStore in
    Text(viewStore.count.description)
}

// ✅ Direct store access (modern)
Text(store.count.description)
```

### Never: Capture whole @ObservableState in effects

```swift
// ❌ Captures hidden registrar (reference type) — causes main-actor violations
return .run { [state] send in
    await send(.delegate(state.information))
}

// ✅ Extract only needed values
let information = state.information
return .run { send in
    await send(.delegate(information))
}
```

### Never: Use actions for code reuse

```swift
// ❌ Unnecessary action traverses ENTIRE reducer tree (~6ms per action in large apps)
case .buttonTapped:
    return .send(.loadData)
case .loadData:
    return .run { ... }

// ✅ Helper function — no action overhead
case .buttonTapped:
    return loadDataEffect(&state)

func loadDataEffect(_ state: inout State) -> Effect<Action> {
    state.isLoading = true
    return .run { send in ... }
}
```

### Never: Empty enum cancel IDs

```swift
// ❌ Swift prunes empty types in release builds — cancellation breaks silently
enum RequestID {}
return .run { ... }.cancellable(id: RequestID.self)

// ✅ Enum with cases
enum CancelID { case request, timer, websocket }
return .run { ... }.cancellable(id: CancelID.request)
```

### Never: Make Action Equatable (unnecessary since TCA 1.4)

```swift
// ❌ Unnecessary boilerplate
enum Action: Equatable { ... }

// ✅ Case key paths handle matching
enum Action { ... }
```

### Never: State/Action in extensions with @Reducer macro

```swift
// ❌ Cryptic compiler errors — macro can't see extension types
@Reducer struct Feature { }
extension Feature {
    struct State: Equatable { ... }
}

// ✅ Define inside the struct body
@Reducer struct Feature {
    @ObservableState struct State: Equatable { ... }
    enum Action { ... }
}
```

### Never: Observe child internal actions from parent

```swift
// ❌ Breaks when child refactors internals
case .child(.onHovered(url)):
    state.statusBar = url.string

// ✅ Only observe delegate actions
case .child(.delegate(.didHover(url))):
    state.statusBar = url.string
```

### Never: Expensive work in reducers

Reducers run on the main thread. Even a no-op action costs ~6ms in large apps (37.5% of 60fps frame budget). Offload all expensive work to `.run` effects.

### Never: Nest NavigationStacks

NavigationStacks cannot be nested. Each tab in a TabView owns its own `StackState` and `NavigationStack`. Never wrap TabView in a NavigationStack.
