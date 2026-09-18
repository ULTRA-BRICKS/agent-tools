# iOS 17/18/26 New Accessibility Features

New APIs and platform features across recent iOS releases.

## iOS 17 (WWDC23)

### AccessibilityNotification API (SwiftUI-native)

Replaces the UIKit `UIAccessibility.post(notification:argument:)` pattern:

```swift
// Announcement
AccessibilityNotification.Announcement("Loading complete").post()

// With priority (prevents interruption by other speech)
var announcement = AttributedString("Camera Active")
announcement.accessibilitySpeechAnnouncementPriority = .high
AccessibilityNotification.Announcement(announcement).post()

// Screen changed
AccessibilityNotification.ScreenChanged(newFocusElement).post()

// Layout changed
AccessibilityNotification.LayoutChanged(element).post()
```

### .isToggle Trait

```swift
Button { object.isFavorite.toggle() } label: { Image(systemName: "star") }
    .accessibilityAddTraits(.isToggle)
// VoiceOver: "Favorite. Toggle button."
```

### Content Shape for Accessibility

```swift
Image("circle").accessibilityLabel("Red")
    .contentShape(.accessibility, Circle())  // Circular hit area for VoiceOver
```

### Direct Touch

```swift
Slider(value: $value).accessibilityDirectTouch(options: .silentOnTouch)
// Bypasses VoiceOver navigation for this element
```

### Zoom Actions

```swift
.accessibilityZoomAction { action in
    switch action.direction {
    case .zoomIn: zoomValue += 1.0
    case .zoomOut: zoomValue -= 1.0
    }
}
```

### .focusable() Modifier

Makes non-input views focusable for Full Keyboard Access:
```swift
CustomCard().focusable()
```

### User Features
- **Assistive Access** — simplified interface mode for cognitive disabilities
- **Personal Voice** — 150 sentences to create AI voice clone
- **Live Speech** — type-to-speak for in-person and calls
- **Point and Speak** — LiDAR + ML reads labels on physical objects

## iOS 18 (WWDC24)

### Conditional Labels

```swift
Image(systemName: isFavorite ? "star.fill" : "star")
    .accessibilityLabel("Super Favorite", isEnabled: isFavorite)
// Only uses "Super Favorite" when isFavorite is true
```

### View-Based Labels

```swift
.accessibilityLabel { Text(rating) + Text(existingLabel) }
// Compose labels from multiple Text views
```

### AppIntent in Accessibility Actions

```swift
.accessibilityAction(named: "Favorite") { FavoriteBeachIntent(beach: beach) }
// Triggers an AppIntent directly from VoiceOver custom actions
```

### User Features
- **Eye Tracking** — front camera only, no additional hardware needed
- **Music Haptics** — haptic feedback synchronized to music
- **Vocal Shortcuts** — custom voice commands for any action
- **Vehicle Motion Cues** — reduces motion sickness with visual indicators
- **Enhanced Braille support** — improved refreshable Braille display integration

## iOS 26 (WWDC 2025)

### Platform Features
- **Accessibility Nutrition Labels** — App Store metadata declaring accessibility features
- **Accessibility Reader** — system-wide reading mode for any app
- **Brain-Computer Interface (BCI) protocol** — Switch Control via BCI devices
- **Personal Voice improvements** — only 10 phrases needed (down from 150)
- **Head Tracking enhancements** — custom facial expression action mapping
- **Assistive Access API** — developers can create tailored simplified experiences
- **Voice Control programming mode** — developer-specific voice commands for Xcode
- **Liquid Glass design** → prompted system-wide Reduce Transparency improvements

## New Modifier Summary

| Modifier | iOS | Purpose |
|---|---|---|
| `.accessibilityAddTraits(.isToggle)` | 17+ | Toggle trait for custom toggle controls |
| `.contentShape(.accessibility, Shape)` | 17+ | Custom hit path for VoiceOver |
| `.accessibilityDirectTouch()` | 17+ | Bypass VoiceOver navigation for element |
| `.accessibilityZoomAction` | 17+ | Custom zoom behavior |
| `AccessibilityNotification` | 17+ | Native SwiftUI notification posting |
| `.focusable()` | 17+ | Make non-inputs keyboard-focusable |
| `.accessibilityLabel(_, isEnabled:)` | 18+ | Conditional labels |
| `.accessibilityLabel { View }` | 18+ | View-based label composition |
| `.accessibilityAction(named:, intent:)` | 18+ | AppIntent-backed actions |
