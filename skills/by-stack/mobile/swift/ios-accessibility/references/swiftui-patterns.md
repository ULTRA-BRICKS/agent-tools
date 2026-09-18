# SwiftUI Accessibility Patterns — Complete Reference

All SwiftUI accessibility modifiers and component-specific patterns for building accessible interfaces.

## Table of Contents

1. [Core Modifiers](#core-modifiers)
2. [Advanced Modifiers](#advanced-modifiers)
3. [Component Patterns](#component-patterns)
4. [Focus Management](#focus-management)

## Core Modifiers

### .accessibilityLabel
```swift
Button(action: share) { Image(systemName: "square.and.arrow.up") }
    .accessibilityLabel("Share")
```

### .accessibilityHint
```swift
Button("Delete").accessibilityHint("Permanently removes this message")
```

### .accessibilityValue
```swift
Slider(value: $volume, in: 0...100)
    .accessibilityLabel("Volume")
    .accessibilityValue("\(Int(volume)) percent")
```

### .accessibilityAddTraits / .accessibilityRemoveTraits
```swift
Text("Account Settings")
    .font(.headline)
    .accessibilityAddTraits(.isHeader)

Button("Option A") { }
    .accessibilityAddTraits(.isSelected)  // Keeps .isButton
```

### .accessibilityElement(children:)
```swift
// .combine — merge children labels
HStack { Text("Price"); Text("$9.99") }
    .accessibilityElement(children: .combine)
// VoiceOver: "Price, $9.99"

// .ignore — custom label on parent
HStack { Text("4"); Image(systemName: "star.fill") }
    .accessibilityElement(children: .ignore)
    .accessibilityLabel("4 out of 5 stars")
```

### .accessibilityHidden
```swift
Image("decorative-divider").accessibilityHidden(true)
```

### .accessibilitySortPriority
```swift
VStack {
    ErrorBanner().accessibilitySortPriority(2)   // Read first
    Title().accessibilitySortPriority(1)          // Read second
    Content()                                      // Read last (default 0)
}
```

### .accessibilityCustomActions
```swift
.accessibilityCustomActions([
    .init(named: "Like") { likeItem(); return true },
    .init(named: "Share") { shareItem(); return true }
])
```

### .accessibilityAdjustableAction
```swift
.accessibilityAdjustableAction { direction in
    switch direction {
    case .increment: value += 1
    case .decrement: value -= 1
    @unknown default: break
    }
}
```

## Advanced Modifiers

### .accessibilityRepresentation
Creates an alternative accessible view for complex custom views:
```swift
CustomGauge(value: 0.7)
    .accessibilityRepresentation {
        Slider(value: $gaugeValue, in: 0...1)
    }
```
**Use when:** Custom-drawn views need standard control behavior for assistive technology.

### .accessibilityChildren
Provides custom children for a container:
```swift
Canvas { context, size in
    // Custom drawing...
}
.accessibilityChildren {
    ForEach(dataPoints) { point in
        Text(point.label)
            .accessibilityLabel(point.description)
    }
}
```

### .accessibilityLabeledPair
Links label and control across view hierarchy:
```swift
Text("Username")
    .accessibilityLabeledPair(role: .label, id: "username", in: namespace)
TextField("", text: $username)
    .accessibilityLabeledPair(role: .content, id: "username", in: namespace)
```

### .accessibilityCustomContent
Extra information via "More Content" rotor:
```swift
ProductView(product)
    .accessibilityCustomContent("Price", "$9.99")
    .accessibilityCustomContent("Rating", "4.5 stars")
    .accessibilityCustomContent("In Stock", "Yes", importance: .high)
```
**Note:** `.high` importance items are read automatically with the label. Default importance requires rotor navigation.

### .accessibilityShowsLargeContentViewer
For controls that can't scale (toolbars, tab bars):
```swift
LocationButton()
    .dynamicTypeSize(...DynamicTypeSize.xxxLarge)
    .accessibilityShowsLargeContentViewer {
        Label("Recenter", systemImage: "location")
    }
```

### .accessibilityInputLabels (Voice Control)
```swift
Button(action: { }) { Image(systemName: "gearshape") }
    .accessibilityLabel("Settings gear icon with 3 notifications")
    .accessibilityInputLabels(["Settings", "Preferences", "Gear"])
// First label shown in Voice Control overlay — keep short and speakable
```

### .accessibilityZoomAction (iOS 17+)
```swift
.accessibilityZoomAction { action in
    switch action.direction {
    case .zoomIn: zoomValue += 1.0
    case .zoomOut: zoomValue -= 1.0
    }
}
```

### .accessibilityDirectTouch (iOS 17+)
```swift
Slider(value: $value).accessibilityDirectTouch(options: .silentOnTouch)
```

### .contentShape(.accessibility, Shape) (iOS 17+)
```swift
Image("circle").accessibilityLabel("Red")
    .contentShape(.accessibility, Circle())  // Circular hit area for VoiceOver
```

### .accessibilityLabel(_, isEnabled:) (iOS 18+)
```swift
Image(systemName: isFavorite ? "star.fill" : "star")
    .accessibilityLabel("Super Favorite", isEnabled: isFavorite)
```

### .accessibilityAction(named:, intent:) (iOS 18+)
```swift
.accessibilityAction(named: "Favorite") { FavoriteBeachIntent(beach: beach) }
```

## Component Patterns

### NavigationStack
```swift
NavigationStack {
    List { ... }
    .navigationTitle("Settings")  // VoiceOver announces on appear
}
```
Standard `NavigationLink` auto-handles screen change announcements.

### List/ForEach
```swift
// Section headers get .isHeader trait automatically
List {
    Section("Account") { /* items */ }
}

// Custom headers need the trait manually
Text("Account").font(.headline)
    .accessibilityAddTraits(.isHeader)
```

### Sheet/Alert/ConfirmationDialog
```swift
// Sheets auto-handle modal behavior
.sheet(isPresented: $showSheet) {
    SheetContent()
    // VoiceOver focus auto-trapped to sheet
    // Escape gesture auto-dismisses
}

// Alerts are fully accessible automatically
.alert("Delete?", isPresented: $showAlert) {
    Button("Delete", role: .destructive) { }
    Button("Cancel", role: .cancel) { }
}
```

### TabView
```swift
TabView {
    HomeView().tabItem { Label("Home", systemImage: "house") }
    SearchView().tabItem { Label("Search", systemImage: "magnifyingglass") }
}
// Auto-announces "Home, tab, 1 of 4"
// Badge: .badge(5) auto-reads "5 items"
```

### Toggle, Slider, Picker, Stepper
```swift
// Standard controls auto-announce name, role, value
Toggle("Airplane Mode", isOn: $airplaneMode)
// VoiceOver: "Airplane Mode. Toggle button. On. Double-tap to toggle."

Slider(value: $volume, in: 0...100)
    .accessibilityLabel("Volume")
    .accessibilityValue("\(Int(volume)) percent")

Picker("Sort by", selection: $sortOrder) {
    Text("Name").tag(SortOrder.name)
    Text("Date").tag(SortOrder.date)
}
```

### Image (Decorative vs Informative)
```swift
// Informative — provide label
Image("product-photo").accessibilityLabel("Red running shoes, side view")

// Decorative — hide from VoiceOver
Image(decorative: "background-gradient")  // Preferred
Image("separator").accessibilityHidden(true)  // Alternative

// SF Symbols: auto-labeled but often wrong — always override
Image(systemName: "star.fill")
    .accessibilityLabel("Favorite")
```

### TextField/SecureField
```swift
// ❌ Missing label
TextField("", text: $email)

// ✅ With prompt (auto-used as label)
TextField("Email address", text: $email)
    .textContentType(.emailAddress)

// ✅ Explicit label when prompt differs from label
TextField("user@example.com", text: $email)
    .accessibilityLabel("Email address")
    .textContentType(.emailAddress)
```

### Canvas
```swift
// Canvas draws pixels — completely invisible to VoiceOver
Canvas { context, size in /* drawing */ }
    .accessibilityLabel("Sales chart showing monthly data")
    .accessibilityChildren {
        ForEach(dataPoints) { point in
            Rectangle()
                .accessibilityLabel("\(point.month): \(point.value) units")
        }
    }
```

### Charts Framework
```swift
// Swift Charts has built-in accessibility (audio graphs, data summaries)
Chart(data) { item in
    BarMark(x: .value("Month", item.month), y: .value("Sales", item.sales))
}
// Auto-generates: chart type, axis info, data point labels
// Audio Graphs: VoiceOver users can "play" the chart as audio
.accessibilityChartDescriptor(self)  // For custom descriptions
```

### Custom Shapes/Path
```swift
MyCustomShape()
    .fill(.blue)
    .accessibilityLabel("Progress indicator")
    .accessibilityValue("75 percent complete")
    .contentShape(.accessibility, Rectangle())  // iOS 17: custom hit area
```

## Focus Management

### @AccessibilityFocusState
```swift
enum FocusableField: Hashable { case email, password, error }
@AccessibilityFocusState private var accessFocus: FocusableField?

Text("Error: Invalid email")
    .accessibilityFocused($accessFocus, equals: .error)

// Programmatic focus change (MUST use async delay)
DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
    accessFocus = .error
}
```

**Separate from @FocusState:** `@AccessibilityFocusState` controls VoiceOver/AT focus, `@FocusState` controls keyboard input focus. These are independent systems — set both when needed.
