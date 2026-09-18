# AI Failure Patterns — Complete Reference

AI coding assistants systematically produce inaccessible iOS code. This reference documents all 11 failure patterns with vulnerable/correct code pairs and detection strategies.

## F1: onTapGesture Instead of Button

**Severity:** 🔴 CRITICAL
**Impact:** Element completely invisible to VoiceOver, Switch Control, Full Keyboard Access, and visionOS eye tracking.
**Detection:** Search for `.onTapGesture` on views that perform actions.

```swift
// ❌ AI-generated: invisible to all assistive technology
Image(systemName: "heart.fill")
    .onTapGesture { toggleFavorite() }

// ✅ Correct: Button gets .isButton trait automatically
Button(action: toggleFavorite) {
    Image(systemName: "heart.fill")
}
.accessibilityLabel("Add to favorites")
```

**Why it matters:** `onTapGesture` adds a gesture recognizer but does NOT add the `.isButton` accessibility trait. VoiceOver skips the element entirely during linear navigation. Switch Control cannot scan to it. Voice Control cannot target it. This is the single most common and most damaging AI failure.

## F2: Hardcoded Font Sizes

**Severity:** 🟡 HIGH
**Impact:** Text doesn't scale for 25%+ of users who change their text size setting.
**Detection:** Search for `.font(.system(size:`.

```swift
// ❌ AI-generated (especially Claude): fixed size
Text("Welcome").font(.system(size: 24))

// ✅ Correct: Dynamic Type text style
Text("Welcome").font(.title)
```

```swift
// ❌ UIKit: fixed size
label.font = UIFont.systemFont(ofSize: 16)

// ✅ UIKit: Dynamic Type with live updating
label.font = UIFont.preferredFont(forTextStyle: .body)
label.adjustsFontForContentSizeCategory = true
label.numberOfLines = 0
```

**Text Style Mapping:**
| Old Pattern | Dynamic Type Replacement |
|---|---|
| `.system(size: 34)` | `.largeTitle` |
| `.system(size: 28)` | `.title` |
| `.system(size: 22)` | `.title2` |
| `.system(size: 20)` | `.title3` |
| `.system(size: 17)` | `.headline` (bold) or `.body` |
| `.system(size: 16)` | `.callout` |
| `.system(size: 15)` | `.subheadline` |
| `.system(size: 13)` | `.footnote` |
| `.system(size: 12)` | `.caption` |
| `.system(size: 11)` | `.caption2` |

## F3: Hardcoded Colors

**Severity:** 🟡 HIGH
**Impact:** Text invisible or unreadable in dark mode. May also fail contrast requirements.
**Detection:** Search for `.foregroundColor(.black)`, `.foregroundColor(.white)`, `.background(Color.white)`, `.background(Color.black)`, hex color literals for text/backgrounds.

```swift
// ❌ AI-generated: invisible in dark mode
Text("Hello").foregroundColor(.black)
    .background(Color.white)

// ✅ Correct: semantic colors that adapt
Text("Hello").foregroundStyle(.primary)
    .background(Color(.systemBackground))
```

**Semantic Color Mapping:**
| Hardcoded | Semantic Replacement |
|---|---|
| `.black` (text) | `.primary` |
| `.gray` (text) | `.secondary` |
| `.white` (background) | `Color(.systemBackground)` |
| `.init(white: 0.95)` (bg) | `Color(.secondarySystemBackground)` |
| `.init(white: 0.9)` (bg) | `Color(.tertiarySystemBackground)` |

## F4: Deprecated API

**Severity:** 🟡 HIGH
**Impact:** Code uses deprecated patterns that may have accessibility improvements in modern replacements.
**Detection:** Search for `.foregroundColor(`, `.cornerRadius(`, `NavigationView`.

```swift
// ❌ AI generates deprecated API constantly
.foregroundColor(.blue)           // deprecated
.cornerRadius(10)                 // deprecated
NavigationView { }                // deprecated since iOS 16

// ✅ Modern equivalents
.foregroundStyle(.blue)
.clipShape(.rect(cornerRadius: 10))
NavigationStack { }
```

## F5: No Accessibility Labels on Image Buttons

**Severity:** 🔴 CRITICAL
**Impact:** VoiceOver reads raw SF Symbol name or just "Button" with no context.
**Detection:** `Button` containing only `Image(systemName:)` without `.accessibilityLabel`.

```swift
// ❌ VoiceOver reads "plus" or "Button"
Button(action: addItem) { Image(systemName: "plus") }

// ✅ With meaningful label
Button(action: addItem) { Image(systemName: "plus") }
    .accessibilityLabel("Add item")
```

**Label quality rules:**
- Describe the ACTION, not the icon: "Add item" not "Plus icon"
- Don't include "button": "Add item" not "Add item button"
- Update when state changes: "Add to favorites" / "Remove from favorites"
- Provide context for identical icons: "Add peanut butter" not just "Add"

## F6: GeometryReader Abuse and Fixed Frames

**Severity:** 🟡 HIGH
**Impact:** Layout breaks at accessibility text sizes, different screen sizes, and zoom.
**Detection:** Search for `GeometryReader` and `.frame(width:` with hardcoded pixel values.

```swift
// ❌ Fixed frame prevents Dynamic Type growth
Text("Price: $9.99")
    .frame(width: 120, height: 44)

// ✅ Let the text grow
Text("Price: $9.99")
    .frame(minWidth: 44, minHeight: 44)  // Only minimum for touch target
```

**Rule:** Avoid `GeometryReader` unless absolutely necessary for custom layout calculations. Avoid fixed `frame(width:height:)`. Use flexible layout with `minWidth`/`minHeight` for touch targets only.

## F7: No Accessibility on Custom Controls

**Severity:** 🔴 CRITICAL
**Impact:** Custom controls appear as static text or are invisible to VoiceOver.
**Detection:** Custom views with tap handling but no accessibility modifiers.

```swift
// ❌ AI-generated custom toggle: no accessibility
HStack {
    Text("Dark Mode")
    Circle()
        .fill(isDarkMode ? .blue : .gray)
        .onTapGesture { isDarkMode.toggle() }
}

// ✅ Accessible custom toggle
Button(action: { isDarkMode.toggle() }) {
    HStack {
        Text("Dark Mode")
        Circle().fill(isDarkMode ? .blue : .gray)
    }
}
.accessibilityElement(children: .combine)
.accessibilityAddTraits(.isToggle)
.accessibilityValue(isDarkMode ? "On" : "Off")
```

**Required accessibility for custom controls:**
- `accessibilityLabel` — what is this control
- `accessibilityValue` — current state ("On"/"Off", "50%", "3 of 5")
- `accessibilityTraits` — what kind of control (.button, .adjustable, .isToggle)
- `accessibilityAdjustableAction` — for sliders/steppers (increment/decrement on swipe up/down)
- `accessibilityCustomActions` — for multi-action elements

## F8: accessibilityIdentifier vs accessibilityLabel Confusion

**Severity:** 🔴 CRITICAL
**Impact:** Element has an identifier for testing but VoiceOver reads nothing useful.

```swift
// ❌ Using identifier where label needed
button.accessibilityIdentifier = "favorite_button"
// VoiceOver: says nothing useful — identifier is not read aloud

// ❌ Using developer jargon as label
button.accessibilityLabel = "btn_favorite_123"
// VoiceOver: "btn underscore favorite underscore one two three"

// ✅ Correct: both set independently
button.accessibilityLabel = "Add to favorites"     // For VoiceOver
button.accessibilityIdentifier = "favoriteButton"  // For UI tests
```

## F9: Missing System Preference Checks

**Severity:** 🟡 HIGH
**Impact:** App ignores user's accessibility settings — animations persist, transparency persists, color-only indicators persist.
**Detection:** Search for `withAnimation` without `reduceMotion` check, `.blur`/`.ultraThinMaterial` without `reduceTransparency` check.

```swift
// ❌ AI ignores reduce motion
withAnimation(.spring()) { showDetails.toggle() }

// ✅ Respect reduce motion
@Environment(\.accessibilityReduceMotion) var reduceMotion
withAnimation(reduceMotion ? .none : .spring()) { showDetails.toggle() }
```

**Complete environment values to check:**
```swift
@Environment(\.accessibilityReduceMotion) var reduceMotion
@Environment(\.accessibilityReduceTransparency) var reduceTransparency
@Environment(\.legibilityWeight) var legibilityWeight           // Bold Text
@Environment(\.colorSchemeContrast) var contrast                // Increase Contrast
@Environment(\.accessibilityDifferentiateWithoutColor) var diffWithoutColor
@Environment(\.dynamicTypeSize) var dynamicTypeSize
@Environment(\.accessibilityVoiceOverEnabled) var voiceOverEnabled
@Environment(\.accessibilityInvertColors) var invertColors
```

**When to check each:**
| Setting | Check when... |
|---|---|
| `reduceMotion` | Any `withAnimation`, `.transition`, `.spring()`, parallax, auto-scroll |
| `reduceTransparency` | `.blur`, `.ultraThinMaterial`, any visual effect |
| `legibilityWeight` | Custom font weights that might need bolder variants |
| `contrast` | Custom colors that might need higher contrast variants |
| `diffWithoutColor` | Color-only status indicators (red/green/yellow) |
| `dynamicTypeSize` | Layout that needs HStack→VStack switch at accessibility sizes |
| `invertColors` | Photos/videos/maps that should not be inverted |

## F10: Assigning Traits Instead of Inserting (UIKit)

**Severity:** 🔴 CRITICAL
**Impact:** Destroys existing traits. A button becomes non-button, breaking VoiceOver interaction.
**Detection:** Search for `.accessibilityTraits = ` (assignment operator, not `.insert` or `.union`).

```swift
// ❌ DESTROYS the button trait — button is no longer announced as button
myButton.accessibilityTraits = .selected

// ✅ INSERT/UNION preserves existing traits
myButton.accessibilityTraits.insert(.selected)

// ✅ SwiftUI: use AddTraits/RemoveTraits
Button("Option A") { }
    .accessibilityAddTraits(.isSelected)  // Keeps .isButton
```

**Common UIKit trait combinations:**
```swift
// Selected tab button:
tab.accessibilityTraits = [.button, .selected]  // OK only at initial setup
tab.accessibilityTraits.insert(.selected)        // Always safe for state changes

// Disabled link:
link.accessibilityTraits.insert(.notEnabled)     // Preserves .link

// Section header button:
headerButton.accessibilityTraits = [.header, .button]  // OK at initial setup
```

## F11: Hiding Disabled Controls from VoiceOver

**Severity:** 🔴 CRITICAL
**Impact:** User doesn't know the control exists. They can't discover it to understand what's needed to enable it.

```swift
// ❌ User has no idea this button exists
submitButton.isAccessibilityElement = false

// ✅ UIKit: notEnabled trait makes VoiceOver say "dimmed"
submitButton.accessibilityTraits.insert(.notEnabled)
// VoiceOver: "Submit, dimmed"

// ✅ SwiftUI: .disabled() automatically adds notEnabled trait
Button("Submit") { submitForm() }
    .disabled(!formIsValid)
// VoiceOver: "Submit. Dimmed. Button."
```

**Why "dimmed" matters:** When a VoiceOver user hears "Submit, dimmed", they know: (1) the button exists, (2) they can't activate it yet, (3) they should look for what to fill in to enable it. Hiding the button removes this crucial information.
