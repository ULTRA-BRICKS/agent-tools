# Dynamic Type — Complete Reference

Dynamic Type allows users to choose their preferred text size. Over 25% of iOS users change this setting. Supporting it is essential for accessibility and required for WCAG 1.4.4.

## Table of Contents

1. [SwiftUI Text Styles](#swiftui-text-styles)
2. [Custom Fonts in SwiftUI](#custom-fonts-in-swiftui)
3. [UIFontMetrics for Custom Fonts (UIKit)](#uifontmetrics-for-custom-fonts-uikit)
4. [@ScaledMetric for Non-Text Dimensions](#scaledmetric-for-non-text-dimensions)
5. [Layout Adaptation at Accessibility Sizes](#layout-adaptation-at-accessibility-sizes)
6. [ScrollView for Large Text](#scrollview-for-large-text)
7. [Large Content Viewer](#large-content-viewer)
8. [Size Category Thresholds](#size-category-thresholds)
9. [Restricting Range](#restricting-range)

## SwiftUI Text Styles

Always use text styles instead of hardcoded sizes:

```swift
// ❌ Hardcoded — never scales
Text("Welcome").font(.system(size: 24))

// ✅ Dynamic Type — scales with user preference
Text("Welcome").font(.title)
```

**Style mapping:**
| Text Style | Default Size | Weight | Typical Use |
|---|---|---|---|
| `.largeTitle` | 34pt | Regular | Screen titles |
| `.title` | 28pt | Regular | Major section headings |
| `.title2` | 22pt | Regular | Sub-section headings |
| `.title3` | 20pt | Regular | Minor headings |
| `.headline` | 17pt | **Semibold** | Emphasized body text |
| `.body` | 17pt | Regular | Main content |
| `.callout` | 16pt | Regular | Secondary content |
| `.subheadline` | 15pt | Regular | Captions, metadata |
| `.footnote` | 13pt | Regular | Footnotes, fine print |
| `.caption` | 12pt | Regular | Labels, timestamps |
| `.caption2` | 11pt | Regular | Smallest readable text |

## Custom Fonts in SwiftUI

```swift
// ✅ Auto-scales with Dynamic Type
Text("Hello").font(.custom("Georgia", size: 24, relativeTo: .headline))

// ❌ Fixed size — no Dynamic Type
Text("Fixed").font(.custom("Georgia", fixedSize: 24))
```

The `relativeTo:` parameter determines scaling behavior — `.largeTitle` scales less aggressively than `.body`.

## UIFontMetrics for Custom Fonts (UIKit)

```swift
// ❌ No scaling
label.font = UIFont(name: "Avenir-Medium", size: 17)

// ✅ Scales with Dynamic Type
let customFont = UIFont(name: "Avenir-Medium", size: 17)!
label.font = UIFontMetrics(forTextStyle: .body).scaledFont(for: customFont)
label.adjustsFontForContentSizeCategory = true  // CRITICAL: enables live updating
```

**Critical:** `adjustsFontForContentSizeCategory` only works on `UILabel`, `UITextField`, and `UITextView` — not arbitrary custom views.

Also always set:
```swift
label.numberOfLines = 0  // Allow text wrapping
```

## @ScaledMetric for Non-Text Dimensions

```swift
// ❌ Fixed image size — doesn't scale
Image("logo").resizable().frame(width: 40, height: 40)

// ✅ Scales proportionally with Dynamic Type
@ScaledMetric(relativeTo: .body) private var imageSize: CGFloat = 40
Image("logo").resizable()
    .aspectRatio(contentMode: .fit)
    .frame(width: imageSize, height: imageSize)
```

**Gotchas:**
- `relativeTo:` matters — `.largeTitle` scales less aggressively than `.body`
- SF Symbols scale automatically with `.font()` — do NOT use `.resizable()` on SF Symbols
- Decorative images should NOT scale — consider hiding at accessibility sizes

## Layout Adaptation at Accessibility Sizes

When text gets very large, horizontal layouts break. Switch to vertical at accessibility sizes.

### ViewThatFits (iOS 16+) — Automatic
```swift
ViewThatFits {
    HStack { content }     // Preferred
    VStack { content }     // Falls back when HStack overflows
}
```

### AnyLayout (iOS 16+) — Preserves state
```swift
@Environment(\.dynamicTypeSize) var dynamicTypeSize
let layout = dynamicTypeSize.isAccessibilitySize
    ? AnyLayout(VStackLayout()) : AnyLayout(HStackLayout())
layout { content }
```

### Manual threshold check
```swift
@Environment(\.dynamicTypeSize) var dynamicTypeSize
if dynamicTypeSize.isAccessibilitySize {
    VStack { content }
} else {
    HStack { content }
}
```

### UIKit
```swift
override func traitCollectionDidChange(_ prev: UITraitCollection?) {
    super.traitCollectionDidChange(prev)
    stackView.axis = traitCollection.preferredContentSizeCategory.isAccessibilityCategory
        ? .vertical : .horizontal
}
```

## ScrollView for Large Text

At accessibility sizes, content often overflows the screen. Always ensure scrollability:

```swift
// ❌ Content clips at large sizes
VStack { /* long content */ }

// ✅ Always scrollable
ScrollView { VStack { /* long content */ } }

// ✅ Conditional scroll (iOS 16+) — only scrolls when needed
ViewThatFits(in: .vertical) {
    content                    // If it fits, no scroll
    ScrollView { content }     // Otherwise, scroll
}
```

## Large Content Viewer

For controls that can't scale (toolbars, tab bars), the Large Content Viewer shows a magnified version on long press:

```swift
// SwiftUI
LocationButton()
    .dynamicTypeSize(...DynamicTypeSize.xxxLarge)  // Cap normal scaling
    .accessibilityShowsLargeContentViewer {
        Label("Recenter", systemImage: "location")
    }

// UIKit
button.showsLargeContentViewer = true
button.largeContentTitle = "Home"
button.largeContentImage = UIImage(systemName: "house")
let interaction = UILargeContentViewerInteraction()
customTabBar.addInteraction(interaction)
```

## Size Category Thresholds

**12 categories (smallest to largest):**
1. `extraSmall`
2. `small`
3. `medium`
4. **`large`** (system default)
5. `extraLarge`
6. `extraExtraLarge`
7. `extraExtraExtraLarge`
8. `accessibilityMedium` ← `.isAccessibilityCategory` = true from here
9. `accessibilityLarge`
10. `accessibilityExtraLarge`
11. `accessibilityExtraExtraLarge`
12. `accessibilityExtraExtraExtraLarge`

**Key property:** `isAccessibilityCategory` (UIKit) / `isAccessibilitySize` (SwiftUI) — returns true for categories 8-12.

## Restricting Range

Use sparingly — Apple says "Please do not use this API to unduly limit text size."

```swift
// SwiftUI — cap at xxxLarge (below accessibility sizes)
Text("Capped").dynamicTypeSize(...DynamicTypeSize.xxxLarge)

// UIKit (iOS 15+)
view.maximumContentSizeCategory = .accessibilityExtraLarge
```

Only cap when truly necessary (e.g., compact toolbar items that use Large Content Viewer instead).
