# RTL Layout — Complete Reference

## Core Rule: Leading/Trailing, Never Left/Right

```swift
// ❌ Never flips for Arabic/Hebrew
NSLayoutConstraint.activate([
    label.leftAnchor.constraint(equalTo: view.leftAnchor, constant: 16),
    label.rightAnchor.constraint(equalTo: view.rightAnchor, constant: -16)
])

// ✅ Flips automatically for RTL
NSLayoutConstraint.activate([
    label.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 16),
    label.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -16)
])
```

## Text Alignment

```swift
// ❌ Always left — broken for RTL
label.textAlignment = .left

// ✅ Auto-adapts to language direction
label.textAlignment = .natural

// SwiftUI — .leading is already direction-aware
Text("Hello").multilineTextAlignment(.leading)
```

## Exceptions: Force LTR

Some content is inherently left-to-right regardless of language:
- Phone numbers
- Media playback controls (play/pause/skip)
- Progress bars
- Numeric sequences
- Mathematical expressions
- Code snippets

```swift
// UIKit
phoneLabel.semanticContentAttribute = .forceLeftToRight
playbackView.semanticContentAttribute = .forceLeftToRight

// SwiftUI
PhoneNumberView()
    .environment(\.layoutDirection, .leftToRight)
```

## Images and Icons

Directional images (arrows, chevrons indicating navigation) should flip in RTL:

```swift
// UIKit — auto-flips for RTL
let image = UIImage(named: "arrow")?.imageFlippedForRightToLeftLayoutDirection()
imageView.image = image

// Or set on the image view
imageView.semanticContentAttribute = .unspecified  // Inherits from parent (flips in RTL)
```

Non-directional images (logos, photos, icons without directional meaning) should NOT flip:
```swift
imageView.semanticContentAttribute = .forceLeftToRight
```

## SwiftUI RTL

SwiftUI handles most RTL automatically:
- `HStack` content order reverses
- `.padding(.leading)` flips to trailing edge
- `Text` alignment follows language direction

Manual control:
```swift
// Force entire view to specific direction
MyView().environment(\.layoutDirection, .rightToLeft)

// Check current direction
@Environment(\.layoutDirection) var layoutDirection
```

## Testing RTL

Xcode → Edit Scheme → Options → Application Language → **Right to Left Pseudolanguage**

This flips the entire UI without needing Arabic translations, making it easy to spot left/right constraint issues.

## Common RTL Pitfalls

1. **UIEdgeInsets** — `left`/`right` don't flip. Use `NSDirectionalEdgeInsets` with `leading`/`trailing`.
2. **UICollectionViewFlowLayout** — Set `sectionInsetReference = .fromContentInset` and use directional insets.
3. **Custom drawing** — `CGRect` operations use absolute coordinates. Check `effectiveUserInterfaceLayoutDirection` and mirror manually.
4. **Tab bar** — Tab order reverses in RTL. Ensure icons and text make sense in both directions.
5. **Navigation** — Back button arrow direction is handled automatically, but custom back buttons need `imageFlippedForRightToLeftLayoutDirection()`.
