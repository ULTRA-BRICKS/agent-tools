# Snapshot Testing

## How to Use This Reference

Read this when setting up snapshot tests (using swift-snapshot-testing by Point-Free), configuring device pinning, managing recording modes in CI, testing SwiftUI views with UIHostingController, or reducing snapshot flakiness from anti-aliasing and rendering differences.

---

## Device Pinning

Always pin device config -- never rely on host simulator.

```swift
// WRONG -- renders at whatever simulator is running
assertSnapshot(of: vc, as: .image)

// CORRECT -- deterministic device config
assertSnapshot(of: vc, as: .image(on: .iPhone13Pro))
```

Different Xcode/macOS/simulator versions produce different renderings. Lock versions in CI config.

---

## Recording Strategy

Use `withSnapshotTesting(record:)` -- not legacy `isRecording`.

```swift
withSnapshotTesting(record: .missing) {
    assertSnapshot(of: vc, as: .image(on: .iPhone13Pro))
}
```

### Record modes

| Mode | Behavior |
|------|----------|
| `.all` | Re-record all snapshots |
| `.missing` | Record only new snapshots, compare existing |
| `.failed` | Re-record only snapshots that failed comparison |
| `.never` | Never record, fail if snapshot is missing |

---

## CI Configuration

CI MUST use `record: .never`. Auto-recording on CI creates uncommitted snapshots and breaks main branch.

```swift
class SnapshotTestCase: XCTestCase {
    override func invokeTest() {
        let isCI = ProcessInfo.processInfo.environment["CI"] == "true"
        withSnapshotTesting(record: isCI ? .never : .missing) {
            super.invokeTest()
        }
    }
}
```

Swift Testing: Use `@Suite(.snapshots(record: .never))` -- no `invokeTest()`.

---

## SwiftUI Setup

SwiftUI views need `UIHostingController` -- not raw `View`.

```swift
let vc = UIHostingController(rootView: MySwiftUIView())
assertSnapshot(of: vc, as: .image(on: .iPhone13Pro))
```

**GOTCHA:** Without explicit size or device config, UIHostingController may render at zero size.

### Dark mode / Dynamic Type / RTL

```swift
vc.overrideUserInterfaceStyle = .dark
// or:
let view = MyView().environment(\.colorScheme, .dark)
let vc = UIHostingController(rootView: view)
```

---

## Precision Settings

Use `precision: 0.99, perceptualPrecision: 0.98` to reduce anti-aliasing flakiness on CI.

```swift
assertSnapshot(
    of: vc,
    as: .image(on: .iPhone13Pro, precision: 0.99, perceptualPrecision: 0.98)
)
```

---

## Multi-Strategy Testing

Use multiple strategies beyond `.image`:

| Strategy | Use case |
|----------|----------|
| `.image` | Visual regression |
| `.recursiveDescription` | View hierarchy structure |
| `.json` | Codable model snapshots |
| `.customDump` | Complex state objects |

---

## What NOT to Snapshot

- Animations or loading spinners
- Timestamps or dates
- Network-fetched images
- Random/dynamic content

Inject deterministic state for all snapshots.

---

## Multi-Config Matrix

Use `named:` parameter for multi-config matrix tests -- prevents file overwrites.

```swift
for style in [UIUserInterfaceStyle.light, .dark] {
    vc.overrideUserInterfaceStyle = style
    let name = style == .light ? "light" : "dark"
    assertSnapshot(of: vc, as: .image(on: .iPhone13Pro), named: name)
}
```

---

## Git LFS

Use Git LFS for `__Snapshots__/` at scale. Snapshot images bloat the repository quickly.

```
# .gitattributes
**/\__Snapshots__/** filter=lfs diff=lfs merge=lfs -text
```

---

## Preview Reuse

Reuse SwiftUI preview configurations for snapshot tests (Mercari pattern). Define shared configurations that serve both previews and snapshots.
