# Performance — Action Costs, Debugging & Optimization

## Key Performance Facts

**From Arc Browser (largest known TCA codebase):**
- 5 lines of text processing took **9 seconds CPU** in vanilla TCA
- No-op action cost: **~6ms** (37.5% of 60fps frame budget at 16ms/frame)
- Solution: separated high-frequency operations from TCA, used dependency clients

**From Lapse (social media app):**
- List of 10,000 items with deeply nested state: stack frame count over 100 mid-scroll
- "The lag becomes unbearable even on iPhone 13 Pro Max"
- Solution: disconnected/detached stores for list items

## Performance Rules

### Reducers Run on Main Thread
Never do expensive operations in reducers. Use `.run` effects for async/expensive work.

### Scope Functions Run for Every Action
Code inside `scope` functions runs for every action sent to the system. Must be O(1) — no collection iteration, no heavy computed properties.

### Computed Properties Re-Create Objects
Computed properties in state re-create objects each time called. Pre-compute heavy data in the reducer, store in state.

### Avoid High-Frequency Actions
Even a no-op action costs ~6ms in large apps. At 60fps, that's 37.5% of frame budget. Use debounce or check conditions in the view layer before sending actions.

**Rule of thumb:** Avoid sending >10 actions/second without debouncing.

## Debugging Tools

### _printChanges()

Prints every action and resulting state diff:

```swift
var body: some ReducerOf<Self> {
    Reduce<State, Action> { state, action in ... }
    ._printChanges()
}
```

**Caveat:** Doesn't print boxed content of `@Shared` in binding situations.

### .signpost()

Higher-order reducer that instruments every action with `os_signpost` for profiling in Instruments:

```swift
var body: some ReducerOf<Self> {
    Reduce<State, Action> { state, action in ... }
    .signpost()
}
```

## Common Performance Pitfalls

### Don't Use Actions for Code Reuse

```swift
// ❌ Extra action traverses entire reducer tree
case .buttonTapped:
    return .send(.loadData)

// ✅ Helper function — zero overhead
case .buttonTapped:
    return loadDataEffect(&state)
```

### Make State Optional When Not Always Visible

Browser Company finding: "The command bar was not optional, so its state/reducer and view layer side effects would run even when it wasn't visible." Fix: make state optional + use `ifLet`.

### Not Everything Needs TCA State

Sidebar hover was a state property, but it only needs to live at the View layer. If you won't write a test for it, use `@State` instead.

### Legacy ViewStore Observation

`WithViewStore(store, observe: { $0 })` observes ALL state — every change triggers re-render. With modern TCA (`@ObservableState`), this is handled automatically via granular observation.

## Optimization Checklist

```
[ ] No expensive work in reducer body
[ ] Scope closures are O(1)
[ ] High-frequency actions debounced
[ ] Optional state for features not always visible
[ ] UI-only state uses @State, not TCA state
[ ] Heavy computed properties pre-computed in reducer
[ ] _printChanges removed before commit
[ ] .signpost removed before commit
```
