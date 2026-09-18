# iOS Accessibility — Priority Rules & Quick Reference

## Priority Rules (Always Apply)

1. **Button over onTapGesture** — Every interactive element must use `Button`, `Toggle`, `Link`, or another standard control. `onTapGesture` produces elements invisible to all assistive technology.
2. **Dynamic Type over hardcoded sizes** — Use `.font(.title)`, `.font(.body)`, etc. Never `.font(.system(size: 24))`. Over 25% of iOS users change their text size.
3. **Semantic colors over hardcoded colors** — `.foregroundStyle(.primary)` not `.foregroundColor(.black)`. `Color(.systemBackground)` not `Color.white`.
4. **Labels describe purpose, not appearance** — "Add to favorites" not "Heart icon". "Warning" not "Red triangle".
5. **Never include element type in label** — "Play" not "Play button". VoiceOver adds the role automatically from traits.
6. **Insert traits, never assign (UIKit)** — `.accessibilityTraits.insert(.selected)` not `.accessibilityTraits = .selected`.
7. **Disabled = dimmed, not hidden** — Use `.disabled(true)` or `.notEnabled` trait, never `isAccessibilityElement = false` on disabled controls.
8. **Group related content** — Use `.accessibilityElement(children: .combine)` for cards, cells, and related info clusters.
9. **Section headings are headers** — `.accessibilityAddTraits(.isHeader)` on every section heading. This enables rotor heading navigation.
10. **Respect system preferences** — Check `reduceMotion`, `dynamicTypeSize`, `colorSchemeContrast`, `reduceTransparency`, `differentiateWithoutColor`.

## Do's

- DO use `Image(decorative:)` for decorative images
- DO add `accessibilityLabel` to every `Image(systemName:)` button
- DO use `accessibilityCustomActions` for list cells with multiple actions
- DO use `accessibilityAdjustableAction` for custom steppers/sliders
- DO post `AccessibilityNotification.ScreenChanged` when presenting new screens programmatically
- DO post `AccessibilityNotification.Announcement` for async status updates
- DO test with Screen Curtain (triple three-finger tap) — the gold standard
- DO set `accessibilityIgnoresInvertColors = true` on photos/videos/maps
- DO use `numberOfLines = 0` on ALL UIKit labels (allows text wrapping for Dynamic Type)
- DO set `adjustsFontForContentSizeCategory = true` on UIKit labels/textfields/textviews

## Don'ts

- DON'T use `onTapGesture` for interactive elements
- DON'T use `.font(.system(size:))` — always use text styles
- DON'T use `.black`, `.white`, or hex colors for text/backgrounds
- DON'T use `.foregroundColor()` — it's deprecated; use `.foregroundStyle()`
- DON'T use `NavigationView` — it's deprecated; use `NavigationStack`
- DON'T use `.cornerRadius()` — it's deprecated; use `.clipShape(.rect(cornerRadius:))`
- DON'T put `accessibilityIdentifier` where `accessibilityLabel` is needed (identifiers are for UI tests only, not read by VoiceOver)
- DON'T describe appearance in labels ("Red warning triangle" → "Warning")
- DON'T add redundant hints ("Tap to delete" on a delete button — the action is obvious)
- DON'T use `GeometryReader` unless truly necessary — it breaks Dynamic Type and flexible layout
- DON'T use fixed `frame(width:height:)` — it prevents text from growing
- DON'T use `.resizable()` on SF Symbols — use `.font()` which scales automatically
- DON'T assume `withAnimation` respects Reduce Motion — it doesn't; you must check manually

## accessibilityLabel vs accessibilityIdentifier vs accessibilityHint

| Property | Purpose | User-facing? | Localized? |
|---|---|---|---|
| `accessibilityLabel` | Read by VoiceOver to describe the element | YES | YES |
| `accessibilityIdentifier` | UI automation/testing only | NO | NO |
| `accessibilityHint` | Extra context read after a pause | YES | YES |
| `accessibilityValue` | Current value of the element ("50%", "On") | YES | YES |

## SwiftUI Top 10 Anti-Patterns

1. `onTapGesture` instead of `Button`
2. No `.accessibilityLabel` on `Image(systemName:)` buttons
3. `.font(.system(size:))` instead of Dynamic Type
4. No grouping with `.accessibilityElement(children:)`
5. Missing `.accessibilityAddTraits(.isHeader)` on section headers
6. No `.accessibilityAdjustableAction` for custom controls
7. Ignoring `@Environment(\.accessibilityReduceMotion)`
8. `foregroundColor(.black)` instead of `.foregroundStyle(.primary)`
9. Not hiding decorative images with `Image(decorative:)`
10. Color-only state with no `.accessibilityDifferentiateWithoutColor` check

## UIKit Top 10 Anti-Patterns

1. Assigning traits (`= .selected`) instead of inserting (`.insert(.selected)`)
2. `isAccessibilityElement = false` on disabled buttons instead of `.notEnabled`
3. No `numberOfLines = 0` + `adjustsFontForContentSizeCategory = true` on labels
4. Not posting `.layoutChanged`/`.screenChanged` notifications
5. Forgetting `shouldGroupAccessibilityChildren = true`
6. No `accessibilityIncrement()`/`accessibilityDecrement()` for adjustable elements
7. Auto-generated labels from image filenames
8. No `accessibilityViewIsModal = true` on custom modals
9. Not overriding `accessibilityElements` for custom navigation order
10. Missing `accessibilityCustomActions` for list cell actions
