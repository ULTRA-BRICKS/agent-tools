# Migration — Legacy to Modern TCA (1.7+)

## Version Progression

| Version | Key Changes |
|---------|-------------|
| Pre-1.0 | `Reducer<State, Action, Environment>`, `pullback`, `combine` |
| 1.0 | `ReducerProtocol` → `Reducer`, `@Dependency` replaces Environment |
| 1.4 | `@Reducer` macro, `@CasePathable`, case key path testing |
| 1.5 | Store scoping with key paths |
| 1.7 | `@ObservableState`, ViewStore deprecated, IfLetStore/ForEachStore/SwitchStore deprecated |
| 1.8 | `@Reducer` on enums, auto-fill empty State/Action/body |
| 1.9 | `@Shared` state, TestStore send with case key paths |

## Per-Feature Migration Checklist (to 1.7+)

**Prerequisites:**
- [ ] TCA 1.4+ with `@Reducer` macro on all reducers
- [ ] All Action enums use `@CasePathable` (automatic with `@Reducer`)
- [ ] State and Action types inside `@Reducer` struct (not in extensions)

**Per Feature:**
- [ ] Add `@ObservableState` to State
- [ ] Replace `@PresentationState` with `@Presents`
- [ ] Remove `WithViewStore` wrappers — access store directly
- [ ] Replace `viewStore.property` with `store.property`
- [ ] Replace `viewStore.send(...)` with `store.send(...)`
- [ ] Replace `IfLetStore` with `if let store = store.scope(...)`
- [ ] Replace `ForEachStore` with `ForEach(store.scope(...))`
- [ ] Replace `SwitchStore`/`CaseLet` with `switch store.state` / `if let store.scope(...)`
- [ ] Replace `NavigationStackStore` with `NavigationStack(path: $store.scope(...))`
- [ ] Replace `sheet(store:)` with `sheet(item: $store.scope(...))`
- [ ] Replace `\.$child` with `\.child` in view scope key paths
- [ ] For iOS 16: wrap views in `WithPerceptionTracking { }`
- [ ] Use `@Bindable var store` when needing `$store` bindings
- [ ] Test feature after migration

## Migration Strategy: Incremental, Bottom-Up

### Phase 1: Leaf Features First
Features with NO child features. Add `@ObservableState` + remove `WithViewStore`.

### Phase 2: Mid-Level Features
Features that compose already-migrated children. Replace `IfLetStore`/`ForEachStore`/`SwitchStore`.

### Phase 3: Navigation Features
Replace `NavigationStackStore`, `sheet(store:)`, etc.

### Phase 4: Root Features
Root/app-level features last.

**Critical:** Migrate BOTH reducer (`@ObservableState`) and view (remove `WithViewStore`) simultaneously. Never one without the other.

**Compatibility:** Legacy and modern features CAN coexist. All deprecated APIs still work — they show deprecation warnings only.

## Key Syntax Transformations

### Scope

```swift
// Pre-1.5:
store.scope(state: { $0.child }, action: { .child($0) })
// 1.5+:
store.scope(state: \.child, action: \.child)
// 1.7+ for navigation bindings:
$store.scope(state: \.child, action: \.child)
```

### Sheet Modifier

```swift
// Legacy:
.sheet(store: store.scope(state: \.$child, action: \.child)) { store in ... }
// Modern:
.sheet(item: $store.scope(state: \.child, action: \.child)) { store in ... }
```

### NavigationStack Destination

```swift
// Legacy:
} destination: {
  switch $0 {
  case .screenA:
    CaseLet(/Path.State.screenA, action: Path.Action.screenA) { store in
      ScreenAView(store: store)
    }
  }
}

// Modern:
} destination: { store in
  switch store.case {
  case .screenA(let store): ScreenAView(store: store)
  }
}
```

### View Helpers — Legacy to Modern

| Legacy (Pre-1.7) | Modern (1.7+) |
|---|---|
| `IfLetStore(store) { }` | `if let store = store.scope(...) { }` |
| `ForEachStore(store) { }` | `ForEach(store.scope(...)) { }` |
| `SwitchStore(store) { CaseLet(...) }` | `switch store.state { if let store.scope(...) }` |
| `NavigationStackStore(store) { }` | `NavigationStack(path: $store.scope(...)) { }` |
| `WithViewStore(store) { }` | Direct `store.property` access |
| `.sheet(store:)` | `.sheet(item: $store.scope(...))` |
| `@PresentationState` | `@Presents` |

## Known Migration Issues

1. **@ObservableState + property wrappers** — macros conflict. Refactor away from property wrappers in State.
2. **iOS 16 requires `WithPerceptionTracking`** — runtime warning if forgotten, silent observation failures.
3. **Capturing @ObservableState in effects** — registrar is reference type, causes main-actor violations.
4. **`didSet` observers on State + @ObservableState** — can create infinite loops.
5. **Path reducer State must also have `@ObservableState`** — easy to forget.
6. **"Update NavigationAuthority bound path tried to update multiple times per frame"** — console warnings after migration (usually harmless).

## Migration Estimates

| App Size | Features | Estimate |
|----------|----------|----------|
| Small | ~10 features | 1-2 days |
| Medium | ~33 features | ~5 working days |
| Large | 50+ features, multiple teams | 2-4 weeks |

Code reduction after migration: ~7% fewer lines. 100% backwards compatible — incremental migration is safe.
