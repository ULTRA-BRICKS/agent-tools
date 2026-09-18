# Anti-Patterns — AI Mistakes, God Reducers & Detection Checklist

## AI Tools Consistently Fail at TCA

From empirical testing (DoubleDotDevelopment.co.uk):
- **0 out of 3 AI tools compiled TCA code on first attempt**
- Neither ChatGPT nor Cursor used `@Reducer` macro
- Neither used `@ObservableState`
- Microsoft Copilot generated 14 errors and was unable to compile at all

The root cause: TCA syntax changed substantially across versions, and AI training data mixes old and new patterns.

## Top 14 AI Anti-Patterns — Detection Checklist

Use this checklist when reviewing AI-generated TCA code:

### 1. WithViewStore instead of direct store access
```swift
// ❌ AI generates this (deprecated 1.7+)
WithViewStore(store, observe: { $0 }) { viewStore in ... }
// ✅ store.property directly
```

### 2. Environment instead of @Dependency
```swift
// ❌ Pre-1.0 pattern
struct FeatureEnvironment { var apiClient: APIClient }
// ✅ @Dependency(\.apiClient) var apiClient
```

### 3. Combine-based Effects
```swift
// ❌ Pre-0.40.0
Effect<Action, Never>.task { ... }
Effect.fireAndForget { ... }
// ✅ Effect.run { send in ... }
```

### 4. State/Action as top-level types
```swift
// ❌ Not inside @Reducer
struct FeatureState: Equatable { ... }
enum FeatureAction { ... }
// ✅ Nested inside @Reducer struct
```

### 5. Missing @ObservableState on State
### 6. Missing @Reducer macro on Feature struct
### 7. Capturing whole @ObservableState in effects
### 8. Missing effect cancellation for long-running operations
### 9. God reducers — all logic in one massive switch

### 10. Deprecated APIs
- `EffectTask` → `Effect`
- `.task { }` → `.run { send in }`
- `.fireAndForget { }` → `.run { _ in }`

### 11. Making Action Equatable (unnecessary since 1.4)
### 12. Wrong body return type
```swift
// ❌
var body: some Reducer<State, Action> { ... }
// ✅
var body: some ReducerOf<Self> { ... }
```

### 13. Old scope syntax
```swift
// ❌
store.scope(state: { $0.child }, action: { .child($0) })
// ✅
store.scope(state: \.child, action: \.child)
```

### 14. Mixing legacy and modern patterns in the same feature
Never apply `@ObservableState` to a feature that still uses `WithViewStore`.

## God Reducer Signs

A reducer is a "god reducer" when:
- File is 800+ lines with a massive switch statement
- Handles logic for multiple screens/features
- State contains properties for many unrelated concerns
- Action enum has dozens of cases mixing UI, internal, and cross-feature logic
- Testing is impossible to isolate

**Real-world report:** App reducer so large Xcode couldn't scroll through it. Had to split into ~20 extension files.

### How to Decompose

1. Identify distinct feature boundaries (each screen = own reducer)
2. Extract child reducers with own State/Action types
3. Use Scope/ifLet/forEach to compose in parent
4. Make state optional where features aren't always visible
5. Use delegate actions for child→parent communication

## Enterprise Anti-Patterns

### Teams Reaching Into Other Stores

TCA has no built-in encapsulation — any parent can reach into any child's state/actions. Use delegate actions to enforce boundaries. For multi-team apps, consider separate/disconnected stores per team with communication via dependency clients.

### Build Time Issues with Macros

- SwiftSyntax clean debug: +15-20 seconds
- Release with WMO: 4+ minutes for SwiftSyntax alone
- Worst case: 4.3 min → 33.4 min (7.76x increase)
- Keep Xcode build configs named exactly "Debug" and "Release" (non-standard names break macros)

### Stack Overflow from Large State Trees

All TCA state is stack-allocated (structs). Deeply nested state trees can overflow. Use `@Cow` (Copy On Write) wrapper for deeply nested state.

### Known Memory Leak Patterns

- StackState/StackReducer: stores stack up after present/dismiss cycles
- Long-living effects with `.cancellable(id:)` preventing Store deallocation
- Combine-based Reducer.combine leaking (pre-async/await era)
