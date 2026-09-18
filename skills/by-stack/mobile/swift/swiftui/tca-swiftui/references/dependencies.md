# Dependencies — @DependencyClient, Registration & Testing

## Struct-of-Closures Pattern

TCA uses struct-of-closures with `@DependencyClient` macro instead of protocols:

```swift
@DependencyClient
struct AudioPlayerClient {
    var loop: (_ url: URL) async throws -> Void
    var play: (_ url: URL) async throws -> Void
    var setVolume: (_ volume: Float) async -> Void
    var stop: () async -> Void
}
```

**Benefits over protocols:**
- Endpoint-level granularity in tests (override only what you need)
- Auto-generated `unimplemented` defaults for `testValue`
- Memberwise initializer for constructing test/live values
- No mock classes needed

## DependencyKey Registration

```swift
extension AudioPlayerClient: DependencyKey {
    static let liveValue = AudioPlayerClient(
        play: { url in /* real AVAudioPlayer implementation */ },
        stop: { /* stop playback */ }
    )
}

extension DependencyValues {
    var audioPlayer: AudioPlayerClient {
        get { self[AudioPlayerClient.self] }
        set { self[AudioPlayerClient.self] = newValue }
    }
}
```

### Key Distinction

| Value | Context | Purpose |
|-------|---------|---------|
| `liveValue` | Production app | Real implementation |
| `testValue` | XCTest context | Auto-generated as unimplemented with `@DependencyClient` |
| `previewValue` | SwiftUI Previews | Falls back to testValue if not defined |

## Usage in Reducers

```swift
@Reducer
struct Feature {
    @Dependency(\.apiClient) var apiClient
    @Dependency(\.continuousClock) var clock

    var body: some ReducerOf<Self> {
        Reduce<State, Action> { state, action in
            case .fetchButtonTapped:
                return .run { [apiClient] send in
                    let data = try await apiClient.fetch()
                    await send(.dataLoaded(data))
                }
        }
    }
}
```

## Critical: Don't Capture Dependencies at Static Init

```swift
// ❌ WRONG — captured once at static init, frozen forever
extension UserClient: DependencyKey {
    static let liveValue: UserClient = {
        let api = DependencyValues._current.apiClient  // Frozen!
        return UserClient(...)
    }()
}

// ✅ CORRECT — resolved at call time
extension UserClient: DependencyKey {
    static let liveValue = UserClient(
        currentUser: {
            @Dependency(\.apiClient) var apiClient
            return try await apiClient.fetchCurrentUser()
        }
    )
}
```

## Module Boundary Pattern (Enterprise)

```
ModuleStructure/
├── APIClientInterface/     # @DependencyClient + DependencyKey (testValue)
├── APIClientLive/          # DependencyKey extension with liveValue
├── FeatureA/               # @Dependency(\.apiClient) — only sees interface
└── App/                    # Links everything together
```

Feature modules depend only on interface modules, never on live implementations.

## Testing Dependencies

### Override in TestStore

```swift
let store = TestStore(initialState: Feature.State()) {
    Feature()
} withDependencies: {
    $0.apiClient.fetch = { _ in mockData }
    $0.continuousClock = ImmediateClock()
}
```

### Track Calls

```swift
let fetchCalls = LockIsolated<[String]>([])
store.dependencies.apiClient.fetch = { query in
    fetchCalls.withValue { $0.append(query) }
    return mockResults
}
// Later: XCTAssertEqual(fetchCalls.value, ["expected query"])
```

### Unimplemented with Placeholder

```swift
// ❌ fatalError stops entire test suite
searchHistory: unimplemented("\(Self.self).searchHistory")

// ✅ Reports via XCTFail, continues with placeholder
searchHistory: unimplemented("\(Self.self).searchHistory", placeholder: [])
```

## Guard Against Test Host

```swift
@main struct MyApp: App {
    var body: some Scene {
        WindowGroup {
            if !_XCTIsTesting {
                RootView(store: Store(initialState: ...) { ... })
            }
        }
    }
}
```

This prevents the test host from instantiating production stores (which would trigger live dependency resolution during tests).
