# VoiceOver Patterns — Complete Reference

VoiceOver is the primary assistive technology on iOS, used by blind and low-vision users. Every interactive element must be navigable, identifiable, and actionable via VoiceOver.

## Table of Contents

1. [accessibilityLabel Rules](#accessibilitylabel-rules)
2. [accessibilityHint Rules](#accessibilityhint-rules)
3. [accessibilityTraits Reference](#accessibilitytraits-reference)
4. [Element Grouping](#element-grouping)
5. [Custom Actions](#custom-actions)
6. [Navigation Order](#navigation-order)
7. [Post Notifications](#post-notifications)
8. [Modal Views & Focus Management](#modal-views--focus-management)
9. [Custom Rotors](#custom-rotors)
10. [Adjustable Controls](#adjustable-controls)
11. [Decorative Images](#decorative-images)
12. [Magic Tap & Escape](#magic-tap--escape)

## accessibilityLabel Rules

| Rule | Bad | Good |
|---|---|---|
| Don't include element type | "Play button" | "Play" |
| Don't include redundant context | "Play song button" (in music player) | "Play" |
| Update when state changes | Static "Add" | "Add" / "Remove" (toggled) |
| Provide context for identical actions | "Add" (which item?) | "Add peanut butter" |
| Keep short | "Press this button to add item to your cart" | "Add to cart" |
| No image filenames | "plus_icon_outline_#999" | "Add" |
| Don't describe appearance | "Red warning triangle" | "Warning" |
| Capitalize naturally | "ADD TO CART" | "Add to cart" |

## accessibilityHint Rules

Hints are read AFTER a short delay. They describe the RESULT of the action, not the action itself.

**Rules:**
- Begin with a verb (third person, as in "Opens the settings")
- Describe the result, not the gesture
- Only add when the label alone isn't enough

```swift
// ❌ Redundant hint — repeats the label
Button("Delete").accessibilityHint("Tap to delete")

// ❌ Describes gesture, not result
Button("Delete").accessibilityHint("Double tap this button")

// ✅ Describes the result
Button("Delete").accessibilityHint("Permanently removes this message")

// ✅ No hint needed — label is sufficient
Button("Sign Out")  // Action is self-explanatory
```

## accessibilityTraits Reference

| Trait | VoiceOver Announces | When to Use |
|---|---|---|
| `.button` | "Button" | Tappable non-button elements, views with tap gestures |
| `.link` | "Link" | Navigation to web/phone; use `.button` for in-app navigation |
| `.header` | "Heading" | Section headings; enables rotor heading navigation |
| `.selected` | "Selected" | Active tab, radio, checkbox, segment |
| `.notEnabled` | "Dimmed" | Disabled controls (NEVER hide them from VoiceOver) |
| `.adjustable` | "Adjustable" | Sliders, pickers, steppers — MUST implement increment/decrement |
| `.image` | "Image" | Meaningful images (NOT decorative) — enables ML description |
| `.staticText` | — | Default on labels; rarely needs to be set manually |
| `.updatesFrequently` | — | Timers, scoreboards; causes AT to poll for changes |
| `.summaryElement` | — | Overview area (e.g., Weather top section) — read first on appear |
| `.startsMediaSession` | — | Play/record buttons; prevents VoiceOver from interrupting |
| `.allowsDirectInteraction` | — | Drawing canvas, instruments; disables VO navigation for the element |
| `.causesPageTurn` | — | E-book pages; auto-turns when AT finishes reading |
| `.tabBar` | "Tab, X of Y" | Container of tab buttons |
| `.searchField` | "Search field" | Search text fields |
| `.keyboardKey` | (phonetic hint) | Custom keyboard keys |
| `.playsSound` | — | Elements that provide own audio feedback |
| `.isToggle` (iOS 17+) | "Toggle" | Toggle-like custom controls |
| `.isModal` | — | Modal content (SwiftUI) |

## Element Grouping

### accessibilityElement(children:) Decision Guide

| Mode | Behavior | When to Use |
|---|---|---|
| `.contain` | Each child stays separate in a container | Form with multiple inputs, settings with individual controls |
| `.combine` | Merges all children into ONE element; labels joined with commas | Product card (name + price), list cell with related info |
| `.ignore` | Hides all children; you provide custom label on parent | Score display, tightly coupled text needing natural sentence |

```swift
// ❌ No grouping — 12 swipes for a 3-row table
VStack {
    HStack { icon; Text("Feature"); Text("Basic"); Text("Pro") }
    HStack { icon; Text("Storage"); Text("5GB"); Text("100GB") }
}

// ✅ Group each row
HStack { icon; Text("Storage"); Text("5GB"); Text("100GB") }
    .accessibilityElement(children: .combine)

// ✅ For natural sentence reading
HStack { ... }
    .accessibilityElement(children: .ignore)
    .accessibilityLabel("Storage: 5 gigabytes for Basic, 100 gigabytes for Pro")
```

**Key:** `.combine` adds a pause between elements. `.ignore` + custom label reads as a single natural sentence. Buttons inside `.combine` are combined by default in SwiftUI and cannot be "uncombined."

## Custom Actions

Solve the problem of repeated button lists in cells:

```swift
// ❌ Each button a separate swipe target (3+ swipes per cell)
HStack {
    Text(item.name)
    Button("Like") { }
    Button("Share") { }
    Button("Delete") { }
}

// ✅ One swipe to reach item, swipe up/down cycles through actions
HStack { Text(item.name) }
    .accessibilityElement(children: .combine)
    .accessibilityCustomActions([
        .init(named: "Like") { likeItem(); return true },
        .init(named: "Share") { shareItem(); return true },
        .init(named: "Delete") { deleteItem(); return true }
    ])
```

**UIKit custom actions:**
```swift
cell.accessibilityCustomActions = [
    UIAccessibilityCustomAction(name: "Add to Wishlist",
        target: self, selector: #selector(addToWishlist)),
    UIAccessibilityCustomAction(name: "Add to Basket",
        target: self, selector: #selector(addToBasket))
]
```

## Navigation Order

**SwiftUI sort priority** (higher = read earlier):
```swift
VStack {
    BarsView(bars: bars)
    LabelsView(bars: bars).accessibilityHidden(true)
    LegendView(bars: bars).accessibilitySortPriority(1)  // Read FIRST
}
```

**UIKit custom order:**
```swift
view.accessibilityElements = [headerLabel, contentView, actionButton, footerLabel]
containerView.shouldGroupAccessibilityChildren = true
```

**Warning:** Setting `accessibilityElements` completely overrides default ordering — you become responsible for ALL elements.

## Post Notifications

| Notification | Sound | Focus Change | When to Use |
|---|---|---|---|
| `.screenChanged` | "Beep-boop" | Moves to argument or first element | New screen/view controller appears |
| `.layoutChanged` | None | Moves to argument element | Part of screen changes (element added/removed) |
| `.announcement` | None | Does NOT move focus | Informational ("Item deleted", "Loading complete") |

```swift
// UIKit
UIAccessibility.post(notification: .screenChanged, argument: newView)
UIAccessibility.post(notification: .layoutChanged, argument: buttonToFocus)
UIAccessibility.post(notification: .announcement, argument: "3 new messages")

// SwiftUI (iOS 17+)
AccessibilityNotification.Announcement("Download complete").post()
AccessibilityNotification.ScreenChanged(newFocusElement).post()
AccessibilityNotification.LayoutChanged(element).post()

// Priority announcements (iOS 17+) — prevents interruption by other speech
var announcement = AttributedString("Camera Active")
announcement.accessibilitySpeechAnnouncementPriority = .high
AccessibilityNotification.Announcement(announcement).post()
```

**Anti-pattern:** Don't over-notify. Announcements can be lost if VoiceOver is already speaking. SwiftUI auto-handles most notifications via state tracking — rarely need manual posting.

## Modal Views & Focus Management

```swift
// UIKit
modalView.accessibilityViewIsModal = true
// CRITICAL: Only SIBLINGS are hidden, not ALL other views.
// Modal must be direct child of window for this to work correctly.
UIAccessibility.post(notification: .screenChanged, argument: modalView)

// SwiftUI
.accessibilityAddTraits(.isModal)
// .sheet() auto-handles modal behavior
```

**Gotcha:** `accessibilityViewIsModal` only hides the modal view's SIBLINGS from VoiceOver, not all other views. View hierarchy matters enormously. If the modal is deeply nested, parent siblings won't be hidden.

## Custom Rotors

```swift
// SwiftUI
List {
    ForEach(items) { item in
        ItemView(item: item)
            .accessibilityRotorEntry(id: item.id, in: rotorNamespace)
    }
}
.accessibilityRotor("Warnings") {
    ForEach(items.filter(\.isWarning)) { item in
        AccessibilityRotorEntry(item.title, item.id, in: rotorNamespace)
    }
}

// UIKit
let rotor = UIAccessibilityCustomRotor(name: "Stores") { predicate in
    let direction = predicate.searchDirection
    // Find next/previous store element based on direction
    return UIAccessibilityCustomRotorItemResult(targetElement: element, targetRange: nil)
}
view.accessibilityCustomRotors = [rotor]
```

**Note:** Custom rotors must handle BOTH `.next` and `.previous` search directions.

## Adjustable Controls

```swift
// SwiftUI
struct CustomStepper: View {
    @Binding var value: Int
    var body: some View {
        HStack { /* visual UI */ }
            .accessibilityElement()
            .accessibilityLabel("Quantity")
            .accessibilityValue("\(value)")
            .accessibilityAdjustableAction { direction in
                switch direction {
                case .increment: value += 1
                case .decrement: value -= 1
                @unknown default: break
                }
            }
    }
}

// UIKit
class CustomSlider: UIView {
    override var accessibilityTraits: UIAccessibilityTraits {
        get { super.accessibilityTraits.union(.adjustable) }
        set { super.accessibilityTraits = newValue }
    }
    override func accessibilityIncrement() { value += step }
    override func accessibilityDecrement() { value -= step }
}
```

**Critical:** The `.adjustable` trait REQUIRES `accessibilityIncrement()`/`accessibilityDecrement()` implementation — crash or undefined behavior if missing.

## Decorative Images

```swift
// SwiftUI
Image(decorative: "background")   // Preferred — ignored by VoiceOver
Image("logo").accessibilityHidden(true)  // Alternative

// UIKit
imageView.isAccessibilityElement = false
imageView.accessibilityElementsHidden = true  // Hides children too
```

## Magic Tap & Escape

```swift
// UIKit — Magic Tap (two-finger double-tap): primary action of the screen
override func accessibilityPerformMagicTap() -> Bool {
    togglePlayPause()
    return true
}

// UIKit — Escape (two-finger Z): dismiss/back
override func accessibilityPerformEscape() -> Bool {
    dismiss(animated: true)
    return true
}
```
