# Motion, Animation & Alternative Input — Complete Reference

Covers reduce motion, animation safety, Switch Control, Voice Control, and Full Keyboard Access.

## Table of Contents

1. [Reduce Motion](#reduce-motion)
2. [What Counts as Motion](#what-counts-as-motion)
3. [Flash/Seizure Thresholds](#flashseizure-thresholds)
4. [Switch Control](#switch-control)
5. [Voice Control](#voice-control)
6. [Full Keyboard Access](#full-keyboard-access)
7. [@FocusState vs @AccessibilityFocusState](#focusstate-vs-accessibilityfocusstate)

## Reduce Motion

```swift
// ❌ Ignores user preference
withAnimation(.spring(response: 0.5, dampingFraction: 0.3)) {
    showDetail.toggle()
}

// ✅ Respects reduce motion
@Environment(\.accessibilityReduceMotion) var reduceMotion

withAnimation(reduceMotion ? .none : .spring()) {
    showDetail.toggle()
}

// ✅ Replace motion with crossfade, not removal
.transition(reduceMotion ? .opacity : .slide)
.animation(reduceMotion ? .easeInOut(duration: 0.2) : .spring(), value: state)
```

**Helper function:**
```swift
func withOptionalAnimation<Result>(
    _ animation: Animation? = .default,
    _ body: () throws -> Result
) rethrows -> Result {
    if UIAccessibility.isReduceMotionEnabled {
        return try body()
    } else {
        return try withAnimation(animation, body)
    }
}
```

**Key principle:** Don't remove ALL animation when reduce motion is enabled — replace aggressive motion with subtle crossfade/opacity transitions. Complete removal feels broken.

## What Counts as Motion

**Needs reduce-motion alternative:**
- Parallax effects
- Scaling/zooming transitions
- Sliding/pushing transitions
- Spinning/rotating animations
- 3D transforms
- Auto-scrolling carousels
- Auto-playing video
- Bouncing springs

**Generally safe (no alternative needed):**
- Opacity/crossfade transitions
- Color changes
- Simple state changes without spatial movement

## Flash/Seizure Thresholds

**WCAG 2.3.1 (Level A):** No more than **3 flashes per second**. Red flashing is especially dangerous.

```swift
// ❌ DANGEROUS: 5 flashes per second
Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { _ in
    view.isHidden.toggle()
}

// ✅ Safe: smooth transitions, never binary on/off
withAnimation(.easeInOut(duration: 0.5)) {
    opacity = isActive ? 1.0 : 0.3
}
```

## Switch Control

If the app is accessible to VoiceOver, it's mostly accessible to Switch Control. Key additions:

```swift
// UIKit: custom action images for Switch Control menu (iOS 14+)
override var accessibilityCustomActions: [UIAccessibilityCustomAction]? {
    get {
        [UIAccessibilityCustomAction(
            name: "Pin",
            image: UIImage(systemName: "pin"),
            target: self,
            selector: #selector(pinAction))]
    }
    set { }
}
```

**What helps Switch Control:**
- Standard `Button` controls (scanned automatically)
- Clear `accessibilityLabel` (shown in Switch Control menu)
- `accessibilityCustomActions` (appears as menu items)
- Adequate touch targets (44x44pt minimum)

## Voice Control

Voice Control overlays element names from `accessibilityLabel`. Users say "Tap [label]" to activate.

### accessibilityInputLabels
Short, speakable alternatives for Voice Control:

```swift
// SwiftUI
Button { } label: { Image(systemName: "gearshape") }
    .accessibilityLabel("Settings with 3 notifications")
    .accessibilityInputLabels(["Settings", "Preferences", "Gear"])
// First label shown in Voice Control overlay — keep short and speakable

// UIKit
button.accessibilityUserInputLabels = ["Settings", "Preferences", "Gear"]
```

**Rules for Voice Control labels:**
- Keep labels speakable — no abbreviations, no developer jargon
- Make labels unique on screen — Voice Control can't disambiguate duplicates
- First input label supersedes `accessibilityLabel` for Voice Control display
- `accessibilityInputLabels` also improves Full Keyboard Access "Find" feature (Cmd+F)

### WCAG 2.5.3: Label in Name
The `accessibilityLabel` MUST contain the visible text. If a button shows "Submit", the label must include "Submit" (not "Send form").

## Full Keyboard Access

Tab navigates between focusable elements, Space/Enter activates.

**Limitations:**
- `@FocusState` does NOT move Full Keyboard Access focus to non-TextField elements (known limitation)
- `accessibilityInputLabels` improves the "Find" feature (Cmd+F)

**What helps FKA:**
- All standard controls are keyboard-focusable by default
- `.focusable()` modifier (iOS 17+) makes non-inputs focusable
- Clear focus indicators (3:1 contrast — WCAG 2.4.13)
- Logical tab order (matches visual order)

## @FocusState vs @AccessibilityFocusState

These are SEPARATE systems — set both when needed:

```swift
@FocusState private var keyboardFocus: Field?          // Keyboard input focus
@AccessibilityFocusState private var voFocus: Field?   // VoiceOver/AT focus

// Move both when showing an error
func showError() {
    keyboardFocus = .email
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
        voFocus = .error  // VoiceOver focus needs async delay
    }
}
```

**Key difference:**
- `@FocusState` — controls which text field receives keyboard input
- `@AccessibilityFocusState` — controls which element VoiceOver/AT reads next
- Moving one does NOT move the other
- `@AccessibilityFocusState` requires an async delay to work programmatically
