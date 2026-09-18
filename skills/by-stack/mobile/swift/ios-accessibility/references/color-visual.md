# Color & Visual Accessibility — Complete Reference

Covers WCAG contrast ratios, color blindness support, dark mode, reduce transparency, smart invert, and increase contrast.

## Table of Contents

1. [WCAG Contrast Ratios](#wcag-contrast-ratios)
2. [Programmatic Contrast Calculation](#programmatic-contrast-calculation)
3. [Color Blindness](#color-blindness)
4. [Differentiate Without Color](#differentiate-without-color)
5. [Increased Contrast](#increased-contrast)
6. [Reduce Transparency](#reduce-transparency)
7. [Smart Invert Colors](#smart-invert-colors)
8. [Dark Mode](#dark-mode)

## WCAG Contrast Ratios

| Level | Normal Text | Large Text (18pt+ or 14pt+ bold) | UI Components |
|---|---|---|---|
| **AA** (required) | **4.5:1** | **3:1** | **3:1** |
| AAA (enhanced) | 7:1 | 4.5:1 | — |

**Exemptions:** Placeholder text, disabled controls, decorative text, logos.

**Large text definition:** 18pt (24px) regular OR 14pt (18.67px) bold.

## Programmatic Contrast Calculation

```swift
extension UIColor {
    var relativeLuminance: CGFloat {
        var r: CGFloat = 0, g: CGFloat = 0, b: CGFloat = 0
        getRed(&r, green: &g, blue: &b, alpha: nil)
        func linearize(_ c: CGFloat) -> CGFloat {
            c <= 0.03928 ? c / 12.92 : pow((c + 0.055) / 1.055, 2.4)
        }
        return 0.2126 * linearize(r) + 0.7152 * linearize(g) + 0.0722 * linearize(b)
    }

    func contrastRatio(with color: UIColor) -> CGFloat {
        let l1 = max(relativeLuminance, color.relativeLuminance)
        let l2 = min(relativeLuminance, color.relativeLuminance)
        return (l1 + 0.05) / (l2 + 0.05)
    }

    func meetsWCAG_AA(against bg: UIColor, isLargeText: Bool = false) -> Bool {
        contrastRatio(with: bg) >= (isLargeText ? 3.0 : 4.5)
    }
}
```

## Color Blindness

Never use color as the sole indicator of state:

```swift
// ❌ Status by color only — invisible to colorblind users
Circle().fill(isOnline ? .green : .red)

// ✅ Color + shape + text
HStack {
    Image(systemName: isOnline ? "checkmark.circle.fill" : "xmark.circle.fill")
    Text(isOnline ? "Online" : "Offline")
}
```

~8% of men and ~0.5% of women have some form of color vision deficiency. Red-green color blindness is most common.

## Differentiate Without Color

```swift
@Environment(\.accessibilityDifferentiateWithoutColor) var diffWithoutColor

// When enabled, add shapes/icons alongside color indicators
if diffWithoutColor {
    HStack {
        Image(systemName: "checkmark").foregroundStyle(.green)
        Text("Success")
    }
} else {
    Circle().fill(.green)
}
```

## Increased Contrast

```swift
// SwiftUI
@Environment(\.colorSchemeContrast) var contrast
// contrast == .increased → use 7:1 ratios, darker borders

// UIKit
if traitCollection.accessibilityContrast == .high {
    // Use higher contrast colors
}
```

**Asset catalog:** Check "High Contrast" to provide 4 variants:
- Any Appearance
- Dark Appearance
- Any High Contrast
- Dark High Contrast

System colors auto-adapt to increased contrast mode.

## Reduce Transparency

```swift
@Environment(\.accessibilityReduceTransparency) var reduceTransparency

.background(
    reduceTransparency
        ? AnyShapeStyle(Color(.systemBackground))
        : AnyShapeStyle(.ultraThinMaterial)
)
```

When reduce transparency is enabled, replace:
- `.ultraThinMaterial` → solid `Color(.systemBackground)`
- `.blur()` → solid background
- Any translucent overlay → opaque equivalent

## Smart Invert Colors

Prevents inversion on real-world content (photos, videos, maps):

```swift
// UIKit
imageView.accessibilityIgnoresInvertColors = true

// SwiftUI
Image("photo").accessibilityIgnoresInvertColors(true)
```

**Set on:**
- All photos and real-world images
- Video players
- Maps
- User-generated content

Setting on a parent cascades to all subviews. Use semantic system colors (`.label`, `.systemBackground`) which adapt correctly to inversion.

## Dark Mode

- Test contrast independently for BOTH light AND dark modes
- Use semantic colors that auto-adapt: `UIColor.label`, `.secondaryLabel`, `.systemBackground`
- Provide separate asset catalog appearances for Any/Dark + High Contrast
- Never hardcode `.black` for text or `.white` for backgrounds

**Semantic color reference:**
| Purpose | UIKit | SwiftUI |
|---|---|---|
| Primary text | `.label` | `.primary` |
| Secondary text | `.secondaryLabel` | `.secondary` |
| Primary background | `.systemBackground` | `Color(.systemBackground)` |
| Secondary background | `.secondarySystemBackground` | `Color(.secondarySystemBackground)` |
| Grouped background | `.systemGroupedBackground` | `Color(.systemGroupedBackground)` |
| Separator | `.separator` | `Color(.separator)` |
| Tint/accent | `.tintColor` | `.accentColor` / `.tint` |
