# Performance, Animations, and Charts

Hot-path optimization and update-scope control, image loading, Liquid Glass (iOS 26+), the animation system (implicit/explicit animations, transitions, `Animatable`, phase/keyframe animators), Swift Charts, macOS-specific view/window/scene patterns, and how to read an Instruments trace by hand.

## Table of Contents

- [Hot-Path Optimization & Update Control](#hot-path-optimization--update-control)
- [Image Loading & Optimization](#image-loading--optimization)
- [Liquid Glass (iOS 26+)](#liquid-glass-ios-26)
- [Animation System](#animation-system)
- [Swift Charts](#swift-charts)
- [macOS View, Window, and Scene Patterns](#macos-view-window-and-scene-patterns)
- [Instruments Trace Interpretation (requires your own trace-capture tooling)](#instruments-trace-interpretation-requires-your-own-trace-capture-tooling)

---

## Hot-Path Optimization & Update Control

### SwiftUI does not compare values before triggering updates

Guard writes yourself when a publisher or callback can repeat the same value:

```swift
// BAD - triggers a body re-evaluation even if the value is unchanged
.onReceive(publisher) { value in
    self.currentValue = value
}

// GOOD - only update when different
.onReceive(publisher) { value in
    if self.currentValue != value {
        self.currentValue = value
    }
}
```

### Gate hot paths by threshold, not by raw value

Hot paths are frequently executed code: scroll handlers, drag gestures, animation ticks.

```swift
// BAD - updates state on every scroll position change
.onPreferenceChange(ScrollOffsetKey.self) { offset in
    shouldShowTitle = offset.y <= -32   // fires constantly during scroll!
}

// GOOD - only update when a threshold is crossed
.onPreferenceChange(ScrollOffsetKey.self) { offset in
    let shouldShow = offset.y <= -32
    if shouldShow != shouldShowTitle {
        shouldShowTitle = shouldShow    // fires only when crossing threshold
    }
}
```

### Prefer ternaries over `if`/`else` branching for modifier values

`if`/`else` view branching produces `_ConditionalContent`, which loses structural identity and can recreate the underlying platform view on every branch switch. A ternary on a modifier's value keeps identity stable:

```swift
// Prefer
Text("Status")
    .foregroundStyle(isActive ? .green : .secondary)

// Over branching the whole view for a modifier-level difference
if isActive {
    Text("Status").foregroundStyle(.green)
} else {
    Text("Status").foregroundStyle(.secondary)
}
```

### Avoid `AnyView`

`AnyView` erases the underlying view type, defeating SwiftUI's diffing. Use `@ViewBuilder`, `Group`, or generics instead — reserve `AnyView` for cases with no alternative (e.g. heterogeneous view arrays that truly can't be typed otherwise).

### `scrollContentBackground` for opaque `ScrollView` backgrounds

If a `ScrollView` has an opaque, static, solid background, prefer `.scrollContentBackground(.visible)` plus a `.background(...)` over manual background layering — it lets the system optimize scroll-edge rendering.

### Pass only what a view needs

Avoid passing large "config"/"context" objects down when a view only reads a few fields:

```swift
// Good - pass specific values
ThemeSelector(theme: config.theme)
FontSizeSlider(fontSize: config.fontSize)

// Avoid - passing the entire config creates a broad dependency
ThemeSelector(config: config)   // notified of ALL config changes
```

With `ObservableObject`, any `@Published` change notifies all observers. `@Observable` narrows this to accessed properties, but passing an entire object still creates a wider dependency than passing the specific values used.

The same narrowing applies to row views: prefer `let` properties over holding an `@Environment(AppModel.self)` reference just to read one field.

```swift
// Bad - broad dependency on the entire model
struct ItemRow: View {
    @Environment(AppModel.self) private var model
    let item: Item
    var body: some View { Text(item.name).foregroundStyle(model.theme.primaryColor) }
}

// Good - narrow dependency
struct ItemRow: View {
    let item: Item
    let themeColor: Color
    var body: some View { Text(item.name).foregroundStyle(themeColor) }
}
```

### Don't store frequently-changing values in the environment

Every write to *any* environment key forces every view reading *any* key in that subtree to be re-checked. A high-frequency value — scroll offset, container size, drag translation, per-frame animation progress, timer ticks, hover location — flowing into an `@Entry` or `.environment(\.key, value)` becomes an invalidation storm.

Hold the raw value in an `@Observable` model and expose a **coarsened** derived value (a boolean threshold, not the raw measurement) so readers invalidate only when crossing a meaningful boundary:

```swift
@MainActor @Observable
final class ViewportModel {
    var width: CGFloat = 0 { didSet { isWide = width > 600 } }
    private(set) var isWide = false   // readers invalidate only when this flips
}
```

For purely visual effects driven by scroll position (opacity, scale, rotation), prefer `scrollTransition` or `visualEffect(in:)` — they push per-frame work to the renderer and skip body re-evaluation entirely. Reach for the `@Observable` + coarsening pattern only when the scroll-derived value must drive non-rendering logic (model updates, prefetches, sibling state).

> Source: "Optimize SwiftUI performance with Instruments" (WWDC25, session 306)

### `@Observable` dependency granularity — per-item state holders

When many list rows share a dependency on the same `@Observable` array, changing one element re-evaluates every row's body:

```swift
// BAD - all item views depend on the full favorites array
@Observable
class ModelData {
    var favorites: [Landmark] = []
    func isFavorite(_ landmark: Landmark) -> Bool { favorites.contains(landmark) }
}

struct LandmarkRow: View {
    let landmark: Landmark
    @Environment(ModelData.self) private var model
    var body: some View {
        HStack {
            Text(landmark.name)
            if model.isFavorite(landmark) { Image(systemName: "heart.fill") }
        }
    }
}

// GOOD - each item has its own observable view model
@Observable
class LandmarkViewModel {
    var isFavorite: Bool = false
}

struct LandmarkRow: View {
    let landmark: Landmark
    let viewModel: LandmarkViewModel
    var body: some View {
        HStack {
            Text(landmark.name)
            if viewModel.isFavorite { Image(systemName: "heart.fill") }
        }
    }
}
```

With the bad pattern, toggling one favorite marks the entire array changed, so every `LandmarkRow` re-runs its body. With per-item view models, only the toggled item's body runs. The same shape applies to scroll-derived per-row state: give each item its own `@Observable` whose properties track only that item's derived state (e.g. `isVisible`) so each row invalidates at most twice (enter/leave) regardless of scroll speed.

> Source: "Optimize SwiftUI performance with Instruments" (WWDC25, session 306)

### Equatable views

For views with expensive bodies, conform to `Equatable` and apply `.equatable()`:

```swift
struct ExpensiveView: View, Equatable {
    let data: SomeData
    static func == (lhs: Self, rhs: Self) -> Bool {
        lhs.data.id == rhs.data.id   // custom equality check
    }
    var body: some View { /* expensive computation */ }
}

ExpensiveView(data: data)
    .equatable()   // use custom equality
```

**Caution**: if you add new state or dependencies to the view, remember to update `==`.

### POD views for fast diffing

A view is POD (Plain Old Data) if it only contains simple value types and no property wrappers — SwiftUI diffs it with `memcmp`, the fastest path.

```swift
// POD view - fastest diffing
struct FastView: View {
    let title: String
    let count: Int
    var body: some View { Text("\(title): \(count)") }
}

// Non-POD view - property wrapper forces reflection/custom equality
struct SlowerView: View {
    let title: String
    @State private var isExpanded = false
    var body: some View { Text(title) }
}
```

**Advanced pattern**: wrap an expensive non-POD view in a POD parent so the fast `memcmp` comparison gates the more expensive internal diff:

```swift
// POD wrapper for fast diffing
struct ExpensiveView: View {
    let value: Int
    var body: some View { ExpensiveViewInternal(value: value) }
}

// Internal view with state - only diffed when `value` actually changes
private struct ExpensiveViewInternal: View {
    let value: Int
    @State private var item: Item?
    var body: some View { /* expensive rendering */ }
}
```

### Extract dedicated views instead of computed properties

Breaking a view up into dedicated `View`-conforming types is strongly preferred over extracting `body` logic into a computed property or method. `@ViewBuilder` on a property or method does **not** solve this — SwiftUI can only skip re-evaluating a subtree when it is a real view with its own identity, not when it's inlined via a computed property on the parent. Isolating a redraw trigger into a single-responsibility subview means SwiftUI can skip that subview's body entirely when its inputs haven't changed.

### Keep initializers small

View initializers should stay trivial. Flag any non-trivial work (parsing, formatting, I/O) for a move into a `.task()` modifier so it runs when the view actually appears, rather than every time the view value is constructed.

### Treat `body` as hot code

Assume `body` runs frequently. Move sorting, filtering, and formatting logic out of it — into models, computed properties updated on `onChange`, or `let` bindings computed once upstream.

```swift
// BAD - sorts array every body call
var body: some View {
    List(items.sorted { $0.name < $1.name }) { item in Text(item.name) }
}

// GOOD - compute once, update via onChange
@State private var sortedItems: [Item] = []
var body: some View {
    List(sortedItems) { item in Text(item.name) }
        .onChange(of: items) { _, newItems in
            sortedItems = newItems.sorted { $0.name < $1.name }
        }
}
```

The same applies to `List`/`ForEach` initializers directly: avoid expensive inline transforms like `items.filter { ... }` when the initializer runs repeatedly. Prefer deriving the transformed collection from the source of truth with `let`, or caching it in `@State` — but only cache in `@State` if you also own explicit invalidation logic (an `onChange` that recomputes it), or the UI goes stale.

### Avoid object creation and formatters in `body`

```swift
// BAD - creates a new formatter every body call
var body: some View {
    let formatter = DateFormatter()
    formatter.dateStyle = .long
    return Text(formatter.string(from: date))
}

// GOOD - static/stored formatter
private static let dateFormatter: DateFormatter = {
    let f = DateFormatter()
    f.dateStyle = .long
    return f
}()
var body: some View { Text(Self.dateFormatter.string(from: date)) }
```

Even better: avoid storing a formatter property at all unless genuinely required. `Text` accepts a format directly:

```swift
Text(Date.now, format: .dateTime.day().month().year())
Text(100, format: .currency(code: "USD"))
```

### Don't store derived state separately

```swift
// BAD - derived state stored separately
@State private var items: [Item] = []
@State private var itemCount: Int = 0   // unnecessary!

// GOOD - compute derived values
@State private var items: [Item] = []
var itemCount: Int { items.count }      // computed property
```

### Store built views, not escaping `@ViewBuilder` closures

```swift
// Anti-pattern: stores an escaping closure on the view.
struct CardView<Content: View>: View {
    let content: () -> Content
    var body: some View {
        VStack(alignment: .leading) { content() }
            .padding()
            .background(.ultraThinMaterial)
            .clipShape(.rect(cornerRadius: 8))
    }
}

// Preferred: store the built view value; the synthesized init calls the builder.
struct CardView<Content: View>: View {
    @ViewBuilder let content: Content
    var body: some View {
        VStack(alignment: .leading) { content }
            .padding()
            .background(.ultraThinMaterial)
            .clipShape(.rect(cornerRadius: 8))
    }
}
```

### Lazy loading for large collections

```swift
// BAD - creates all views immediately
ScrollView {
    VStack { ForEach(items) { item in ExpensiveRow(item: item) } }
}

// GOOD - creates views on demand
ScrollView {
    LazyVStack { ForEach(items) { item in ExpensiveRow(item: item) } }
}
```

Flag eager `VStack`/`HStack` with many children inside a `ScrollView` for conversion to `LazyVStack`/`LazyHStack`.

**iOS 26+**: nested scroll views containing lazy stacks now automatically defer loading their children until about to appear, matching top-level lazy stack behavior — this benefits patterns like horizontal photo carousels inside a vertical scroll view.

> Source: "What's new in SwiftUI" (WWDC25, session 256)

### Prefer `.task()` over `.onAppear()` for async work

`.task()` cancels its work automatically when the view disappears; `.onAppear()` does not.

```swift
struct DataView: View {
    @State private var data: [Item] = []
    var body: some View {
        List(data) { item in Text(item.name) }
            .task {
                data = await fetchData()   // cancelled automatically on disappear
            }
    }
}
```

### Off-main-thread closures must be `Sendable`

SwiftUI may call certain closures on a background thread for performance. They must be `Sendable` and must not access `@MainActor`-isolated state directly — capture the values you need instead.

Closures that may run off the main thread: `Shape.path(in:)`, the `visualEffect` closure, `Layout` protocol methods, the `onGeometryChange` transform closure.

```swift
// BAD - accessing @MainActor state directly
.visualEffect { content, geometry in
    content.blur(radius: self.pulse ? 5 : 0)   // compiler error: @MainActor isolated
}

// GOOD - capture the value
.visualEffect { [pulse] content, geometry in
    content.blur(radius: pulse ? 5 : 0)
}
```

> Source: "Explore concurrency in SwiftUI" (WWDC25, session 266)

### Common bottlenecks

- View invalidation storms from broad state changes
- Unstable identity in lists causing excessive diffing
- Heavy work in `body` (formatting, sorting, image decoding)
- Layout thrash from deep stacks or preference chains

When performance issues arise, profile with Instruments (SwiftUI template) to identify specific bottlenecks — see [Instruments Trace Interpretation](#instruments-trace-interpretation-requires-your-own-trace-capture-tooling) below.

### Debugging unexpected view updates

Use `Self._printChanges()` or `Self._logChanges()` (iOS 17+) to see which properties caused a body re-evaluation:

```swift
struct DebugView: View {
    @State private var count = 0
    @State private var name = ""
    var body: some View {
        #if DEBUG
        let _ = Self._logChanges()   // Xcode 15.1+: logs to com.apple.SwiftUI subsystem
        #endif
        VStack {
            Text("Count: \(count)")
            Text("Name: \(name)")
        }
    }
}
```

- `Self._printChanges()`: prints which properties changed to standard output.
- `Self._logChanges()` (iOS 17+): logs to the `com.apple.SwiftUI` subsystem, category "Changed Body Properties", via `os_log`.

Both print `@self` when the view value itself changed and `@identity` when the view's persistent data was recycled. Isolating a redraw trigger into a single-responsibility subview is usually the fix — see "Extract dedicated views" above.

### Summary checklist

- [ ] State updates check for value changes before assigning
- [ ] Hot paths (scroll, drag, per-frame) minimize/threshold-gate state updates
- [ ] Ternaries used for modifier-value branching instead of `if`/`else` view branching
- [ ] `AnyView` avoided in favor of `@ViewBuilder`/`Group`/generics
- [ ] Pass only needed values to views (avoid large config/context objects)
- [ ] Frequently-changing values not stored in the environment; coarsened where they must be
- [ ] Consider granular per-item `@Observable` dependencies for list rows
- [ ] Equatable conformance for expensive views (when it measurably helps)
- [ ] Consider POD view wrappers for advanced diffing optimization
- [ ] View logic extracted into dedicated views, not computed properties/methods
- [ ] View initializers stay trivial; heavy work moved to `.task()`
- [ ] Body kept simple and pure — no side effects, no repeated sorting/filtering/formatting
- [ ] No object/formatter creation in `body`; `Text` format style preferred over stored `DateFormatter`
- [ ] Derived state computed, not stored separately
- [ ] Escaping `@ViewBuilder` closures replaced with stored built views where possible
- [ ] Large lists/collections use `LazyVStack`/`LazyHStack`
- [ ] `.task()` preferred over `.onAppear()` for async work (automatic cancellation)
- [ ] Sendable closures (`Shape`, `visualEffect`, `Layout`, `onGeometryChange`) capture values instead of accessing `@MainActor` state
- [ ] `Self._logChanges()`/`Self._printChanges()` used to debug unexpected updates

---

## Image Loading & Optimization

### `AsyncImage` phase handling

```swift
// Good - handles loading and error states
AsyncImage(url: imageURL) { phase in
    switch phase {
    case .empty:
        ProgressView()
    case .success(let image):
        image.resizable().aspectRatio(contentMode: .fit)
    case .failure:
        Image(systemName: "photo").foregroundStyle(.secondary)
    @unknown default:
        EmptyView()
    }
}
.frame(width: 200, height: 200)
```

For custom placeholders, replace `ProgressView()` in the `.empty` case. Add `.transition(.opacity)` to the success case and `.animation(.easeInOut, value: imageURL)` to the container for fade-in transitions.

### Decoding and downsampling (optional optimization)

When you see `UIImage(data:)` usage — especially in scrollable lists, grids, galleries, or carousels — consider suggesting downsampling as an optional, not automatic, improvement.

```swift
// Current pattern - decodes full image on main thread; force unwrap can crash
Image(uiImage: UIImage(data: imageData)!)
    .resizable()
    .aspectRatio(contentMode: .fit)
    .frame(width: 200, height: 200)
```

```swift
// Suggested optimization - decode and downsample off the main thread
struct OptimizedImageView: View {
    let imageData: Data
    let targetSize: CGSize
    @State private var processedImage: UIImage?

    var body: some View {
        Group {
            if let processedImage {
                Image(uiImage: processedImage).resizable().aspectRatio(contentMode: .fit)
            } else {
                ProgressView()
            }
        }
        .task {
            processedImage = await decodeAndDownsample(imageData, targetSize: targetSize)
        }
    }

    private func decodeAndDownsample(_ data: Data, targetSize: CGSize) async -> UIImage? {
        await Task.detached {
            guard let source = CGImageSourceCreateWithData(data as CFData, nil) else { return nil }
            let options: [CFString: Any] = [
                kCGImageSourceThumbnailMaxPixelSize: max(targetSize.width, targetSize.height),
                kCGImageSourceCreateThumbnailFromImageAlways: true,
                kCGImageSourceCreateThumbnailWithTransform: true
            ]
            guard let cgImage = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else { return nil }
            return UIImage(cgImage: cgImage)
        }.value
    }
}
```

For production use, wrap the logic in an `actor` with scale-aware sizing and cache-disabled source options:

```swift
actor ImageProcessor {
    func downsample(data: Data, targetSize: CGSize) -> UIImage? {
        let scale = await UIScreen.main.scale
        let maxPixel = max(targetSize.width, targetSize.height) * scale
        let sourceOptions: [CFString: Any] = [kCGImageSourceShouldCache: false]
        guard let source = CGImageSourceCreateWithData(data as CFData, sourceOptions as CFDictionary) else { return nil }
        let downsampleOptions: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceThumbnailMaxPixelSize: maxPixel,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceShouldCacheImmediately: true
        ]
        guard let cgImage = CGImageSourceCreateThumbnailAtIndex(source, 0, downsampleOptions as CFDictionary) else { return nil }
        return UIImage(cgImage: cgImage)
    }
}
```

`kCGImageSourceShouldCache: false` on the source prevents the full-resolution image from being cached in memory. Multiplying `targetSize` by `UIScreen.main.scale` keeps thumbnails sharp on Retina displays. `kCGImageSourceShouldCacheImmediately: true` forces decoding at creation time rather than at first render.

Present this as an optional improvement for performance-sensitive scenarios — don't automatically rewrite every `UIImage(data:)` call site.

### `UIImage(named:)` caches in the system cache

Loading many images this way (sliders, galleries) can spike memory. For single-use or frequently-rotated images, bypass the cache:

```swift
// Caches in system cache -- memory builds up
let image = UIImage(named: "Wallpapers/image_001.jpg")

// No system caching -- memory stays flat
guard let path = Bundle.main.path(forResource: "Wallpapers/image_001.jpg", ofType: nil) else { return nil }
let image = UIImage(contentsOfFile: path)
```

### `NSCache` for controlled image caching

When image processing (resizing, filtering) is needed, use `NSCache` with a `countLimit` to bound memory instead of relying on system caching:

```swift
struct ImageCache {
    private let cache = NSCache<NSString, UIImage>()
    init(countLimit: Int = 50) { cache.countLimit = countLimit }
    subscript(key: String) -> UIImage? {
        get { cache.object(forKey: key as NSString) }
        nonmutating set {
            if let newValue {
                cache.setObject(newValue, forKey: key as NSString)
            } else {
                cache.removeObject(forKey: key as NSString)
            }
        }
    }
}
```

### SF Symbols

```swift
Image(systemName: "star.fill")
    .foregroundStyle(.yellow)
    .symbolRenderingMode(.multicolor)   // or .hierarchical, .palette, .monochrome

// Animated symbols (iOS 17+)
Image(systemName: "antenna.radiowaves.left.and.right")
    .symbolEffect(.variableColor)
```

Variants follow a naming convention: `star.circle.fill`, `star.square.fill`, `folder.badge.plus`.

### Checklist

- [ ] `AsyncImage` handles empty, success, and failure phases
- [ ] Downsampling considered (not auto-applied) for `UIImage(data:)` in performance-sensitive scenarios
- [ ] Decoding/downsampling happens off the main thread with appropriate target sizes
- [ ] `UIImage(contentsOfFile:)` used instead of `UIImage(named:)` for single-use/rotated images
- [ ] `NSCache` with a bound `countLimit` used for processed-image caching
- [ ] SF Symbols use appropriate rendering modes

---

## Liquid Glass (iOS 26+)

Liquid Glass is Apple's design language introduced in iOS 26: translucent, dynamic surfaces that respond to content and user interaction. **Only adopt Liquid Glass when explicitly requested by the user** — do not proactively convert existing UI to glass effects.

### Availability

All Liquid Glass APIs require iOS 26+. Always provide a fallback:

```swift
if #available(iOS 26, *) {
    // Liquid Glass implementation
} else {
    // Fallback using materials
}
```

### `glassEffect` modifier

```swift
.glassEffect(_ glass: Glass = .regular, in shape: some Shape = .rect, isEnabled: Bool = true)
```

```swift
Text("Hello").padding().glassEffect()                          // default regular style, rect shape
Text("Rounded Glass").padding().glassEffect(in: .rect(cornerRadius: 16))
Image(systemName: "star").padding().glassEffect(in: .circle)
Text("Capsule").padding(.horizontal, 20).padding(.vertical, 10).glassEffect(in: .capsule)
```

`Glass` exposes exactly three static values — **there is no `.prominent`**:

```swift
.glassEffect(.regular)    // standard glass appearance (most common)
.glassEffect(.clear)      // nearly invisible glass surface
.glassEffect(.identity)   // no-op / pass-through glass
```

To make a surface read as more prominent, increase tint opacity instead of reaching for a non-existent `.prominent` property.

```swift
// Tinting
.glassEffect(.regular.tint(.blue))
.glassEffect(.regular.tint(.red.opacity(0.3)))

// Interactivity - responds to touch/pointer hover
.glassEffect(.regular.interactive())
.glassEffect(.regular.tint(.blue).interactive())
```

**Important**: only use `.interactive()` on elements that actually respond to user input (buttons, tappable views, focusable elements).

### `GlassEffectContainer`

**Glass cannot sample other glass.** The material reflects/refracts light by sampling content from an area larger than itself; nearby glass elements in different containers produce inconsistent results because they can't sample each other. `GlassEffectContainer` gives grouped elements a shared sampling region for a consistent appearance.

```swift
GlassEffectContainer {
    HStack {
        Button("One") { }.glassEffect()
        Button("Two") { }.glassEffect()
    }
}

// With explicit spacing that should match the actual layout spacing
GlassEffectContainer(spacing: 24) {
    HStack(spacing: 24) {
        GlassChip(icon: "pencil")
        GlassChip(icon: "eraser")
        GlassChip(icon: "trash")
    }
}
```

> Source: "Build a SwiftUI app with the new design" (WWDC25, session 323)

### Glass button styles

```swift
Button("Action") { }.buttonStyle(.glass)
Button("Primary Action") { }.buttonStyle(.glassProminent)   // higher visibility

// Manual control
Button(action: { }) {
    Label("Settings", systemImage: "gear").padding()
}
.glassEffect(.regular.interactive(), in: .capsule)
```

### Morphing transitions

Use `glassEffectID` with `@Namespace` to morph smoothly between glass elements. Requirements: both views share the same `glassEffectID` and `@Namespace`, both are wrapped in `GlassEffectContainer`, and the animation is applied to the container or a parent.

```swift
struct MorphingExample: View {
    @Namespace private var animation
    @State private var isExpanded = false

    var body: some View {
        GlassEffectContainer {
            if isExpanded {
                ExpandedCard().glassEffect().glassEffectID("card", in: animation)
            } else {
                CompactCard().glassEffect().glassEffectID("card", in: animation)
            }
        }
        .animation(.smooth, value: isExpanded)
    }
}
```

### Modifier order

Apply `glassEffect` **after** layout and visual modifiers — it must be last:

```swift
// CORRECT order
Text("Label")
    .font(.headline)             // 1. typography
    .foregroundStyle(.primary)   // 2. color
    .padding()                   // 3. layout
    .glassEffect()               // 4. glass effect LAST

// WRONG order
Text("Label")
    .glassEffect()   // applied too early
    .padding()
    .font(.headline)
```

### Complete examples

```swift
// Toolbar with glass buttons and a materials-based fallback
struct GlassToolbar: View {
    var body: some View {
        if #available(iOS 26, *) {
            GlassEffectContainer(spacing: 16) {
                HStack(spacing: 16) {
                    ToolbarButton(icon: "pencil", action: { })
                    ToolbarButton(icon: "eraser", action: { })
                    ToolbarButton(icon: "scissors", action: { })
                    Spacer()
                    ToolbarButton(icon: "square.and.arrow.up", action: { })
                }
                .padding(.horizontal)
            }
        } else {
            HStack(spacing: 16) { /* ... fallback implementation */ }
        }
    }
}

struct ToolbarButton: View {
    let icon: String
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            Image(systemName: icon).font(.title2).frame(width: 44, height: 44)
        }
        .glassEffect(.regular.interactive(), in: .circle)
    }
}
```

```swift
// Segmented control with a glass implementation and a Picker fallback
struct GlassSegmentedControl: View {
    @Binding var selection: Int
    let options: [String]
    @Namespace private var animation

    var body: some View {
        if #available(iOS 26, *) {
            GlassEffectContainer(spacing: 4) {
                HStack(spacing: 4) {
                    ForEach(options.indices, id: \.self) { index in
                        Button(options[index]) {
                            withAnimation(.smooth) { selection = index }
                        }
                        .padding(.horizontal, 16)
                        .padding(.vertical, 8)
                        .glassEffect(
                            selection == index
                                ? .regular.tint(.accentColor.opacity(0.4)).interactive()
                                : .regular.interactive(),
                            in: .capsule
                        )
                        .glassEffectID(selection == index ? "selected" : "option\(index)", in: animation)
                    }
                }
                .padding(4)
            }
        } else {
            Picker("Options", selection: $selection) {
                ForEach(options.indices, id: \.self) { index in Text(options[index]).tag(index) }
            }
            .pickerStyle(.segmented)
        }
    }
}
```

### Fallback strategies

```swift
if #available(iOS 26, *) {
    content.glassEffect()
} else {
    content.background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 16))
}
```

Materials, from closest to farthest from a glass appearance: `.ultraThinMaterial`, `.thinMaterial`, `.regularMaterial`, `.thickMaterial`, `.ultraThickMaterial`.

```swift
extension View {
    @ViewBuilder
    func glassEffectWithFallback(
        _ glass: Glass = .regular,
        in shape: some Shape = .rect,
        fallbackMaterial: Material = .ultraThinMaterial
    ) -> some View {
        if #available(iOS 26, *) {
            self.glassEffect(glass, in: shape)
        } else {
            self.background(fallbackMaterial, in: shape)
        }
    }
}
```

### Design system notes

- **Toolbar icons** use monochrome rendering by default in the new design; use `tint(_:)` only to convey meaning (a call to action), not for visual effect.
- **Sheet presentations**: partial-height sheets get a Liquid Glass background by default. Remove a previously-set custom `presentationBackground(_:)` to let the new material show. Sheets can morph out of the glass controls that present them via `navigationZoomTransition`.
- **Scroll edge effects**: an automatic scroll edge effect blurs/fades content under system toolbars to keep controls legible. Remove custom background-darkening effects behind bar items — they interfere with it.

> Source: "Build a SwiftUI app with the new design" (WWDC25, session 323)

### Best practices

**Do**: use `GlassEffectContainer` for grouped glass elements; apply glass after layout modifiers; use `.interactive()` only on tappable elements; match container spacing to layout spacing; provide material-based fallbacks for older iOS; keep glass shapes consistent within a feature; remove custom `presentationBackground(_:)` on sheets.

**Don't**: apply glass to every element; use `.interactive()` on static content; mix corner radii arbitrarily; forget `#available` checks; apply glass before padding/frame modifiers; nest `GlassEffectContainer` unnecessarily; add custom darkening backgrounds behind toolbars.

### Checklist

- [ ] `#available(iOS 26, *)` with a fallback
- [ ] `GlassEffectContainer` wraps grouped elements
- [ ] `.glassEffect()` applied after layout modifiers
- [ ] `.interactive()` only on user-interactable elements
- [ ] `glassEffectID` with `@Namespace` for morphing
- [ ] Consistent shapes and spacing across a feature; container spacing matches layout spacing
- [ ] Tint opacity used instead of the non-existent `.prominent` for emphasis

---

## Animation System

### Core concepts

State changes trigger view updates; SwiftUI animates the transition between the current render tree and the new one. Animatable properties are interpolated (~60 fps). Animations are additive and cancelable: they always start from the current render-tree state and blend smoothly when interrupted.

### Implicit vs explicit animations

Use `.animation(_:value:)` to animate a specific value's changes:

```swift
// GOOD - uses the value parameter
Rectangle()
    .frame(width: isExpanded ? 200 : 100, height: 50)
    .animation(.spring, value: isExpanded)
    .onTapGesture { isExpanded.toggle() }

// BAD - deprecated, animates all changes unexpectedly
Rectangle()
    .frame(width: isExpanded ? 200 : 100, height: 50)
    .animation(.spring)   // deprecated!
```

Use `withAnimation` for event-driven state changes:

```swift
// GOOD
Button("Toggle") { withAnimation(.spring) { isExpanded.toggle() } }

// BAD - no animation context, abrupt change
Button("Toggle") { isExpanded.toggle() }
```

**When to use which**: implicit for animations tied to a specific value with a precise view-tree scope; explicit for event-driven animations (taps, gestures).

### Placement and selective animation

Place `.animation(_:value:)` *after* the properties it should animate:

```swift
// GOOD
Rectangle()
    .frame(width: isExpanded ? 200 : 100, height: 50)
    .foregroundStyle(isExpanded ? .blue : .red)
    .animation(.default, value: isExpanded)   // animates both

// BAD - too early
Rectangle()
    .animation(.default, value: isExpanded)
    .frame(width: isExpanded ? 200 : 100, height: 50)
```

Animate only specific properties with multiple animation modifiers, or scoped animation (iOS 17+):

```swift
// Multiple modifiers - explicitly opt one property out
Rectangle()
    .frame(width: isExpanded ? 200 : 100, height: 50)
    .animation(.spring, value: isExpanded)     // animate size
    .foregroundStyle(isExpanded ? .blue : .red)
    .animation(nil, value: isExpanded)         // don't animate color

// iOS 17+ scoped animation
Rectangle()
    .foregroundStyle(isExpanded ? .blue : .red)   // not animated
    .animation(.spring) {
        $0.frame(width: isExpanded ? 200 : 100, height: 50)   // animated
    }
```

### Timing curves

| Curve | Use case |
|-------|----------|
| `.spring` | Interactive elements, most UI |
| `.easeInOut` | Appearance changes |
| `.bouncy` | Playful feedback (iOS 17+) |
| `.linear` | Progress indicators only |

```swift
.animation(.default.speed(2.0), value: flag)     // 2x faster
.animation(.default.delay(0.5), value: flag)     // delayed start
.animation(.default.repeatCount(3, autoreverses: true), value: flag)
```

```swift
// GOOD - appropriate timing for a button tap
Button("Tap") {
    withAnimation(.spring(response: 0.3, dampingFraction: 0.7)) { isActive.toggle() }
}
.scaleEffect(isActive ? 0.95 : 1.0)

// BAD - way too slow for button feedback
Button("Tap") {
    withAnimation(.easeInOut(duration: 1.0)) { isActive.toggle() }
}

// BAD - linear feels robotic/mechanical for UI
Rectangle().animation(.linear(duration: 0.5), value: isActive)
```

### Animation performance

Prefer transforms (GPU-accelerated) over layout changes:

```swift
// GOOD - fast
Rectangle()
    .frame(width: 100, height: 100)
    .scaleEffect(isActive ? 1.5 : 1.0)
    .offset(x: isActive ? 50 : 0)
    .rotationEffect(.degrees(isActive ? 45 : 0))
    .animation(.spring, value: isActive)

// BAD - layout changes are expensive
Rectangle()
    .frame(width: isActive ? 150 : 100, height: isActive ? 150 : 100)
    .padding(isActive ? 50 : 0)
```

Scope animations narrowly instead of applying them at the root:

```swift
// GOOD - scoped to the subview that actually changes
VStack {
    HeaderView()
    ExpandableContent(isExpanded: isExpanded)
        .animation(.spring, value: isExpanded)
    FooterView()
}

// BAD - animates the whole subtree
VStack {
    HeaderView()
    ExpandableContent(isExpanded: isExpanded)
    FooterView()
}
.animation(.spring, value: isExpanded)
```

Avoid animating in hot paths (see also [Hot-Path Optimization](#hot-path-optimization--update-control)):

```swift
// GOOD - gate by threshold
.onPreferenceChange(ScrollOffsetKey.self) { offset in
    let shouldShow = offset.y < -50
    if shouldShow != showTitle {
        withAnimation(.easeOut(duration: 0.2)) { showTitle = shouldShow }
    }
}

// BAD - fires constantly during scroll
.onPreferenceChange(ScrollOffsetKey.self) { offset in
    withAnimation { self.offset = offset.y }
}
```

### Disabling animations

```swift
// GOOD - disable via transaction
Text("Count: \(count)").transaction { $0.animation = nil }

// GOOD - disable from a parent context
DataView().transaction { $0.disablesAnimations = true }

// BAD - hacky zero-duration animation
Text("Count: \(count)").animation(.linear(duration: 0), value: count)
```

### Transactions

`withAnimation` is shorthand for `withTransaction` — transactions are the underlying mechanism for every SwiftUI animation.

```swift
withAnimation(.default) { flag.toggle() }

// Equivalent explicit form
var transaction = Transaction(animation: .default)
withTransaction(transaction) { flag.toggle() }
```

```swift
Rectangle()
    .frame(width: flag ? 100 : 50, height: 50)
    .transaction { t in t.animation = .default }
```

This behaves like the deprecated `.animation(_:)` without a `value:` parameter — it animates on every state change.

**Implicit animations override explicit ones** (later in the view tree wins):

```swift
Button("Tap") { withAnimation(.linear) { flag.toggle() } }
    .animation(.bouncy, value: flag)   // .bouncy wins!
```

Prevent an override or remove an animation entirely:

```swift
.transaction { t in t.disablesAnimations = true }
.transaction { $0.animation = nil }
```

Custom transaction keys (iOS 17+) pass metadata through the animation system:

```swift
struct ChangeSourceKey: TransactionKey { static let defaultValue: String = "unknown" }
extension Transaction {
    var changeSource: String {
        get { self[ChangeSourceKey.self] }
        set { self[ChangeSourceKey.self] = newValue }
    }
}

var transaction = Transaction(animation: .default)
transaction.changeSource = "server"
withTransaction(transaction) { flag.toggle() }

.transaction { t in
    if t.changeSource == "server" { t.animation = .smooth } else { t.animation = .bouncy }
}
```

### Transitions

**Property animations** interpolate values on a view that exists before *and* after a state change. **Transitions** animate a view being inserted or removed from the render tree.

```swift
// Property animation - same view, different properties
Rectangle()
    .frame(width: isExpanded ? 200 : 100, height: 50)
    .animation(.spring, value: isExpanded)

// Transition - view inserted/removed
if showDetail { DetailView().transition(.scale) }
```

**Transitions require an animation context outside the conditional.** Placing the animation modifier inside the conditional (attached to the view that's about to be removed) fails on removal:

```swift
// GOOD - animation outside the conditional
VStack {
    Button("Toggle") { showDetail.toggle() }
    if showDetail { DetailView().transition(.slide) }
}
.animation(.spring, value: showDetail)

// GOOD - explicit animation
Button("Toggle") { withAnimation(.spring) { showDetail.toggle() } }
if showDetail { DetailView().transition(.scale.combined(with: .opacity)) }

// BAD - animation inside the conditional (removed along with the view!)
if showDetail {
    DetailView().transition(.slide).animation(.spring, value: showDetail)
}

// BAD - no animation context; transition is ignored (just appears/disappears)
Button("Toggle") { showDetail.toggle() }
if showDetail { DetailView().transition(.slide) }
```

Built-in transitions: `.opacity` (fade, default), `.scale`, `.slide` (from leading edge), `.move(edge:)`, `.offset(x:y:)`.

```swift
.transition(.slide.combined(with: .opacity))                                  // parallel
.transition(.scale.combined(with: .opacity).combined(with: .offset(y: 20)))   // chained
```

Asymmetric transitions give insertion and removal different animations:

```swift
if showCard {
    CardView().transition(
        .asymmetric(
            insertion: .scale.combined(with: .opacity),
            removal: .move(edge: .bottom).combined(with: .opacity)
        )
    )
}
```

**Custom transitions**, pre-iOS 17 (`AnyTransition` + `ViewModifier`):

```swift
struct BlurModifier: ViewModifier {
    var radius: CGFloat
    func body(content: Content) -> some View { content.blur(radius: radius) }
}
extension AnyTransition {
    static func blur(radius: CGFloat) -> AnyTransition {
        .modifier(active: BlurModifier(radius: radius), identity: BlurModifier(radius: 0))
    }
}
.transition(.blur(radius: 10))
```

**Custom transitions**, iOS 17+ (`Transition` protocol):

```swift
struct BlurTransition: Transition {
    var radius: CGFloat
    func body(content: Content, phase: TransitionPhase) -> some View {
        content
            .blur(radius: phase.isIdentity ? 0 : radius)
            .opacity(phase.isIdentity ? 1 : 0)
    }
}
.transition(BlurTransition(radius: 10))
```

Inline blur/opacity toggling is *not* a transition — it won't animate on removal:

```swift
// BAD
if showContent {
    ContentView()
        .blur(radius: showContent ? 0 : 10)
        .opacity(showContent ? 1 : 0)
}
```

**Identity and transitions**: view identity changes trigger a transition, not a property animation.

```swift
// Triggers transition - different branches have different identities
if isExpanded { Rectangle().frame(width: 200, height: 50) } else { Rectangle().frame(width: 100, height: 50) }

// Triggers transition - .id() changes identity
Rectangle().id(flag).transition(.scale)

// Property animation - same view, same identity
Rectangle().frame(width: isExpanded ? 200 : 100, height: 50).animation(.spring, value: isExpanded)
```

### `matchedGeometryEffect`

`matchedGeometryEffect(id:in:)` shares an `@Namespace` between two views so SwiftUI interpolates one view's frame into the other's across an identity change — the classic "hero" transition (e.g. a thumbnail morphing into a detail view). It is the pre-iOS-26, general-purpose counterpart to Liquid Glass's `glassEffectID` morphing (see above), which only applies to glass surfaces. Give the source and destination views the same id and namespace, and drive the switch with an animated state change; as with any transition, the animation must wrap the state change, not live inside the conditional branch being removed.

### The `Animatable` protocol

Enables custom property interpolation during animation:

```swift
protocol Animatable {
    associatedtype AnimatableData: VectorArithmetic
    var animatableData: AnimatableData { get set }
}
```

```swift
// GOOD - explicit animatableData
struct ShakeModifier: ViewModifier, Animatable {
    var shakeCount: Double
    var animatableData: Double {
        get { shakeCount }
        set { shakeCount = newValue }
    }
    func body(content: Content) -> some View {
        content.offset(x: sin(shakeCount * .pi * 2) * 10)
    }
}
extension View {
    func shake(count: Int) -> some View { modifier(ShakeModifier(shakeCount: Double(count))) }
}
Button("Shake") { shakeCount += 3 }.shake(count: shakeCount).animation(.default, value: shakeCount)

// BAD - missing animatableData (silent failure!); uses EmptyAnimatableData,
// so the animation jumps to the final value instead of interpolating
struct BadShakeModifier: ViewModifier {
    var shakeCount: Double
    func body(content: Content) -> some View { content.offset(x: sin(shakeCount * .pi * 2) * 10) }
}
```

Multiple properties use `AnimatablePair`, nested for 3+ properties:

```swift
struct ComplexModifier: ViewModifier, Animatable {
    var scale: CGFloat
    var rotation: Double
    var animatableData: AnimatablePair<CGFloat, Double> {
        get { AnimatablePair(scale, rotation) }
        set { scale = newValue.first; rotation = newValue.second }
    }
    func body(content: Content) -> some View {
        content.scaleEffect(scale).rotationEffect(.degrees(rotation))
    }
}

struct ThreePropertyModifier: ViewModifier, Animatable {
    var x: CGFloat
    var y: CGFloat
    var rotation: Double
    var animatableData: AnimatablePair<AnimatablePair<CGFloat, CGFloat>, Double> {
        get { AnimatablePair(AnimatablePair(x, y), rotation) }
        set { x = newValue.first.first; y = newValue.first.second; rotation = newValue.second }
    }
    func body(content: Content) -> some View {
        content.offset(x: x, y: y).rotationEffect(.degrees(rotation))
    }
}
```

#### `@Animatable` macro (iOS 26+)

Auto-synthesizes `animatableData` from all animatable stored properties, eliminating verbose manual conformance. Use `@AnimatableIgnored` to exclude properties that should not animate (directions, flags, identifiers).

```swift
// Before (manual)
struct Wedge: Shape {
    var startAngle: Angle
    var endAngle: Angle
    var drawClockwise: Bool
    var animatableData: AnimatablePair<Double, Double> {
        get { AnimatablePair(startAngle.radians, endAngle.radians) }
        set { startAngle = .radians(newValue.first); endAngle = .radians(newValue.second) }
    }
    func path(in rect: CGRect) -> Path { /* ... */ }
}

// After (@Animatable)
@Animatable
struct Wedge: Shape {
    var startAngle: Angle
    var endAngle: Angle
    @AnimatableIgnored var drawClockwise: Bool
    func path(in rect: CGRect) -> Path { /* ... */ }
}
```

Prefer `@Animatable` for any custom `Shape`, `AnimatableModifier`, or `Animatable`-conforming type with multiple properties; it works with any conforming type, not just `Shape`.

> Source: "What's new in SwiftUI" (WWDC25, session 256)

Reach for an explicit `animatableData` instead of the macro when the interpolated value needs custom logic that doesn't map 1:1 to a stored property — normalization, clamping, or a derived value. For an iOS 26+ deployment target, use `AnimatableValues`; for earlier targets, use `AnimatablePair` with `newValue.first`/`newValue.second`.

```swift
// iOS 26+: keep phase in 0..<2π and clamp amplitude during interpolation
struct WaveShape: Shape {
    var amplitude: CGFloat
    var phase: CGFloat
    var maxAmplitude: CGFloat
    var animatableData: AnimatableValues<CGFloat, CGFloat> {
        get { AnimatableValues(amplitude, phase) }
        set {
            amplitude = min(max(newValue.value.0, 0), maxAmplitude)
            phase = newValue.value.1.truncatingRemainder(dividingBy: 2 * .pi)
        }
    }
    func path(in rect: CGRect) -> Path { /* ... */ }
}
```

### Phase animations (iOS 17+)

Cycle through discrete phases automatically; each phase change is a separate animation.

```swift
// Triggered phase animation
Button("Shake") { trigger += 1 }
    .phaseAnimator([0.0, -10.0, 10.0, -5.0, 5.0, 0.0], trigger: trigger) { content, offset in
        content.offset(x: offset)
    }

// Infinite loop (no trigger)
Circle().phaseAnimator([1.0, 1.2, 1.0]) { content, scale in content.scaleEffect(scale) }
```

Enum phases read more clearly than raw value arrays:

```swift
enum BouncePhase: CaseIterable {
    case initial, up, down, settle
    var scale: CGFloat {
        switch self {
        case .initial: 1.0
        case .up: 1.2
        case .down: 0.9
        case .settle: 1.0
        }
    }
}
Circle().phaseAnimator(BouncePhase.allCases, trigger: trigger) { content, phase in
    content.scaleEffect(phase.scale)
}
```

Custom timing per phase:

```swift
.phaseAnimator([0, -20, 20], trigger: trigger) { content, offset in
    content.offset(x: offset)
} animation: { phase in
    switch phase {
    case -20: .bouncy
    case 20: .linear
    default: .smooth
    }
}
```

Prefer `phaseAnimator` over manual `DispatchQueue.asyncAfter` sequencing:

```swift
// BAD - manual, error-prone timing
Button("Animate") {
    withAnimation(.easeOut(duration: 0.1)) { offset = -10 }
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { withAnimation { offset = 10 } }
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { withAnimation { offset = 0 } }
}
```

### Keyframe animations (iOS 17+)

Precise timing control with exact values at specific times.

```swift
Button("Bounce") { trigger += 1 }
    .keyframeAnimator(initialValue: AnimationValues(), trigger: trigger) { content, value in
        content.scaleEffect(value.scale).offset(y: value.verticalOffset)
    } keyframes: { _ in
        KeyframeTrack(\.scale) {
            SpringKeyframe(1.2, duration: 0.15)
            SpringKeyframe(0.9, duration: 0.1)
            SpringKeyframe(1.0, duration: 0.15)
        }
        KeyframeTrack(\.verticalOffset) {
            LinearKeyframe(-20, duration: 0.15)
            LinearKeyframe(0, duration: 0.25)
        }
    }

struct AnimationValues {
    var scale: CGFloat = 1.0
    var verticalOffset: CGFloat = 0
}
```

Keyframe types: `CubicKeyframe` (smooth interpolation), `LinearKeyframe` (straight-line), `SpringKeyframe` (spring physics), `MoveKeyframe` (instant jump, no interpolation).

Multiple tracks run **in parallel**, each animating one property:

```swift
struct BellAnimation { var rotation: Double = 0; var scale: CGFloat = 1.0 }

Image(systemName: "bell.fill")
    .keyframeAnimator(initialValue: BellAnimation(), trigger: trigger) { content, value in
        content.rotationEffect(.degrees(value.rotation)).scaleEffect(value.scale)
    } keyframes: { _ in
        KeyframeTrack(\.rotation) {
            CubicKeyframe(15, duration: 0.1)
            CubicKeyframe(-15, duration: 0.1)
            CubicKeyframe(10, duration: 0.1)
            CubicKeyframe(-10, duration: 0.1)
            CubicKeyframe(0, duration: 0.1)
        }
        KeyframeTrack(\.scale) {
            CubicKeyframe(1.1, duration: 0.25)
            CubicKeyframe(1.0, duration: 0.25)
        }
    }
```

`KeyframeTimeline` queries animation values directly for testing or non-SwiftUI use:

```swift
let timeline = KeyframeTimeline(initialValue: AnimationValues()) {
    KeyframeTrack(\.scale) {
        CubicKeyframe(1.2, duration: 0.25)
        CubicKeyframe(1.0, duration: 0.25)
    }
}
let midpoint = timeline.value(time: 0.25)
print(midpoint.scale)   // value at 0.25 seconds
```

### Animation completion handlers (iOS 17+)

```swift
// One-shot completion with withAnimation
Button("Animate") {
    withAnimation(.spring) { isExpanded.toggle() } completion: { showNextStep = true }
}
```

Without a `value:` parameter, `.transaction`'s completion fires only once, ever. Use `.transaction(value:)` when the completion should refire on every value change:

```swift
// GOOD - refires on every trigger change
Circle()
    .scaleEffect(bounceCount % 2 == 0 ? 1.0 : 1.2)
    .transaction(value: bounceCount) { transaction in
        transaction.animation = .spring
        transaction.addAnimationCompletion { message = "Bounce \(bounceCount) complete" }
    }

// BAD - completion only fires ONCE (no value parameter)
Circle()
    .scaleEffect(bounceCount % 2 == 0 ? 1.0 : 1.2)
    .animation(.spring, value: bounceCount)
    .transaction { transaction in
        transaction.addAnimationCompletion { completionCount += 1 }   // fires once, ever
    }
```

### Debugging animations

```swift
// Slow down for inspection
#if DEBUG
.animation(.linear(duration: 3.0).speed(0.2), value: isExpanded)
#else
.animation(.spring, value: isExpanded)
#endif

// Debug modifier to log interpolated values
struct AnimationDebugModifier: ViewModifier, Animatable {
    var value: Double
    var animatableData: Double {
        get { value }
        set { value = newValue; print("Animation: \(newValue)") }
    }
    func body(content: Content) -> some View { content.opacity(value) }
}
```

### Quick reference

**Do**: use `.animation(_:value:)` with a value parameter; use `withAnimation` for event-driven animations; prefer transforms over layout changes; scope animations narrowly; choose appropriate timing curves; place transitions/animation modifiers outside conditionals; implement `animatableData` explicitly (or via `@Animatable`) for custom `Animatable` types; use `AnimatablePair`/`AnimatableValues` for multiple animated properties; use `phaseAnimator`/`keyframeAnimator` instead of manual `DispatchQueue` timing.

**Don't**: use the deprecated value-less `.animation(_:)`; animate layout properties in hot paths; apply broad animations at the root level; use linear timing for UI; animate on every frame in scroll handlers; put animation modifiers inside the conditional that removes the view; forget `animatableData` (silent failure — jumps instead of interpolating); expect a property animation when view identity changes.

---

## Swift Charts

Swift Charts is Apple's native charting framework for SwiftUI: `Chart` plus one or more marks (bar, line, area, point, rule, rectangle, sector).

### Availability

- `Chart`, custom axes, scales, `BarMark`, `LineMark`, `AreaMark`, `PointMark`, `RectangleMark`, `RuleMark`: iOS 16+
- `SectorMark`, built-in selection, scrollable chart axes: iOS 17+
- Data-driven plot types (`BarPlot`, `LinePlot`, etc.): iOS 18+
- `Chart3D` and Z-axis APIs: iOS 26+

```swift
if #available(iOS 17, *) {
    // Selection, SectorMark, scrollable axes
} else {
    // Base Chart, axes, scales, and core marks
}
```

### Core APIs

Always confirm `import Charts` is present before using `Chart`, `Chart3D`, `BarMark`, `SectorMark`, or `ChartProxy` — if chart types are unresolved, check this first.

```swift
import SwiftUI
import Charts
```

`Chart` is the root view:

```swift
Chart(sales) { item in
    BarMark(x: .value("Month", item.month), y: .value("Revenue", item.revenue))
}
```

Prefer `Identifiable` chart data models so identity stays stable as data changes:

```swift
struct SalesPoint: Identifiable {
    let id: UUID
    let month: String
    let revenue: Double
}
```

If the model can't conform, provide an explicit id key path: `Chart(sales, id: \.month) { ... }`.

Use `.value(_, _)` to describe what each axis value means — those labels are reused by axes, legends, and accessibility:

```swift
LineMark(x: .value("Day", entry.date), y: .value("Steps", entry.count))
```

### Chart types

```swift
// BarMark - stacking via .standard, .normalized, .center, .unstacked
BarMark(x: .value("Product", product.name), y: .value("Units", product.units))

// LineMark - interpolation: .linear, .monotone, .cardinal, .catmullRom, .stepStart, .stepCenter, .stepEnd
LineMark(x: .value("Day", day.date), y: .value("Steps", day.count))
    .interpolationMethod(.monotone)

// AreaMark
AreaMark(x: .value("Hour", sample.hour), y: .value("Temperature", sample.value), stacking: .unstacked)

// Ranged area - min/max bands, confidence intervals
AreaMark(x: .value("Day", sample.day), yStart: .value("Low", sample.low), yEnd: .value("High", sample.high))

// PointMark
PointMark(x: .value("Time", measurement.time), y: .value("Value", measurement.value))

// RectangleMark
RectangleMark(
    xStart: .value("Start Day", cell.startDay), xEnd: .value("End Day", cell.endDay),
    yStart: .value("Low", cell.low), yEnd: .value("High", cell.high)
)

// RuleMark
RuleMark(y: .value("Goal", 10_000)).foregroundStyle(.red)
```

`SectorMark` (iOS 17+) for pie/donut charts — `innerRadius` turns a pie into a donut, `angularInset` separates slices:

```swift
Chart(expenses) { expense in
    SectorMark(angle: .value("Amount", expense.amount), innerRadius: .ratio(0.6), angularInset: 2)
        .foregroundStyle(by: .value("Category", expense.category))
}
```

iOS 18+ adds data-driven plot wrappers (`AreaPlot`, `BarPlot`, `LinePlot`, `PointPlot`, `RectanglePlot`, `RulePlot`, `SectorPlot`). `LinePlot`/`AreaPlot` also plot mathematical functions directly:

```swift
if #available(iOS 18, *) {
    Chart { LinePlot(x: "x", y: "sin(x)") { x in sin(x) } }
        .chartXScale(domain: -Double.pi ... Double.pi)
        .chartYScale(domain: -1.5 ... 1.5)
}
```

**`Chart3D` (iOS 26+)** is a separate API for 3D content: 3D `PointMark`, `RectangleMark`, `RuleMark`, and `SurfacePlot`. Always gate with `#available(iOS 26, *)`.

```swift
if #available(iOS 26, *) {
    Chart3D(points) { point in
        PointMark(x: .value("X", point.x), y: .value("Y", point.y), z: .value("Z", point.z))
    }
    .chart3DPose(.front)
    .chart3DCameraProjection(.perspective)
}
```

`SurfacePlot` visualizes a two-variable function:

```swift
if #available(iOS 26, *) {
    Chart3D {
        SurfacePlot(x: "x", y: "height", z: "z") { x, z in sin(x) * cos(z) }
    }
    .chartXScale(domain: -Double.pi ... Double.pi)
    .chartZScale(domain: -Double.pi ... Double.pi)
}
```

Camera/pose configuration: projection — `.chart3DCameraProjection(.orthographic)` (default, precise measurements) or `.perspective` (depth effect); pose presets — `.chart3DPose(.default)`, `.front`, `.back`, `.left`, `.right`; custom pose — `.chart3DPose(azimuth: .degrees(45), inclination: .degrees(30))`. On visionOS, `Chart3D` supports natural 3D rotation/exploration gestures.

### Axis tweaks

```swift
Chart(data) { item in
    BarMark(x: .value("Month", item.month), y: .value("Revenue", item.revenue))
}
.chartXAxis(.visible)      // .automatic, .visible, .hidden
.chartYAxis(.hidden)
.chartXAxisLabel("Month")
.chartYAxisLabel("Revenue")
```

Custom `AxisMarks` control tick placement, labels, and grid lines:

```swift
Chart(steps) { day in
    LineMark(x: .value("Day", day.date), y: .value("Steps", day.count))
}
.chartXAxis {
    AxisMarks(preset: .aligned, position: .bottom, values: .stride(by: .day)) {
        AxisGridLine()
        AxisTick(length: .label)
        AxisValueLabel(format: .dateTime.weekday(.abbreviated))
    }
}
```

`AxisMarks` inputs — `preset`: `.automatic`, `.extended`, `.aligned`, `.inset`; `position`: `.automatic`, `.leading`, `.trailing`, `.top`, `.bottom`; `values`: `.automatic`, `.automatic(desiredCount:)`, `.stride(by:)`, `.stride(by:count:)`, or an explicit array.

Components combine inside `AxisMarks`: `AxisGridLine()`, `AxisTick()`, `AxisValueLabel()`. Tune dense axes with `AxisValueLabel(collisionResolution: .greedy(minimumSpacing: 8), orientation: .vertical)`. Label orientations: `.automatic`, `.horizontal`, `.vertical`, `.verticalReversed`. Collision strategies: `.automatic`, `.greedy`, `.greedy(priority:minimumSpacing:)`, `.truncate`, `.disabled`.

Explicit axis domains and plot area tweaks via scales:

```swift
Chart(data) { item in
    LineMark(x: .value("Index", item.index), y: .value("Score", item.score))
}
.chartXScale(domain: 0...30)
.chartYScale(domain: 0...100)
.chartPlotStyle { plotArea in plotArea.background(.gray.opacity(0.08)) }
```

You can set one axis domain without forcing the other: `.chartXScale(domain: startDate...endDate)`.

Scrollable axes (iOS 17+) for larger datasets:

```swift
@State private var scrollX = 7

Chart(data) { item in
    BarMark(x: .value("Day", item.day), y: .value("Value", item.value))
}
.chartScrollableAxes(.horizontal)
.chartXVisibleDomain(length: 7)
.chartScrollPosition(x: $scrollX)
```

### Selection APIs

```swift
// Single-value selection
@State private var selectedDate: Date?
Chart(steps) { day in
    LineMark(x: .value("Day", day.date), y: .value("Steps", day.count))
    if let selectedDate {
        RuleMark(x: .value("Selected Day", selectedDate)).foregroundStyle(.secondary)
    }
}
.chartXSelection(value: $selectedDate)

// Range selection - bind to a ClosedRange matching the plotted axis value type
@State private var selectedWeeks: ClosedRange<Int>?
Chart(weeks) { week in
    BarMark(x: .value("Week", week.index), y: .value("Revenue", week.revenue))
}
.chartXSelection(range: $selectedWeeks)

// Angle selection - SectorMark only, no range overload
@State private var selectedAmount: Double?
Chart(expenses) { expense in
    SectorMark(angle: .value("Amount", expense.amount))
        .foregroundStyle(by: .value("Category", expense.category))
}
.chartAngleSelection(value: $selectedAmount)
```

Use `value:` bindings for a single selected point/axis value; `range:` bindings when users should brush a span (zoom windows, comparisons, grouped summaries). **Selection bindings return the plottable axis value, not the full data element** — map back to your model if you need the selected record.

### Annotations

```swift
BarMark(x: .value("Month", item.month), y: .value("Revenue", item.revenue))
    .annotation(position: .top) { Text(item.revenue.formatted()) }
```

Useful for selected values, thresholds, summaries, direct labeling. Common positions: `.overlay`, `.top`, `.bottom`, `.leading`, `.trailing`.

### `ChartProxy` and custom touch handling

Use `chartOverlay`/`chartBackground` (iOS 16+) or `chartGesture` (iOS 17+) with `ChartProxy` when built-in selection isn't enough:

```swift
.chartOverlay { proxy in
    GeometryReader { geometry in
        Rectangle().fill(.clear).contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { value in
                        guard let plotFrame = proxy.plotFrame else { return }   // iOS 16: proxy.plotAreaFrame
                        let frame = geometry[plotFrame]
                        let x = value.location.x - frame.origin.x
                        guard x >= 0, x <= frame.size.width else { return }
                        selectedDate = proxy.value(atX: x, as: Date.self)
                    }
                    .onEnded { _ in selectedDate = nil }
            )
    }
}
```

`ChartProxy` gives lower-level access: `value(atX:as:)`/`value(atY:as:)`/`value(at:as:)` convert gesture coordinates to chart values; `position(forX:)`/`position(forY:)`/`position(for:)` place custom overlays; `selectXValue(at:)`/`selectYValue(at:)`/`selectXRange(from:to:)`/`selectYRange(from:to:)` drive built-in selection from custom gestures (iOS 17+, along with `chartGesture`); `plotFrame` (iOS 17+) or `plotAreaFrame` (iOS 16) with `plotSize` convert between gesture coordinates and the plot area.

### Modifier scope

Apply chart-wide modifiers to the `Chart` container; mark-specific modifiers to the individual mark:

```swift
Chart(data) { item in
    LineMark(x: .value("Day", item.date), y: .value("Value", item.value))
        .interpolationMethod(.monotone)   // mark-level
}
.chartXAxis { AxisMarks() }               // chart-level
.chartYScale(domain: 0...100)             // chart-level
.chartPlotStyle { $0.background(.thinMaterial) }
```

### Styling and visual channels

```swift
// Categorical coloring - generates a legend automatically
Chart(sales) { item in
    BarMark(x: .value("Month", item.month), y: .value("Revenue", item.revenue))
        .foregroundStyle(by: .value("Region", item.region))
}
```

**Avoid** `.foregroundStyle(.red)` per mark for categorical data — it suppresses the automatic legend and breaks accessibility.

```swift
// Custom color scale
.chartForegroundStyleScale(["North": .blue, "South": .orange, "East": .green])

// Dynamic mapping when not every series appears at every point
.chartForegroundStyleScale(domain: regions, mapping: { region in colorForRegion(region) })

// Symbol/size channels for additional data dimensions on PointMark/LineMark
Chart(measurements) { item in
    PointMark(x: .value("Time", item.time), y: .value("Value", item.value))
        .foregroundStyle(by: .value("Category", item.category))
        .symbol(by: .value("Category", item.category))
        .symbolSize(by: .value("Weight", item.weight))
}

// Legend control
.chartLegend(.visible)
.chartLegend(.hidden)
.chartLegend(position: .bottom, alignment: .center)
```

### Composing multiple marks

```swift
// Line with points
LineMark(x: .value("Day", day.date), y: .value("Steps", day.count)).interpolationMethod(.monotone)
PointMark(x: .value("Day", day.date), y: .value("Steps", day.count))

// Bars with a threshold line
BarMark(x: .value("Month", item.month), y: .value("Revenue", item.revenue))
RuleMark(y: .value("Target", 10_000)).foregroundStyle(.red).lineStyle(StrokeStyle(dash: [5, 3]))
```

### Animating chart data

Marks animate automatically when data identity is stable and the change is wrapped in an animation:

```swift
withAnimation(.easeInOut) { chartData = updatedData }
```

Always use `Identifiable` models (or explicit `id:`) so Swift Charts can match old and new data points and animate the transition.

### Accessibility

Swift Charts provides built-in VoiceOver support: three rotor actions — **Describe Chart** (axes/series overview), **Audio Graph** (sonification, pitch represents value), and **Chart Detail** (interactive point-by-point exploration). For general accessibility guidance (labels, traits, Dynamic Type, contrast) see the `ios-accessibility` skill; this section covers only the chart-specific mechanics.

Always use clear, descriptive strings in `.value(_, _)` — they're read by VoiceOver and used in the Audio Graph:

```swift
// Good
LineMark(x: .value("Date", entry.date), y: .value("Daily Steps", entry.count))
// Bad - generic labels
LineMark(x: .value("X", entry.date), y: .value("Y", entry.count))
```

For a custom Audio Graph, conform the chart view to `AXChartDescriptorRepresentable` and implement `makeChartDescriptor()`, attached via `.accessibilityChartDescriptor(self)`:

```swift
struct StepsChart: View, AXChartDescriptorRepresentable {
    let steps: [DailySteps]

    var body: some View {
        Chart(steps) { day in
            LineMark(x: .value("Date", day.date), y: .value("Steps", day.count))
        }
        .accessibilityChartDescriptor(self)
    }

    func makeChartDescriptor() -> AXChartDescriptor {
        guard let first = steps.first, let last = steps.last else {
            return AXChartDescriptor(title: "Daily Step Count", summary: nil,
                xAxis: AXNumericDataAxisDescriptor(title: "Date", range: 0...1, gridlinePositions: []) { "\($0)" },
                yAxis: AXNumericDataAxisDescriptor(title: "Steps", range: 0...1, gridlinePositions: []) { "\($0)" },
                additionalAxes: [], series: [])
        }
        let xAxis = AXDateDataAxisDescriptor(title: "Date", range: first.date...last.date, gridlinePositions: [])
        let yAxis = AXNumericDataAxisDescriptor(
            title: "Steps", range: 0...Double(steps.map(\.count).max() ?? 0),
            gridlinePositions: []) { "\(Int($0)) steps" }
        let series = AXDataSeriesDescriptor(
            name: "Daily Steps", isContinuous: true,
            dataPoints: steps.map { .init(x: $0.date, y: Double($0.count)) })
        return AXChartDescriptor(title: "Daily Step Count", summary: nil,
            xAxis: xAxis, yAxis: yAxis, additionalAxes: [], series: [series])
    }
}
```

**Fallback strategy**: gate advanced chart APIs with `#available` and provide a fallback chart without the gated features. Because modifiers like `.chartXSelection` change the return type, you must duplicate the entire `Chart` — you cannot conditionally apply just the modifier. Version breakdown: iOS 16+ base `Chart`/axes/scales/core marks/`ChartProxy`/`chartOverlay`/`chartBackground`; iOS 17+ `SectorMark`/`chartXSelection`/`chartYSelection`/`chartAngleSelection`/`chartScrollableAxes`/visible-domain scrolling/`chartGesture`; iOS 18+ plot types/function plotting; iOS 26+ `Chart3D`/`SurfacePlot`/Z-axis/3D camera-pose APIs.

### Best practices

**Do**: use semantic `.value(_, _)` labels; prefer `Identifiable` models (or explicit `id:`) for stable identity; use `foregroundStyle(by:)` for categorical series (automatic legend + accessibility); use `RuleMark` for goals/thresholds/selected-value indicators; use explicit `AxisMarks(values:)` when automatic ticks get crowded; use `chartXScale`/`chartYScale` for stable visual comparisons; use range selection for brushed selection; gate iOS 17+/18+/26+ APIs with `#available`.

**Don't**: put chart-wide modifiers on individual marks; apply manual per-mark `.foregroundStyle(.color)` for categorical data; rely on unstable identities when data can be inserted/removed/reordered; use string values for naturally numeric/date axes unless categorical behavior is wanted; stack unrelated series just because the mark allows it; force every tick label to render when collision handling would read better; assume selection returns a model object (it returns only the plottable axis value); forget that range selection only applies to X/Y axes, not angle selection.

---

## macOS View, Window, and Scene Patterns

### Scenes

| API | Availability | macOS-only? | Behavior |
|-----|-------------|:-----------:|----------|
| `WindowGroup` | macOS 11.0+ | No | Multiple window instances, tabbed interface, automatic Window menu commands |
| `Window` | macOS 13.0+ | No | App quits when the sole window closes; adds itself to the Windows menu |
| `UtilityWindow` | macOS 15.0+ | Yes | Floating tool palette; receives `FocusedValues` from the active main window |
| `Settings` | macOS 11.0+ | Yes | Preferences window (Cmd+,) |
| `MenuBarExtra` | macOS 13.0+ | Yes | Persistent icon/menu in the system menu bar |
| `DocumentGroup` | macOS 11.0+ | No | Document-based File menu commands; multiple document windows |

**`Settings`** presents preferences (Cmd+, or the app menu); SwiftUI manages the menu item and window lifecycle automatically. Use `TabView` with `Tab` items for multi-pane preferences (typically a `Form` with `@AppStorage`-backed controls):

```swift
Settings {
    TabView {
        Tab("General", systemImage: "gear") { GeneralSettingsView() }
        Tab("Advanced", systemImage: "star") { AdvancedSettingsView() }
    }
    .scenePadding()
    .frame(maxWidth: 350, minHeight: 100)
}
```

`SettingsLink` (macOS 14+) opens Settings from in-app navigation; the `openSettings` environment action (macOS 14+) does it programmatically:

```swift
SettingsLink { Label("Preferences", systemImage: "gear") }

@Environment(\.openSettings) private var openSettings
Button("Open Settings") { openSettings() }
```

**`MenuBarExtra`** renders a persistent menu-bar control in two styles — `.menu` (default dropdown) or `.window` (popover panel with custom SwiftUI content):

```swift
MenuBarExtra("My Utility", systemImage: "hammer") {
    Button("Action One") { }
    Button("Action Two") { }
    Divider()
    Button("Quit") { NSApplication.shared.terminate(nil) }
}

MenuBarExtra("Status", systemImage: "chart.bar") {
    DashboardView().frame(width: 240)
}
.menuBarExtraStyle(.window)
```

Toggleable extra: `isInserted:` bound to `@AppStorage`. Menu-bar-only app: `MenuBarExtra` as the sole scene plus `LSUIElement = true` in Info.plist hides the Dock icon (the app auto-terminates if the user removes the extra).

**`WindowGroup`** supports multiple window instances, a tabbed interface, and an automatic Window menu on macOS. It keeps the app running after all windows close — unlike `Window` (a single, unique window), which quits the app if it is the sole scene and gets closed. Prefer `WindowGroup` for the primary scene; use `Window` for supplementary singletons.

```swift
@main
struct Mail: App {
    var body: some Scene {
        WindowGroup { MailViewer() }
        WindowGroup("Message", for: Message.ID.self) { $messageID in MessageDetail(messageID: messageID) }
        Window("Connection Doctor", id: "connection-doctor") { ConnectionDoctor() }
    }
}

@Environment(\.openWindow) private var openWindow
Button("Open Message") { openWindow(value: message.id) }
Button("Connection Doctor") { openWindow(id: "connection-doctor") }   // brings existing window to front
```

**`UtilityWindow`** (macOS 15+) is a floating tool palette/inspector window. It receives `FocusedValues` from the focused main scene, floats at `.floating` level by default, hides when the app is inactive, only focuses when explicitly needed (e.g. clicking the title bar), dismisses with Escape, is not minimizable by default, and auto-adds a show/hide item to the View menu (removable with `.commandsRemoved()`).

```swift
UtilityWindow("Photo Info", id: "photo-info") { PhotoInfoViewer() }

struct PhotoInfoViewer: View {
    @FocusedValue(PhotoSelection.self) private var selectedPhotos
    var body: some View {
        if let photos = selectedPhotos { Text("\(photos.count) photos selected") }
        else { Text("No selection").foregroundStyle(.secondary) }
    }
}
```

**`DocumentGroup`** provides document-based menu commands (File > New/Open/Save/Revert) and multiple document windows on macOS (a document browser on iOS). The document type conforms to `FileDocument` (value type) or `ReferenceFileDocument` (reference type):

```swift
DocumentGroup(newDocument: TextFile()) { config in ContentView(document: config.$document) }

struct TextFile: FileDocument {
    static var readableContentTypes: [UTType] { [.plainText] }
    var text: String = ""
    init() {}
    init(configuration: ReadConfiguration) throws {
        text = String(data: configuration.file.regularFileContents ?? Data(), encoding: .utf8) ?? ""
    }
    func fileWrapper(configuration: WriteConfiguration) throws -> FileWrapper {
        FileWrapper(regularFileWithContents: Data(text.utf8))
    }
}
```

Use `DocumentGroup(viewing:)` for read-only formats; add additional `DocumentGroup` scenes for multiple document types.

Always wrap macOS-only scenes in `#if os(macOS)` in multiplatform projects:

```swift
#if os(macOS)
Settings { SettingsView() }
MenuBarExtra("Status", systemImage: "bolt") { StatusMenu() }
#endif
```

**Best practices**: prefer `Settings` over a custom preferences window; prefer `MenuBarExtra` over managing `NSStatusItem` directly; use `WindowGroup` as the primary scene, `Window` for supplementary singletons; use `UtilityWindow` for inspectors/palettes (handles floating/focus/visibility automatically); use `DocumentGroup` for document-based apps; use `openWindow(id:)` to bring an existing window to front.

### Views

**`HSplitView`/`VSplitView`** give resizable, user-draggable-divider layouts for IDE-style panes where all panels are equal peers — use `NavigationSplitView` instead for sidebar-driven navigation:

```swift
HSplitView {
    FileTreeView().frame(minWidth: 200)
    CodeEditorView().frame(minWidth: 400)
    PreviewPane().frame(minWidth: 200)
}
```

**`Table`** macOS-specific styling (for basics — creation, selection, sorting, adaptive compact layout — see the sibling view-structure/list-patterns guidance):

```swift
Table(people) { /* columns */ }.tableStyle(.bordered)                                  // visible grid lines
Table(people) { /* columns */ }.tableStyle(.bordered(alternatesRowBackgrounds: true))
Table(people) { /* columns */ }.tableStyle(.inset)                                     // no borders
Table(people) { /* columns */ }.tableColumnHeaders(.hidden)
```

**`PasteButton`** reads clipboard content via `Transferable`; unlike iOS, it does **not** auto-validate pasteboard changes on macOS. **`CopyButton`** (macOS 15+) copies `Transferable` content to the clipboard:

```swift
PasteButton(payloadType: String.self) { strings in pastedText = strings[0] }
CopyButton(item: shareableText)
```

**File operations**: `fileImporter`/`fileExporter` present native `NSOpenPanel`/`NSSavePanel` (column/list/gallery view, sidebar favorites, tags, QuickLook). `fileDialogMessage(_:)`, `fileDialogConfirmationLabel(_:)`, and `fileExporterFilenameLabel(_:)` (macOS 13+) customize dialog text.

```swift
.fileImporter(isPresented: $showImporter, allowedContentTypes: [.pdf], allowsMultipleSelection: false) { result in
    if case .success(let urls) = result, let url = urls.first {
        guard url.startAccessingSecurityScopedResource() else { return }
        defer { url.stopAccessingSecurityScopedResource() }
        // use url
    }
}
```

**Important**: always call `startAccessingSecurityScopedResource()` on returned URLs and `stopAccessingSecurityScopedResource()` when done — these are security-scoped bookmarks; access fails without it.

**Drag, drop, pasteboard**: on macOS, drag and drop works **across applications** (e.g. dragging to Finder or Mail). Prefer the modern `Transferable`-based approach over the legacy `NSItemProvider` one:

```swift
// Modern (Transferable)
Text(item.title).draggable(item)
.dropDestination(for: MyItem.self) { items, location in
    droppedItems.append(contentsOf: items)
    return true
}

// Legacy (NSItemProvider)
Image(systemName: "doc").onDrag { NSItemProvider(object: fileURL as NSURL) }
Text("Drop files here").onDrop(of: [.fileURL], isTargeted: nil) { providers in /* ... */ return true }
```

**AppKit interop**: `NSViewRepresentable`/`NSViewControllerRepresentable` wrap AppKit views/controllers in SwiftUI (implement `make…`/`update…`); `NSHostingController`/`NSHostingView` host SwiftUI inside AppKit in the reverse direction. Use a `Coordinator` to forward delegate/target-action callbacks:

```swift
struct SearchField: NSViewRepresentable {
    @Binding var text: String
    func makeNSView(context: Context) -> NSSearchField {
        let field = NSSearchField()
        field.delegate = context.coordinator
        return field
    }
    func updateNSView(_ nsView: NSSearchField, context: Context) { nsView.stringValue = text }
    func makeCoordinator() -> Coordinator { Coordinator(text: $text) }

    class Coordinator: NSObject, NSSearchFieldDelegate {
        var text: Binding<String>
        init(text: Binding<String>) { self.text = text }
        func controlTextDidChange(_ obj: Notification) {
            if let field = obj.object as? NSSearchField { text.wrappedValue = field.stringValue }
        }
    }
}
```

**Warning**: never set `frame`/`bounds` directly on the managed `NSView`/`NSViewController` — SwiftUI owns layout.

```swift
// Host SwiftUI as a view controller / NSView, inside an AppKit app
let hostingController = NSHostingController(rootView: MySwiftUIView())
window.contentViewController = hostingController
let hostingView = NSHostingView(rootView: MySwiftUIView())
someNSView.addSubview(hostingView)
```

**Best practices**: prefer `NavigationSplitView` for sidebar-driven navigation, reserve `HSplitView`/`VSplitView` for IDE-style equal panes; make `Table` adaptive for compact size classes; always call `startAccessingSecurityScopedResource()`; prefer `Transferable` for drag & drop; use `NSViewRepresentable` + `Coordinator` for delegate callbacks; never set `frame`/`bounds` on AppKit-managed views; prefer native SwiftUI over AppKit interop unless SwiftUI lacks the feature.

### Window and toolbar styling

**Toolbar style** (`windowToolbarStyle`, macOS-only, applied to a scene): `.automatic` (system default), `.unified` (title bar + toolbar in one row), `.unifiedCompact` (reduced height), `.expanded` (title bar above toolbar, more toolbar space); `.unified(showsTitle: false)` hides the title.

```swift
WindowGroup { ContentView() }.windowToolbarStyle(.unified)
```

```swift
NavigationSplitView { SidebarView() } detail: { DetailView() }
    .toolbar { ToolbarItem(placement: .automatic) { Button(action: addItem) { Label("Add", systemImage: "plus") } } }
    .searchable(text: $searchText, placement: .sidebar)
```

**Window style**: `.titleBar` (default) or `.hiddenTitleBar` for chromeless windows (media players, custom-chrome apps, immersive experiences).

```swift
WindowGroup { ContentView() }.windowStyle(.hiddenTitleBar)
```

**Window sizing**: `windowResizability`, `defaultSize`, `defaultPosition` configure sizing/placement together.

```swift
WindowGroup {
    ContentView().frame(minWidth: 600, minHeight: 400)
}
.defaultSize(width: 900, height: 600)
.defaultPosition(.center)
.windowResizability(.contentMinSize)
```

`windowResizability`: `.automatic` (system decides), `.contentSize` (fixed to content, zoom button disabled), `.contentMinSize` (resizable, minimum from content's `minWidth`/`minHeight`). `defaultPosition`: `.center`, `.topLeading`, `.top`, `.topTrailing`, `.leading`, `.trailing`, `.bottomLeading`, `.bottom`, `.bottomTrailing`. Set `minWidth`/`minHeight` on content and enforce with `.contentMinSize`; use `.defaultSize()` for initial dimensions larger than the minimums.

`windowIdealPlacement` (macOS 15+) gives precise programmatic positioning via display geometry:

```swift
.windowIdealPlacement { context in
    let screen = context.defaultDisplay.visibleArea
    return WindowPlacement(x: screen.midX, y: screen.midY, width: screen.width / 2, height: screen.height)
}
```

**`NavigationSplitView`** on macOS shows columns side-by-side (never overlaid), with a translucent sidebar background and user-resizable column widths:

```swift
NavigationSplitView {
    List(items, selection: $selectedId) { item in Text(item.name) }
        .navigationSplitViewColumnWidth(min: 180, ideal: 220, max: 300)
} detail: {
    DetailView(id: selectedId)
}
.navigationSplitViewStyle(.balanced)
```

Use the three-column variant (sidebar/content/detail) for master-detail-detail layouts.

**`Inspector`** (macOS 14+) is a trailing-edge panel resizable by dragging its edge:

```swift
MainContent()
    .inspector(isPresented: $showInspector) {
        InspectorView().inspectorColumnWidth(min: 200, ideal: 250, max: 400)
    }
    .toolbar {
        ToolbarItem { Button { showInspector.toggle() } label: { Label("Inspector", systemImage: "info.circle") } }
    }
```

**Commands & keyboard**: `Commands`/`CommandGroup`/`CommandMenu` populate the menu bar directly on macOS (key commands on iOS). `CommandGroup` placement: `.replacing(_:)` replaces a system group, `.before(_:)`/`.after(_:)` insert adjacent to it (common placements: `.newItem`, `.saveItem`, `.help`, `.toolbar`, `.sidebar`). On macOS, `KeyboardShortcut` shows alongside menu items and in button tooltips on hover.

```swift
.commands {
    CommandMenu("Tools") {
        Button("Run Analysis") { }.keyboardShortcut("r", modifiers: [.command, .shift])
    }
    CommandGroup(after: .newItem) { Button("New From Template...") { } }
}

Button("Save") { save() }.keyboardShortcut("s", modifiers: .command)
Button("Delete") { delete() }.keyboardShortcut(.delete, modifiers: .command)
```

**Best practices**: use `.unified`/`.unifiedCompact` for most apps, `.expanded` only for toolbar-heavy apps; set min frame sizes on content and enforce with `.contentMinSize`; always provide `defaultSize` for new windows; use `NavigationSplitView` for sidebar navigation, not `HSplitView`; use `Inspector` for supplementary panels; define `Commands` for repeatable actions (macOS users expect keyboard shortcuts); wrap macOS-only window configuration in `#if os(macOS)` for multiplatform projects.

---

## Instruments Trace Interpretation (requires your own trace-capture tooling)

The guidance below is for manually reading an Xcode Instruments `.trace` recording in Instruments.app (or any tool that surfaces the same lanes: Time Profiler, Hangs, Animation Hitches, SwiftUI updates, and the SwiftUI cause graph). It is diagnostic knowledge, not a scripted pipeline — apply it with whatever trace-capture/inspection tooling you have available.

### The key diagnostic: main-thread coverage during a hang or hitch

Time Profiler samples the main thread roughly every 1ms. For a blocked-frame or hang window of `N` ms, you'd expect about `N` main-thread *running* samples if the main thread were fully CPU-bound the whole time. **Main-thread coverage** is the ratio of observed running samples to that expectation:

- **< 25% coverage** → the main thread was **blocked** (I/O, a lock, synchronous XPC, `Task.sleep`, waiting on an actor-isolated call). The hot symbols you do see are the moments main *was* executing — look there for the code that *initiates* the blocking work, not the work itself. Common fix: move the work to a background executor, mark it `nonisolated`, or dispatch via `Task.detached`.
- **≥ 75% coverage** → the main thread was **CPU-bound** the whole window. Hot symbols point directly at the expensive work. Common fixes: hoist computation out of view bodies, cache derived values, avoid per-frame allocation, debounce `onChange`.
- **25–75% coverage** → a mix, usually computation plus intermittent I/O; report both the hot symbols and the partial blocking.

When reading hot symbols, treat frames in your own module (or Swift free-function form) as the fix candidates. System frames (`swift_`, `dyld`, `objc_`, `CA*`, `CF*`, `NS*`, `__open`, `pthread*`) tell you *what* the code was doing, but the user-code caller one frame up is typically what to fix — e.g. `__open` under a hot symbol usually means a `FileHandle`/`Data(contentsOf:)`/`JSONDecoder.decode(from:)` call site is worth searching for.

### The SwiftUI cause graph: why an update keeps firing

The SwiftUI updates lane tells you *what* is expensive; the SwiftUI cause graph tells you *why* it keeps being triggered. Each edge in the graph is "source node propagated to destination node" in SwiftUI's attribute graph — looking at a hot destination's top incoming sources tells you what keeps invalidating it. Watch for these source signatures:

- **An `@AppStorage`/`UserDefaults` observer closure** with thousands of outgoing edges to multiple `@AppStorage <Type>.<prop>` destinations — a settings write is fanning out to every reader (a feedback storm). Fix by reading each key once at a high level and passing values down, or wrapping settings in a single `@Observable` so only genuine readers invalidate.
- **An environment-writing node** with thousands of edges — a modifier (often a custom environment key) is applied too broadly and is being re-installed on every layout pass.
- **"View Creation / Reuse" as the dominant source** — the hierarchy is replacing children rather than mutating them in place. Look for unstable identity: missing/unstable `.id(…)` on `ForEach`, type-erased `AnyView` wrappers, or conditional structure swaps (see [Hot-Path Optimization](#hot-path-optimization--update-control) and the `Identity and Transitions` note above).

When a specific view keeps showing up as expensive, inspect its ranked list of incoming sources in the cause graph directly — that ranked list is the "why", independent of which specific tool produced it.

### Scoping analysis to a window

A full trace is noisy. Narrow the analysis to the relevant slice by anchoring on something identifiable in the recording — an `os_log` message timestamp, a signpost interval's start/end, or a specific hang/hitch's time range — and restrict every lane (Time Profiler, hangs, hitches, SwiftUI updates, cause graph) to just that window before drawing conclusions. This is the general shape of the technique regardless of which trace-inspection tool performs the windowing: find the anchor event, take its timestamp(s), and filter every other lane to that range so correlations are precise rather than trace-wide.

### High-severity SwiftUI events → likely cause

| Description | Likely cause |
|---|---|
| `onChange` | Expensive `.onChange` body |
| `Gesture` | Heavy gesture handler |
| `Action Callback` | Button/tap handler doing real work |
| `Update` | View body recomputation |
| `Creation` | View init cost |
| `Layout` | `GeometryReader` churn |

Route each to the matching guidance above (`onChange`/`Gesture`/`Action Callback`/`Update` → [Hot-Path Optimization](#hot-path-optimization--update-control); `Creation`/unstable identity → the POD-view and view-extraction guidance in that same section).

### Composing a prioritized fix plan

Order findings from most to least actionable, and cite the concrete evidence for each:

1. **Hangs with < 25% main-thread coverage** — blocking-I/O smells, nearly always fixable by moving work off-main.
2. **Hangs with ≥ 75% coverage** — CPU-bound main-thread work; fix the top hot symbols.
3. **Cause-graph sources with very high edge counts** (thousands) — structural invalidation bugs (feedback storms, over-applied modifiers). Often cheaper to fix than per-view optimizations, and they collapse many downstream high-severity updates at once.
4. **Hitches attributed to the app** (as opposed to a system hitch) with an overlapping SwiftUI update — specific views to restructure.
5. **High-severity `onChange`/`Gesture`/`Action Callback` events** taking longer than roughly one frame (~16ms) — frame-dropping handlers; for repeat offenders, look up their cause-graph sources.
6. **Heaviest views by total body time**, even without triggering a hitch — candidates for view extraction, `Equatable`, or POD-wrapper treatment.

When reporting back: give a one-line summary (counts of hangs/hitches/high-severity updates and the worst durations), then one paragraph per prioritized finding citing the actual evidence (coverage %, hot symbol name, cause-graph edge count, overlapping view name), then a numbered, file-specific fix plan — cite line numbers when you have them, and don't edit code unless asked to.
