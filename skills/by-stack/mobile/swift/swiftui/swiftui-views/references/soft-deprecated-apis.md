# Deprecated SwiftUI APIs and Their Replacements

SwiftUI's deprecation story has two distinct tiers, and conflating them leads to the wrong
urgency in code review:

- **Soft-deprecated** — marked `@available(..., deprecated: 100000.0, ...)` in the SDK
  headers. `100000.0` is a placeholder version that suppresses compiler warnings while
  signaling "don't use this in new code." These APIs still compile and work correctly today.
  Treat them as **informational**, not urgent. Examples: `NavigationView` (use
  `NavigationStack`/`NavigationSplitView`), `ActionSheet`/`Alert` (use the
  `.confirmationDialog`/`.alert` modifiers), `MagnificationGesture` (renamed
  `MagnifyGesture`), `PresentationMode` (use `\.dismiss`).
- **Hard-deprecated / modernized** — APIs that were deprecated in an earlier SDK cycle and
  now carry a real version number, or patterns that Apple has since replaced with a clearly
  better modern API even where the old one hasn't been marked deprecated yet (e.g.
  `ObservableObject` → `@Observable`). These are grouped below by the minimum OS version that
  unlocks the replacement, because the correct choice depends on the deployment target.

Use the two reference lists below together: check "Soft-Deprecated SwiftUI APIs" for anything
with the `100000.0` placeholder, and "Hard-Deprecated / Modern API Replacements" for
version-gated modernization opportunities.

## How to identify soft-deprecated APIs

The "Soft-Deprecated SwiftUI APIs" list below (generated from iOS/macOS/tvOS/watchOS/visionOS
27.0 SDK headers) is the canonical reference — check it before recommending or flagging any
API. If you're working against a newer SDK than the versions it was generated from, the list
may be incomplete; in that case inspect the `@available` attribute directly in the SDK headers
— a soft-deprecated API has `deprecated: 100000.0`.

Do not rely on memory to judge whether an API is soft-deprecated. Any API — even one that
worked fine in a prior release — could have been soft-deprecated since. Verify against the
list.

## Scoping rule — read this first

All soft-deprecation guidance here is scoped to the code you are **directly modifying**. If a
file contains multiple views and the task only touches one of them, the other views are out of
scope.

**What to do**: Only discuss the view(s) you edited. Structure your response as code output,
then reasoning about *your changes* — nothing else.

**What not to do**: Do not mention, flag, comment on, offer to migrate, or ask about
soft-deprecated APIs in out-of-scope code. This includes trailing questions like "Would you
like me to migrate `OtherView` to `NavigationStack`?" — if you didn't edit that view, don't
bring it up. This rule takes precedence over any prompt asking for "observations" or "other
notes."

**Example of what NOT to do**: The user asks you to add a button to `SettingsView`. The same
file contains `DashboardView`, which uses `NavigationView`. Do not write "I noticed
`DashboardView` uses `NavigationView`, which is soft-deprecated" or mention `DashboardView` at
all — you weren't asked to touch it.

**Why**: mentioning soft-deprecated APIs in code the user didn't ask you to change creates
noise, distracts from the task, and pressures the user into unrelated work.

## When to migrate

- **Generating new code from scratch**: never introduce a new usage of a soft-deprecated API.
  If unsure whether an API is soft-deprecated, check the list below before recommending it.
- **Reviewing, refactoring, modernizing, or cleaning up**: point out soft-deprecated APIs in
  the code under review and suggest the modern replacement. Keep the tone informational — these
  still compile and run, so frame migration as an improvement, not a bug fix.
- **Adding a feature or fixing a bug**: if the view you're editing already uses a
  soft-deprecated API, **keep it as-is** in your change. Don't silently swap `NavigationView`
  for `NavigationStack` while adding a search bar — that produces an unexpected diff, risks
  regressions (state resets, navigation-behavior changes), and makes the change harder to
  review. After delivering the requested change, you may add one brief line offering to migrate
  as a separate follow-up step.

  *Example — view you ARE editing*: the user asks for a search bar on a view that uses
  `NavigationView`. Your code output must still use `NavigationView`. After the code block:
  "I noticed this view uses `NavigationView`, which is soft-deprecated. Want me to migrate it
  to `NavigationSplitView` as a follow-up?"

  *Example — view you are NOT editing*: the user asks for a search bar on `SearchView`. The
  same file has `HomeView`, which uses `NavigationView`. Say nothing about `HomeView`.

  If a *different* view in the same file uses a soft-deprecated API, ignore it entirely — see
  the scoping rule above.
- Migrations are real edits with behavioral risk (state resets, navigation-behavior changes).
  They belong in their own focused change, never bundled into an unrelated feature/bugfix diff.
- Don't proactively scan a codebase for soft-deprecated APIs — only notice them when they appear
  in code you're directly modifying for the current request.

---

## Soft-Deprecated SwiftUI APIs

Generated from: iOS 27.0, macOS 27.0, tvOS 27.0, watchOS 27.0, visionOS 27.0. Each entry is the
soft-deprecated declaration followed by its replacement.

### Types

- `struct CarouselTabViewStyle : TabViewStyle` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to VerticalTabViewStyle
- `struct MenuButton<Label, Content> : View where Label : View, Content : View` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use `Menu` instead.
- `struct ActionSheet` (iOS, macOS, tvOS, watchOS, visionOS)
  - use `View.confirmationDialog(title:isPresented:titleVisibility:presenting::actions:)`instead.
- `struct ColumnNavigationViewStyle : NavigationViewStyle` (iOS, macOS, tvOS, watchOS, visionOS)
  - replace styled NavigationView with NavigationSplitView
- `struct Alert` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use View.alert(_:isPresented:presenting:actions:) instead.
- `struct BorderedButtonMenuStyle : MenuStyle` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use .menuStyle(.button) and .buttonStyle(.bordered).
- `struct RotationGesture : Gesture` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to RotateGesture
- `struct PresentationMode` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use EnvironmentValues.isPresented or EnvironmentValues.dismiss
- `struct MagnificationGesture : Gesture` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to MagnifyGesture
- `struct ContextMenu<MenuItems> where MenuItems : View` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use `contextMenu(menuItems:)` instead.
- `struct PullDownMenuButtonStyle : MenuButtonStyle` (iOS, macOS, visionOS)
  - Use `BorderedButtonMenuStyle` instead.
- `struct BorderlessPullDownMenuButtonStyle : MenuButtonStyle` (iOS, macOS, visionOS)
  - Use `BorderlessButtonMenuStyle` instead.
- `struct BorderlessButtonMenuButtonStyle : MenuButtonStyle` (iOS, macOS, visionOS)
  - Use `BorderlessButtonMenuStyle` instead.
- `struct DefaultMenuButtonStyle : MenuButtonStyle` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use `menuStyle(.automatic)` instead.
- `struct DefaultNavigationViewStyle : NavigationViewStyle` (iOS, macOS, tvOS, watchOS, visionOS)
  - replace styled NavigationView with NavigationStack or NavigationSplitView instead
- `struct BorderlessButtonMenuStyle : MenuStyle` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use .menuStyle(.button) and .buttonStyle(.borderless).
- `struct DoubleColumnNavigationViewStyle : NavigationViewStyle` (iOS, macOS, tvOS, watchOS, visionOS)
  - replace styled NavigationView with NavigationStack or NavigationSplitView instead
- `struct NavigationView<Content> : View where Content : View` (iOS, macOS, tvOS, watchOS, visionOS)
  - use NavigationStack or NavigationSplitView instead
- `struct PopUpButtonPickerStyle : PickerStyle` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use the `menu` style instead.
- `struct StackNavigationViewStyle : NavigationViewStyle` (iOS, macOS, tvOS, watchOS, visionOS)
  - replace stack-styled NavigationView with NavigationStack
- `enum ContentSizeCategory : Hashable, CaseIterable, Sendable` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to DynamicTypeSize
- `enum ControlActiveState : Equatable, CaseIterable, Sendable` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use `EnvironmentValues.appearsActive` instead.

### Protocols

- `protocol NavigationViewStyle` (iOS, macOS, tvOS, watchOS, visionOS)
  - replace styled NavigationView with NavigationStack or NavigationSplitView instead
- `protocol AnimatableModifier : Animatable, ViewModifier` (iOS, macOS, tvOS, watchOS, visionOS)
  - use Animatable directly
- `protocol MenuButtonStyle` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use `MenuStyle` instead.

### Initializers

- `TextField.init<V>(_ titleKey: LocalizedStringKey, value: Binding<V>, formatter: Formatter, onEditingChanged: @escaping (Bool) -> Void, onCommit: @escaping () -> Void)` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed TextField.init(_:value:formatter:onEditingChanged:). Use View.onSubmit(of:_:) for functionality previously provided by the onCommit parameter. Use FocusState<T> and View.focused(_:equals:) for functionality previously provided by the onEditingChanged parameter.
- `TextField.init<V>(_ titleKey: LocalizedStringKey, value: Binding<V>, formatter: Formatter, onEditingChanged: @escaping (Bool) -> Void)` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed TextField.init(_:value:formatter:onEditingChanged:). Use View.onSubmit(of:_:) for functionality previously provided by the onCommit parameter. Use FocusState<T> and View.focused(_:equals:) for functionality previously provided by the onEditingChanged parameter.
- `TextField.init<V>(_ titleKey: LocalizedStringKey, value: Binding<V>, formatter: Formatter, onCommit: @escaping () -> Void)` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed TextField.init(_:value:formatter:onEditingChanged:). Use View.onSubmit(of:_:) for functionality previously provided by the onCommit parameter. Use FocusState<T> and View.focused(_:equals:) for functionality previously provided by the onEditingChanged parameter.
- `TextField.init<S, V>(_ title: S, value: Binding<V>, formatter: Formatter, onEditingChanged: @escaping (Bool) -> Void, onCommit: @escaping () -> Void) where S : StringProtocol` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed TextField.init(_:value:formatter:onEditingChanged:). Use View.onSubmit(of:_:) for functionality previously provided by the onCommit parameter. Use FocusState<T> and View.focused(_:equals:) for functionality previously provided by the onEditingChanged parameter.
- `TextField.init<S, V>(_ title: S, value: Binding<V>, formatter: Formatter, onEditingChanged: @escaping (Bool) -> Void) where S : StringProtocol` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed TextField.init(_:value:formatter:onEditingChanged:). Use View.onSubmit(of:_:) for functionality previously provided by the onCommit parameter. Use FocusState<T> and View.focused(_:equals:) for functionality previously provided by the onEditingChanged parameter.
- `TextField.init<S, V>(_ title: S, value: Binding<V>, formatter: Formatter, onCommit: @escaping () -> Void) where S : StringProtocol` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed TextField.init(_:value:formatter:onEditingChanged:). Use View.onSubmit(of:_:) for functionality previously provided by the onCommit parameter. Use FocusState<T> and View.focused(_:equals:) for functionality previously provided by the onEditingChanged parameter.
- `MenuButton.init(_ titleKey: LocalizedStringKey, @ContentBuilder content: () -> Content)` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use `Menu` instead.
- `TabView.init(selection: Binding<SelectionValue>?, @ContentBuilder content: () -> Content)` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use TabContentBuilder-based TabView initializers instead
- `Slider.init<V>(value: Binding<V>, in bounds: ClosedRange<V> = 0...1, onEditingChanged: @escaping (Bool) -> Void = { _ in }, minimumValueLabel: ValueLabel, maximumValueLabel: ValueLabel, @ContentBuilder label: () -> Label) where V : BinaryFloatingPoint, V.Stride : BinaryFloatingPoint` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to Slider(value:in:label:minimumValueLabel:maximumValueLabel:onEditingChanged:)
- `Slider.init<V>(value: Binding<V>, in bounds: ClosedRange<V>, step: V.Stride = 1, onEditingChanged: @escaping (Bool) -> Void = { _ in }, minimumValueLabel: ValueLabel, maximumValueLabel: ValueLabel, @ContentBuilder label: () -> Label) where V : BinaryFloatingPoint, V.Stride : BinaryFloatingPoint` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to Slider(value:in:step:label:minimumValueLabel:maximumValueLabel:onEditingChanged:)
- `Slider.init<V>(value: Binding<V>, in bounds: ClosedRange<V> = 0...1, onEditingChanged: @escaping (Bool) -> Void = { _ in }, @ContentBuilder label: () -> Label) where V : BinaryFloatingPoint, V.Stride : BinaryFloatingPoint` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to Slider(value:in:label:onEditingChanged:)
- `Slider.init<V>(value: Binding<V>, in bounds: ClosedRange<V>, step: V.Stride = 1, onEditingChanged: @escaping (Bool) -> Void = { _ in }, @ContentBuilder label: () -> Label) where V : BinaryFloatingPoint, V.Stride : BinaryFloatingPoint` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to Slider(value:in:step:label:onEditingChanged:)
- `LinearProgressViewStyle.init(tint: Color)` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use ``View/tint(_)`` instead.
- `CircularProgressViewStyle.init(tint: Color)` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use ``View/tint(_)`` instead.
- `TextField.init(_ titleKey: LocalizedStringKey, text: Binding<String>, onEditingChanged: @escaping (Bool) -> Void, onCommit: @escaping () -> Void)` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed TextField.init(_:text:onEditingChanged:). Use View.onSubmit(of:_:) for functionality previously provided by the onCommit parameter. Use FocusState<T> and View.focused(_:equals:) for functionality previously provided by the onEditingChanged parameter.
- `TextField.init(_ titleKey: LocalizedStringKey, text: Binding<String>, onEditingChanged: @escaping (Bool) -> Void)` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed TextField.init(_:text:onEditingChanged:). Use View.onSubmit(of:_:) for functionality previously provided by the onCommit parameter. Use FocusState<T> and View.focused(_:equals:) for functionality previously provided by the onEditingChanged parameter.
- `TextField.init(_ titleKey: LocalizedStringKey, text: Binding<String>, onCommit: @escaping () -> Void)` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed TextField.init(_:text:onEditingChanged:). Use View.onSubmit(of:_:) for functionality previously provided by the onCommit parameter. Use FocusState<T> and View.focused(_:equals:) for functionality previously provided by the onEditingChanged parameter.
- `TextField.init<S>(_ title: S, text: Binding<String>, onEditingChanged: @escaping (Bool) -> Void, onCommit: @escaping () -> Void) where S : StringProtocol` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed TextField.init(_:text:onEditingChanged:). Use View.onSubmit(of:_:) for functionality previously provided by the onCommit parameter. Use FocusState<T> and View.focused(_:equals:) for functionality previously provided by the onEditingChanged parameter.
- `TextField.init<S>(_ title: S, text: Binding<String>, onEditingChanged: @escaping (Bool) -> Void) where S : StringProtocol` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed TextField.init(_:text:onEditingChanged:). Use View.onSubmit(of:_:) for functionality previously provided by the onCommit parameter. Use FocusState<T> and View.focused(_:equals:) for functionality previously provided by the onEditingChanged parameter.
- `TextField.init<S>(_ title: S, text: Binding<String>, onCommit: @escaping () -> Void) where S : StringProtocol` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed TextField.init(_:text:onEditingChanged:). Use View.onSubmit(of:_:) for functionality previously provided by the onCommit parameter. Use FocusState<T> and View.focused(_:equals:) for functionality previously provided by the onEditingChanged parameter.
- `InsetListStyle.init(alternatesRowBackgrounds: Bool)` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use the `.inset` style with the `.alternatingRowBackgrounds()` view modifier
- `ToolbarItem.init(id: String, placement: ToolbarItemPlacement = .automatic, showsByDefault: Bool, @ContentBuilder content: () -> Content)` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use the CustomizableToolbarContent/defaultCustomization(_:options) modifier with a value of .hidden
- `Section.init(header: Parent, footer: Footer, @ContentBuilder content: () -> Content)` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to Section(content:header:footer:)
- `Section.init(footer: Footer, @ContentBuilder content: () -> Content)` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to Section(content:footer:)
- `Section.init(header: Parent, @ContentBuilder content: () -> Content)` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to Section(content:header:)
- `GroupBox.init(label: Label, @ContentBuilder content: () -> Content)` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to GroupBox(content:label:)
- `InsetTableStyle.init(alternatesRowBackgrounds: Bool)` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use the `.inset` style with the `.alternatingRowBackgrounds()` view modifier
- `Picker.init(selection: Binding<SelectionValue>, label: Label, @ContentBuilder content: () -> Content)` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to Picker(selection:content:label:)
- `ScrollView.init(_ axes: Set = .vertical, showsIndicators: Bool = true, @ContentBuilder content: () -> Content)` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use the ScrollView(_:content:) initializer and the scrollIndicators(:_) modifier
- `NavigationLink.init(destination: Destination, @ContentBuilder label: () -> Label)` (iOS, macOS, tvOS, watchOS, visionOS)
  - Pass a closure as the destination
- `NavigationLink.init(_ titleKey: LocalizedStringKey, destination: Destination)` (iOS, macOS, tvOS, watchOS, visionOS)
  - Pass a closure as the destination
- `NavigationLink.init<S>(_ title: S, destination: Destination) where S : StringProtocol` (iOS, macOS, tvOS, watchOS, visionOS)
  - Pass a closure as the destination
- `NavigationLink.init(destinationName: String, @ContentBuilder label: () -> Label)` (iOS, macOS, tvOS, watchOS, visionOS)
  - use NavigationLink(value:label:)
- `NavigationLink.init(destinationName: String, isActive: Binding<Bool>, @ContentBuilder label: () -> Label)` (iOS, macOS, tvOS, watchOS, visionOS)
  - use NavigationLink(value:label:)
- `NavigationLink.init<V>(destinationName: String, tag: V, selection: Binding<V?>, @ContentBuilder label: () -> Label) where V : Hashable` (iOS, macOS, tvOS, watchOS, visionOS)
  - use NavigationLink(value:label:)
- `SecureField.init(_ titleKey: LocalizedStringKey, text: Binding<String>, onCommit: @escaping () -> Void)` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed SecureField.init(_:text:). Use View.onSubmit(of:_:) for functionality previously provided by the onCommit parameter.
- `SecureField.init<S>(_ title: S, text: Binding<String>, onCommit: @escaping () -> Void) where S : StringProtocol` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed SecureField.init(_:text:). Use View.onSubmit(of:_:) for functionality previously provided by the onCommit parameter.
- `BorderedButtonStyle.init(tint: Color)` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use ``View/tint(_)`` instead.
- `Color.init(_ color: UIColor)` (iOS, tvOS, watchOS, visionOS)
  - Use Color(uiColor:) when converting a UIColor, or create a standard Color directly
- `BorderedListStyle.init(alternatesRowBackgrounds: Bool)` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use the `.bordered` style with the `.alternatingRowBackgrounds()` view modifier
- `Stepper.init(onIncrement: (() -> Void)?, onDecrement: (() -> Void)?, onEditingChanged: @escaping (Bool) -> Void = { _ in }, @ContentBuilder label: () -> Label)` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to Stepper(label:onIncrement:onDecrement:onEditingChanged:)
- `Stepper.init<V>(value: Binding<V>, step: V.Stride = 1, onEditingChanged: @escaping (Bool) -> Void = { _ in }, @ContentBuilder label: () -> Label) where V : Strideable` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to Stepper(value:step:label:onEditingChanged:)
- `Stepper.init<V>(value: Binding<V>, in bounds: ClosedRange<V>, step: V.Stride = 1, onEditingChanged: @escaping (Bool) -> Void = { _ in }, @ContentBuilder label: () -> Label) where V : Strideable` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to Stepper(value:in:step:label:onEditingChanged:)
- `LinearGaugeStyle.init(tint: Color)` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use ``View/tint(_)`` instead.
- `LinearGaugeStyle.init(tint: Gradient)` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use ``View/tint(_)`` instead.
- `BorderedTableStyle.init(alternatesRowBackgrounds: Bool)` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use the `.bordered` style with the `.alternatingRowBackgrounds()` view modifier
- `PasteButton.init<Payload>(supportedContentTypes: [UTType], validator: @escaping ([NSItemProvider]) -> Payload?, payloadAction: @escaping (Payload) -> Void)` (iOS, macOS, tvOS, watchOS, visionOS)
- `PasteButton.init(supportedTypes: [String], payloadAction: @escaping ([NSItemProvider]) -> Void)` (iOS, macOS, tvOS, watchOS, visionOS)
  - Provide `UTType`s as the `supportedContentTypes` instead.
- `SpatialTapGesture.init(count: Int = 1, coordinateSpace: CoordinateSpace = .local)` (iOS, macOS, tvOS, watchOS, visionOS)
  - use overload that accepts a CoordinateSpaceProtocol instead
- `SwitchToggleStyle.init(tint: Color)` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use ``View/tint(_)`` instead.
- `Color.init(_ cgColor: CGColor)` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use Color(cgColor:) when converting a CGColor, or create a standard Color directly
- `Color.init(_ color: NSColor)` (macOS)
  - Use Color(nsColor:) when converting a NSColor, or create a standard Color directly

### Functions and Methods

- `View.accessibility(value: Text) -> ModifiedContent<Self, AccessibilityAttachmentModifier>` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to accessibilityValue(_:)
- `ModifiedContent.accessibility(value: Text) -> ModifiedContent<Content, Modifier>` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to accessibilityValue(_:)
- `View.actionSheet<T>(item: Binding<T?>, content: (T) -> ActionSheet) -> some View where T : Identifiable` (iOS, macOS, tvOS, watchOS, visionOS)
  - use `confirmationDialog(title:isPresented:titleVisibility:presenting::actions:)`instead.
- `View.actionSheet(isPresented: Binding<Bool>, content: () -> ActionSheet) -> some View` (iOS, macOS, tvOS, watchOS, visionOS)
  - use `confirmationDialog(title:isPresented:titleVisibility:presenting::actions:)`instead.
- `View.alert<Item>(item: Binding<Item?>, content: (Item) -> Alert) -> some View where Item : Identifiable` (iOS, macOS, tvOS, watchOS, visionOS)
  - use `alert(title:isPresented:presenting::actions:) instead.
- `View.alert(isPresented: Binding<Bool>, content: () -> Alert) -> some View` (iOS, macOS, tvOS, watchOS, visionOS)
  - use `alert(title:isPresented:presenting::actions:) instead.
- `View.onContinuousHover(coordinateSpace: CoordinateSpace = .local, perform action: @escaping (HoverPhase) -> Void) -> some View` (iOS, macOS, tvOS, watchOS, visionOS)
  - use overload that accepts a CoordinateSpaceProtocol instead
- `View.listRowPlatterColor(_ color: Color?) -> some View` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to listItemTint(_:)
- `View.dropDestination<T>(for payloadType: T.Type = T.self, action: @escaping (_ items: [T], _ location: CGPoint) -> Bool, isTargeted: @escaping (Bool) -> Void = { _ in }) -> some View where T : Transferable` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use `dropDestination(for:isEnabled:action:)` with an `action` that takes a `DropSession` parameter instead.
- `DropInfo.hasItemsConforming(to types: [String]) -> Bool` (iOS, macOS, tvOS, watchOS, visionOS)
  - Provide `UTType`s as the `types` instead.
- `View.statusBarHidden(_ hidden: Bool = true) -> some View` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use .toolbarVisibility(_, for: .statusBar) instead
- `View.statusBar(hidden: Bool) -> some View` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to statusBarHidden(_:)
- `View.autocapitalization(_ style: UITextAutocapitalizationType) -> some View` (iOS, tvOS, visionOS)
  - use textInputAutocapitalization(_:)
- `ListStyle.static inset(alternatesRowBackgrounds: Bool) -> InsetListStyle` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use the `.inset` style with the `.alternatingRowBackgrounds()` view modifier
- `View.navigationBarItems<L, T>(leading: L, trailing: T) -> some View where L : View, T : View` (iOS, macOS, tvOS, visionOS)
  - Use toolbar(_:) with navigationBarLeading or navigationBarTrailing placement
- `View.navigationBarItems<L>(leading: L) -> some View where L : View` (iOS, macOS, tvOS, visionOS)
  - Use toolbar(_:) with navigationBarLeading or navigationBarTrailing placement
- `View.navigationBarItems<T>(trailing: T) -> some View where T : View` (iOS, macOS, tvOS, visionOS)
  - Use toolbar(_:) with navigationBarLeading or navigationBarTrailing placement
- `View.accessibility(hidden: Bool) -> ModifiedContent<Self, AccessibilityAttachmentModifier>` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to accessibilityHidden(_:)
- `View.accessibility(label: Text) -> ModifiedContent<Self, AccessibilityAttachmentModifier>` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to accessibilityLabel(_:)
- `View.accessibility(hint: Text) -> ModifiedContent<Self, AccessibilityAttachmentModifier>` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to accessibilityHint(_:)
- `View.accessibility(inputLabels: [Text]) -> ModifiedContent<Self, AccessibilityAttachmentModifier>` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to accessibilityInputLabels(_:)
- `View.accessibility(identifier: String) -> ModifiedContent<Self, AccessibilityAttachmentModifier>` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to accessibilityIdentifier(_:)
- `View.accessibility(sortPriority: Double) -> ModifiedContent<Self, AccessibilityAttachmentModifier>` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to accessibilitySortPriority(_:)
- `View.accessibility(activationPoint: CGPoint) -> ModifiedContent<Self, AccessibilityAttachmentModifier>` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to accessibilityActivationPoint(_:)
- `View.accessibility(activationPoint: UnitPoint) -> ModifiedContent<Self, AccessibilityAttachmentModifier>` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to accessibilityActivationPoint(_:)
- `ModifiedContent.accessibility(hidden: Bool) -> ModifiedContent<Content, Modifier>` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to accessibilityHidden(_:)
- `ModifiedContent.accessibility(label: Text) -> ModifiedContent<Content, Modifier>` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to accessibilityLabel(_:)
- `ModifiedContent.accessibility(hint: Text) -> ModifiedContent<Content, Modifier>` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to accessibilityHint(_:)
- `ModifiedContent.accessibility(inputLabels: [Text]) -> ModifiedContent<Content, Modifier>` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to accessibilityInputLabels(_:)
- `ModifiedContent.accessibility(identifier: String) -> ModifiedContent<Content, Modifier>` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to accessibilityIdentifier(_:)
- `ModifiedContent.accessibility(sortPriority: Double) -> ModifiedContent<Content, Modifier>` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to accessibilitySortPriority(_:)
- `ModifiedContent.accessibility(activationPoint: CGPoint) -> ModifiedContent<Content, Modifier>` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to accessibilityActivationPoint(_:)
- `ModifiedContent.accessibility(activationPoint: UnitPoint) -> ModifiedContent<Content, Modifier>` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to accessibilityActivationPoint(_:)
- `View.navigationBarHidden(_ hidden: Bool) -> some View` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use toolbar(.hidden)
- `View.navigationBarTitle(_ title: Text) -> some View` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to navigationTitle(_:)
- `View.navigationBarTitle(_ titleKey: LocalizedStringKey) -> some View` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to navigationTitle(_:)
- `View.navigationBarTitle<S>(_ title: S) -> some View where S : StringProtocol` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to navigationTitle(_:)
- `View.navigationBarTitle(_ title: Text, displayMode: TitleDisplayMode) -> some View` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use navigationTitle(_:) with navigationBarTitleDisplayMode(_:)
- `View.navigationBarTitle(_ titleKey: LocalizedStringKey, displayMode: TitleDisplayMode) -> some View` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use navigationTitle(_:) with navigationBarTitleDisplayMode(_:)
- `View.navigationBarTitle<S>(_ title: S, displayMode: TitleDisplayMode) -> some View where S : StringProtocol` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use navigationTitle(_:) with navigationBarTitleDisplayMode(_:)
- `View.navigationViewStyle<S>(_ style: S) -> some View where S : NavigationViewStyle` (iOS, macOS, tvOS, watchOS, visionOS)
  - replace styled NavigationView with NavigationStack or NavigationSplitView instead
- `View.contextMenu<MenuItems>(_ contextMenu: ContextMenu<MenuItems>?) -> some View where MenuItems : View` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use `contextMenu(menuItems:)` instead.
- `DynamicViewContent.onInsert(of acceptedTypeIdentifiers: [String], perform action: @escaping (Int, [NSItemProvider]) -> Void) -> some DynamicViewContent` (iOS, macOS, tvOS, watchOS, visionOS)
  - Provide `UTType`s as the `supportedContentTypes` instead.
- `View.toolbarBackground(_ visibility: Visibility, for bars: ToolbarPlacement...) -> some View` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to toolbarBackgroundVisibility(_:for:)
- `View.toolbar(_ visibility: Visibility, for bars: ToolbarPlacement...) -> some View` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to toolbarVisibility(_:for:)
- `View.onPasteCommand(of supportedTypes: [String], perform payloadAction: @escaping ([NSItemProvider]) -> Void) -> some View` (iOS, macOS, tvOS, watchOS, visionOS)
  - Provide `UTType`s as the `supportedContentTypes` instead.
- `View.searchable<S>(text: Binding<String>, placement: SearchFieldPlacement = .automatic, prompt: Text? = nil, @ContentBuilder suggestions: () -> S) -> some View where S : View` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use the searchable modifier with the searchSuggestions modifier
- `View.searchable<S>(text: Binding<String>, placement: SearchFieldPlacement = .automatic, prompt: LocalizedStringKey, @ContentBuilder suggestions: () -> S) -> some View where S : View` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use the searchable modifier with the searchSuggestions modifier
- `View.searchable<V, S>(text: Binding<String>, placement: SearchFieldPlacement = .automatic, prompt: S, @ContentBuilder suggestions: () -> V) -> some View where V : View, S : StringProtocol` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use the searchable modifier with the searchSuggestions modifier
- `View.tabItem<V>(@ContentBuilder _ label: () -> V) -> some View where V : View` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use `Tab(title:image:value:content:)` and related initializers instead
- `View.coordinateSpace<T>(name: T) -> some View where T : Hashable` (iOS, macOS, tvOS, watchOS, visionOS)
  - use coordinateSpace(_:) instead
- `View.onLongPressGesture(minimumDuration: Double = 0.5, maximumDistance: CGFloat = 10, pressing: ((Bool) -> Void)? = nil, perform action: @escaping () -> Void) -> some View` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to onLongPressGesture(minimumDuration:maximumDuration:perform:onPressingChanged:)
- `View.onLongPressGesture(minimumDuration: Double = 0.5, pressing: ((Bool) -> Void)? = nil, perform action: @escaping () -> Void) -> some View` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to onLongPressGesture(minimumDuration:perform:onPressingChanged:)
- `ListStyle.static bordered(alternatesRowBackgrounds: Bool) -> BorderedListStyle` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use the `.bordered` style with the `.alternatingRowBackgrounds()` view modifier
- `TabViewCustomization.resetSectionOrder(for sectionID: String)` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use the `section` subscript and call `resetTabOrder` instead.
- `View.disableAutocorrection(_ disable: Bool?) -> some View` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to autocorrectionDisabled(_:)
- `View.menuButtonStyle<S>(_ style: S) -> some View where S : MenuButtonStyle` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use `menuStyle(_:)` instead.
- `View.accessibility(addTraits traits: AccessibilityTraits) -> ModifiedContent<Self, AccessibilityAttachmentModifier>` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to accessibilityAddTraits(_:)
- `View.accessibility(removeTraits traits: AccessibilityTraits) -> ModifiedContent<Self, AccessibilityAttachmentModifier>` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to accessibilityRemoveTraits(_:)
- `ModifiedContent.accessibility(addTraits traits: AccessibilityTraits) -> ModifiedContent<Content, Modifier>` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to accessibilityAddTraits(_:)
- `ModifiedContent.accessibility(removeTraits traits: AccessibilityTraits) -> ModifiedContent<Content, Modifier>` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to accessibilityRemoveTraits(_:)
- `View.onTapGesture(count: Int = 1, coordinateSpace: CoordinateSpace = .local, perform action: @escaping (CGPoint) -> Void) -> some View` (iOS, macOS, tvOS, watchOS, visionOS)
  - use overload that accepts a CoordinateSpaceProtocol instead
- `View.foregroundColor(_ color: Color?) -> some View` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to foregroundStyle(_:)
- `View.accentColor(_ accentColor: Color?) -> some View` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use the asset catalog's accent color or View.tint(_:) instead.
- `View.overlay<Overlay>(_ overlay: Overlay, alignment: Alignment = .center) -> some View where Overlay : View` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use `overlay(alignment:content:)` instead.
- `View.mask<Mask>(_ mask: Mask) -> some View where Mask : View` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use overload where mask accepts a @ContentBuilder instead.
- `GeometryProxy.frame(in coordinateSpace: CoordinateSpace) -> CGRect` (iOS, macOS, tvOS, watchOS, visionOS)
  - use overload that accepts a CoordinateSpaceProtocol instead
- `Font.static system(_ style: TextStyle, design: Design = .default) -> Font` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use `system(_:design:weight:)` instead.
- `Text.foregroundColor(_ color: Color?) -> Text` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to foregroundStyle(_:)
- `View.background<Background>(_ background: Background, alignment: Alignment = .center) -> some View where Background : View` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use `background(alignment:content:)` instead.
- `View.edgesIgnoringSafeArea(_ edges: Set) -> some View` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use ignoresSafeArea(_:edges:) instead.
- `View.cornerRadius(_ radius: CGFloat, antialiased: Bool = true) -> some View` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use `clipShape` or `fill` instead.
- `Font.static system(size: CGFloat, weight: Weight = .regular, design: Design = .default) -> Font` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use `system(size:weight:design:)` instead.
- `View.colorScheme(_ colorScheme: ColorScheme) -> some View` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to preferredColorScheme(_:)
- `Section.collapsible(_ collapsible: Bool) -> some View` (macOS, tvOS, watchOS)
  - Use a standard Section initializer which does not allow for collapsibility\nby default after macOS 14.0.

### Properties

- `NavigationViewStyle.static columns: ColumnNavigationViewStyle` (iOS, macOS, tvOS, watchOS, visionOS)
  - replace styled NavigationView with NavigationSplitView
- `ToolbarItemPlacement.static navigationBarLeading: ToolbarItemPlacement` (iOS, macOS, tvOS, watchOS, visionOS)
  - use topBarLeading instead
- `ToolbarItemPlacement.static navigationBarTrailing: ToolbarItemPlacement` (iOS, macOS, tvOS, watchOS, visionOS)
  - use topBarTrailing instead
- `EnvironmentValues.presentationMode: Binding<PresentationMode>` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use isPresented or dismiss
- `NavigationViewStyle.static automatic: DefaultNavigationViewStyle` (iOS, macOS, tvOS, watchOS, visionOS)
  - replace styled NavigationView with NavigationStack or NavigationSplitView instead
- `MenuStyle.static borderlessButton: BorderlessButtonMenuStyle` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use .menuStyle(.button) and .buttonStyle(.borderless).
- `EnvironmentValues.disableAutocorrection: Bool?` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to autocorrectionDisabled
- `NavigationViewStyle.static stack: StackNavigationViewStyle` (iOS, macOS, tvOS, watchOS, visionOS)
  - replace stack-styled NavigationView with NavigationStack
- `EnvironmentValues.sizeCategory: ContentSizeCategory` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to dynamicTypeSize
- `Color.cgColor: CGColor?` (iOS, macOS, tvOS, watchOS, visionOS)
  - Renamed to resolve(in:)
- `EnvironmentValues.controlActiveState: ControlActiveState` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use `EnvironmentValues.appearsActive` instead.
- `SurroundingsEffect.static systemDark: SurroundingsEffect` (macOS, visionOS)
  - Renamed to dark

### Subscripts

- `TabViewCustomization.subscript(sectionID id: String) -> [String]?` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use the `section` subscript and read `tabOrder` instead.
- `TabViewCustomization.subscript(sidebarVisibility id: String) -> Visibility` (iOS, macOS, tvOS, watchOS, visionOS)
  - Use the `tab` subscript and read `sidebarVisibility` instead.

---

## Hard-Deprecated / Modern API Replacements (iOS 15-26+)

This section is a different category from soft-deprecation above: these are transitions Apple
has clearly established as the modern path — either via a real (non-placeholder) deprecation or
via a strongly recommended successor pattern (e.g. `ObservableObject` → `@Observable`). It's
organized by the minimum deployment target that unlocks each replacement, since the right
choice depends on what you're shipping to.

### Always Use (iOS 15+)

These APIs have been deprecated long enough that there is no reason to use the old variants.

**Compact replacements** — minimal API shape changes, near-direct swaps (a few need an extra
parameter or small structural adjustment):

- **`navigationTitle(_:)`** instead of `navigationBarTitle(_:)`
- **`toolbar { ToolbarItem(...) }`** instead of `navigationBarItems(...)` (structural change)
- **`toolbarVisibility(.hidden, for: .navigationBar)`** instead of `navigationBarHidden(_:)`
- **`statusBarHidden(_:)`** instead of `statusBar(hidden:)`
- **`ignoresSafeArea(_:edges:)`** instead of `edgesIgnoringSafeArea(_:)`
- **`preferredColorScheme(_:)`** instead of `colorScheme(_:)`
- **`foregroundStyle(_:)`** instead of `foregroundColor(_:)` (e.g., `.foregroundStyle(.primary)`)
- **`clipShape(.rect(cornerRadius:))`** instead of `cornerRadius()`
- **`textInputAutocapitalization(_:)`** instead of `autocapitalization(_:)` (note: `.never` replaces `.none`)
- **`animation(_:value:)`** instead of `animation(_:)` (adds required `value:` parameter; back-deploys to iOS 13+)

**Lists and forms** — use trailing-closure `Section` initializers instead of the positional
header/footer `View` initializers. The single-title form is still current, not deprecated:

```swift
// Current - single-title LocalizedStringKey initializer
Section("Settings") {
    Toggle("Notifications", isOn: .constant(true))
}

// Replacement - content/header/footer trailing-closure initializer
Section {
    Toggle("Notifications", isOn: .constant(true))
} header: {
    Text("Settings")
} footer: {
    Text("Changes apply immediately.")
}

// Deprecated/renamed - positional header/footer View arguments
Section(header: Text("Settings"), footer: Text("Changes apply immediately.")) {
    Toggle("Notifications", isOn: .constant(true))
}

Section(header: Text("Settings")) {
    Toggle("Notifications", isOn: .constant(true))
}

Section(footer: Text("Changes apply immediately.")) {
    Toggle("Notifications", isOn: .constant(true))
}
```

**Presentation** — always use `.confirmationDialog(_:isPresented:actions:message:)` instead of
`actionSheet(...)`, and `.alert(_:isPresented:actions:message:)` instead of
`alert(isPresented:content:)`. Both take a title `String`, `isPresented: Binding<Bool>`, an
`actions` builder with `Button` items (supporting `role: .destructive` / `.cancel`), and an
optional `message` builder:

```swift
.alert("Delete Item?", isPresented: $showAlert) {
    Button("Delete", role: .destructive) { deleteItem() }
    Button("Cancel", role: .cancel) { }
} message: {
    Text("This action cannot be undone.")
}
```

**Text input** — always use `onSubmit(of:_:)` and `focused(_:equals:)` instead of `TextField`
`onEditingChanged`/`onCommit` callbacks:

```swift
@FocusState private var isFocused: Bool

TextField("Search", text: $query)
    .focused($isFocused)
    .onSubmit { performSearch() }
```

**Accessibility** — always use dedicated accessibility modifiers instead of the generic
`accessibility(...)` variants: `.accessibilityLabel()`, `.accessibilityValue()`,
`.accessibilityHint()`, `.accessibilityAddTraits()`, `.accessibilityHidden()` instead of
`.accessibility(label:)`, `.accessibility(value:)`, etc. (Full VoiceOver/Dynamic
Type/contrast auditing is out of scope here — see the `ios-accessibility` skill for that.)

**Custom environment/container values** — always use the `@Entry` macro instead of manual
`EnvironmentKey` conformance. `@Entry` was introduced in Xcode 16 and back-deploys to all OS
versions:

```swift
// Modern — one line replaces ~10 lines of EnvironmentKey boilerplate
extension EnvironmentValues {
    @Entry var myCustomValue: String = "Default value"
}
```

**Styling** — always use `Button` instead of `onTapGesture()` unless you need tap location or
count:

```swift
Button("Tap me") { performAction() }

// Use onTapGesture only when you need location or count
Image("photo")
    .onTapGesture(count: 2) { handleDoubleTap() }
```

### When Targeting iOS 16+

**Navigation** — use `NavigationStack` (or `NavigationSplitView`) instead of `NavigationView`.
Value-based `NavigationLink(value:)` with `.navigationDestination(for:)` replaces
destination-based links:

```swift
NavigationStack {
    List(items) { item in
        NavigationLink(value: item) { Text(item.name) }
    }
    .navigationDestination(for: Item.self) { DetailView(item: $0) }
}
```

**Simple renames** — `tint(_:)` instead of `accentColor(_:)`; `autocorrectionDisabled(_:)`
instead of `disableAutocorrection(_:)`.

**Clipboard** — prefer `PasteButton` for user-initiated paste UI to avoid paste prompts; it
handles permissions automatically. Use `UIPasteboard` only when you need programmatic or
non-`Transferable` clipboard access (which triggers the paste permission prompt):

```swift
PasteButton(payloadType: String.self) { strings in
    pastedText = strings.first ?? ""
}
```

### When Targeting iOS 17+

**State management** — prefer `@Observable` over `ObservableObject` for new code. Use `@State`
instead of `@StateObject`; use `@Bindable` instead of `@ObservedObject` (see this skill's
state-management/data-flow reference for the full `@Observable` migration mapping).

**Events** — use `onChange(of:initial:_:)` or `onChange(of:) { }` instead of
`onChange(of:perform:)`. The deprecated variant passes only the new value; the modern variants
provide either both old and new values, or a no-parameter closure:

- **No-parameter** (most common): `.onChange(of: value) { doSomething() }`
- **Old and new values**: `.onChange(of: value) { old, new in ... }`
- **With initial trigger**: `.onChange(of: value, initial: true) { ... }`
- **Deprecated**: `.onChange(of: value) { newValue in ... }` — single-parameter closure

**Sensory feedback** — prefer `sensoryFeedback(_:trigger:)` and related overloads instead of
`UIImpactFeedbackGenerator`, `UISelectionFeedbackGenerator`, and
`UINotificationFeedbackGenerator` in SwiftUI views. Attach haptics declaratively to the view
that owns the state change, rather than imperatively firing UIKit generators inside button
actions:

```swift
@State private var isFavorite = false

Button("Favorite", systemImage: isFavorite ? "heart.fill" : "heart") {
    isFavorite.toggle()
}
.sensoryFeedback(.selection, trigger: isFavorite)
```

Use the conditional overload when feedback should fire only for specific transitions:

```swift
.sensoryFeedback(.selection, trigger: phase) { old, new in
    old == .inactive || new == .expanded
}
```

**Gestures** — `MagnifyGesture` instead of `MagnificationGesture` (access magnitude via
`value.magnification`); `RotateGesture` instead of `RotationGesture` (access angle via
`value.rotation`).

**Layout** — consider `containerRelativeFrame()` or `visualEffect()` as alternatives to
`GeometryReader` for sizing and position-based effects. `GeometryReader` is not deprecated and
remains necessary for many measurement-based layouts:

```swift
Image("hero")
    .resizable()
    .containerRelativeFrame(.horizontal) { length, axis in length * 0.8 }
```

- **`visualEffect { content, geometry in ... }`** — position-based effects (parallax, offsets) without a `GeometryReader` wrapper.
- **`onGeometryChange(for:of:action:)`** — react to geometry changes of a specific view; useful for driving state/effects. `GeometryReader` is still better when layout itself depends on geometry. Note the two-closure shape:
  ```swift
  .onGeometryChange(for: CGFloat.self) { proxy in proxy.size.height } action: { newHeight in height = newHeight }
  ```
- **`.coordinateSpace(.named("scroll"))`** instead of `.coordinateSpace(name: "scroll")`.

### When Targeting iOS 18+

**Tabs** — use the `Tab` API instead of `tabItem(_:)`:

```swift
TabView {
    Tab("Home", systemImage: "house") { HomeView() }
    Tab("Search", systemImage: "magnifyingglass") { SearchView() }
    Tab("Profile", systemImage: "person") { ProfileView() }
}
```

When using `Tab(role:)`, all tabs must use the `Tab` syntax — mixing `Tab(role:)` with
`.tabItem()` causes compilation errors.

**Previews** — use `@Previewable` for dynamic properties in previews:

```swift
// Modern (iOS 18+)
#Preview {
    @Previewable @State var isOn = false
    Toggle("Setting", isOn: $isOn)
}
```

### When Targeting iOS 26+

For Liquid Glass APIs (`glassEffect`, `GlassEffectContainer`, glass button styles), see this
skill's design/structure reference if it covers Liquid Glass, or Apple's "Build a SwiftUI app
with the new design" (WWDC25, session 323).

**Scroll edge effects** — use `scrollEdgeEffectStyle(_:for:)` to configure scroll edge behavior:

```swift
ScrollView {
    // content
}
.scrollEdgeEffectStyle(.soft, for: .top)
```

**Background extension** — use `backgroundExtensionEffect()` for edge-extending blurred
backgrounds. Views behind a Liquid Glass sidebar can appear clipped; this modifier mirrors and
blurs content outside the safe area so artwork remains visible:

```swift
Image("hero")
    .backgroundExtensionEffect()
```

> Source: "Build a SwiftUI app with the new design" (WWDC25, session 323)

**Tab bar** — use `tabBarMinimizeBehavior(_:)` to control tab bar minimization on scroll:

```swift
TabView {
    // tabs
}
.tabBarMinimizeBehavior(.onScrollDown)
```

Use `tabViewBottomAccessory` for persistent controls above the tab bar. Read
`tabViewBottomAccessoryPlacement` from the environment to adapt content when the accessory
collapses into the tab bar area:

```swift
TabView {
    // tabs
}
.tabViewBottomAccessory {
    NowPlayingBar()
}
```

Use `Tab(role: .search)` for a dedicated search tab — it separates from the rest and morphs into
a search field when selected:

```swift
TabView {
    Tab("Home", systemImage: "house") { HomeView() }
    Tab("Profile", systemImage: "person") { ProfileView() }
    Tab(role: .search) { SearchResultsView() }
}
```

> Source: "What's new in SwiftUI" (WWDC25, session 256) and "Build a SwiftUI app with the new design" (WWDC25, session 323)

**Toolbars** — use `ToolbarSpacer` to control grouping of toolbar items: fixed spacers visually
separate related groups, flexible spacers push items apart.

```swift
.toolbar {
    ToolbarItem(placement: .topBarTrailing) {
        Button("Up", systemImage: "chevron.up") { }
    }
    ToolbarItem(placement: .topBarTrailing) {
        Button("Down", systemImage: "chevron.down") { }
    }
    ToolbarSpacer(.fixed)
    ToolbarItem(placement: .topBarTrailing) {
        Button("Settings", systemImage: "gear") { }
    }
}
```

Use `sharedBackgroundVisibility(.hidden)` to remove the glass group background from an
individual toolbar item, and `badge(_:)` on toolbar item content to display an indicator:

```swift
ToolbarItem(placement: .topBarTrailing) {
    Image(systemName: "person.circle.fill")
        .sharedBackgroundVisibility(.hidden)
}

ToolbarItem(placement: .topBarTrailing) {
    Button("Notifications", systemImage: "bell") { }
        .badge(unreadCount)
}
```

> Source: "Build a SwiftUI app with the new design" (WWDC25, session 323)

**Search** — use `searchToolbarBehavior(.minimizable)` to opt into a minimized search button.
The system may automatically minimize search into a toolbar button depending on available
space; this modifier explicitly opts in:

```swift
NavigationStack {
    ContentView()
        .searchable(text: $query)
        .searchToolbarBehavior(.minimizable)
}
```

> Source: "Build a SwiftUI app with the new design" (WWDC25, session 323)

**Animations** — use the `@Animatable` macro instead of manual `animatableData` declarations.
The macro auto-synthesizes `animatableData` from all animatable properties; use
`@AnimatableIgnored` to exclude specific properties:

```swift
@Animatable
struct Wedge: Shape {
    var startAngle: Angle
    var endAngle: Angle
    @AnimatableIgnored var drawClockwise: Bool

    func path(in rect: CGRect) -> Path { /* ... */ }
}
```

> Source: "What's new in SwiftUI" (WWDC25, session 256)

**Presentations** — use `navigationZoomTransition` to morph sheets out of their source view.
Toolbar items and buttons can serve as the transition source:

```swift
.toolbar {
    ToolbarItem {
        Button("Add", systemImage: "plus") { showSheet = true }
            .navigationTransitionSource(id: "addSheet", namespace: namespace)
    }
}
.sheet(isPresented: $showSheet) {
    AddItemView()
        .navigationTransitionDestination(id: "addSheet", namespace: namespace)
}
```

> Source: "Build a SwiftUI app with the new design" (WWDC25, session 323)

**Controls** — use `controlSize(.extraLarge)` for extra-large prominent action buttons, and the
`concentric` corner style for buttons that match their container's corners:

```swift
Button("Get Started") { }
    .buttonStyle(.borderedProminent)
    .controlSize(.extraLarge)

Button("Confirm") { }
    .clipShape(.rect(cornerRadius: 12, style: .concentric))
```

Sliders now support tick marks and a neutral value:

```swift
Slider(value: $speed, in: 0.5...2.0, step: 0.25) {
    Text("Speed")
} ticks: {
    SliderTick(value: 0.6)
    SliderTick(value: 0.9)
}
.sliderNeutralValue(1.0)
```

> Source: "Build a SwiftUI app with the new design" (WWDC25, session 323)

**Rich text** — use `TextEditor` with an `AttributedString` binding for rich text editing. It
supports bold, italic, underline, strikethrough, custom fonts, foreground/background colors,
paragraph styles, and Genmoji:

```swift
@State private var text: AttributedString = "Hello, world!"

var body: some View {
    TextEditor(text: $text)
}
```

> Source: "Cook up a rich text experience in SwiftUI with AttributedString" (WWDC25, session 280)

**Web content** — use `WebView` to display web content; for richer interaction, create a
`WebPage` observable model:

```swift
// Simple URL display
WebView(url: URL(string: "https://example.com")!)

// With observable model
@State private var page = WebPage()

WebView(page)
    .onAppear { page.load(URLRequest(url: myURL)) }
    .navigationTitle(page.title ?? "")
```

> Source: "Meet WebKit for SwiftUI" (WWDC25, session 231)

**Drag and drop** — use `dragContainer` for multi-item drag operations. Combine with
`DragConfiguration` for custom drag behavior and `onDragSessionUpdated` to observe events:

```swift
PhotoGrid(photos: photos)
    .dragContainer(for: Photo.self) { selection in
        return selection.map { $0.transferable }
    }
    .onDragSessionUpdated { session in
        if session.phase == .endedWithDelete {
            deleteSelectedPhotos()
        }
    }
```

> Source: "What's new in SwiftUI" (WWDC25, session 256)

**Scene bridging** — UIKit and AppKit lifecycle apps can now request SwiftUI scenes. This
enables using SwiftUI-only scene types like `MenuBarExtra` and `ImmersiveSpace` from imperative
lifecycle apps via `UIApplication.shared.activateSceneSession(for:errorHandler:)`.

> Source: "What's new in SwiftUI" (WWDC25, session 256)

### Quick Lookup Table

| Deprecated | Recommended | Since |
|-----------|-------------|-------|
| `navigationBarTitle(_:)` | `navigationTitle(_:)` | iOS 15+ |
| `navigationBarItems(...)` | `toolbar { ToolbarItem(...) }` | iOS 15+ |
| `navigationBarHidden(_:)` | `toolbarVisibility(.hidden, for: .navigationBar)` | iOS 15+ |
| `statusBar(hidden:)` | `statusBarHidden(_:)` | iOS 15+ |
| `edgesIgnoringSafeArea(_:)` | `ignoresSafeArea(_:edges:)` | iOS 15+ |
| `colorScheme(_:)` | `preferredColorScheme(_:)` | iOS 15+ |
| `foregroundColor(_:)` | `foregroundStyle(_:)` | iOS 15+ |
| `cornerRadius(_:)` | `clipShape(.rect(cornerRadius:))` | iOS 15+ |
| `actionSheet(...)` | `confirmationDialog(...)` | iOS 15+ |
| `alert(isPresented:content:)` | `alert(_:isPresented:actions:message:)` | iOS 15+ |
| `autocapitalization(_:)` | `textInputAutocapitalization(_:)` | iOS 15+ |
| `accessibility(label:)` etc. | `accessibilityLabel()` etc. | iOS 15+ |
| `TextField` `onCommit`/`onEditingChanged` | `onSubmit` + `focused` | iOS 15+ |
| `animation(_:)` (no value) | `animation(_:value:)` | Back-deploys (iOS 13+) |
| `Section(header:content:)` | `Section(content:header:)` | Future-deprecated |
| `Section(footer:content:)` | `Section(content:footer:)` | Future-deprecated |
| `Section(header:footer:content:)` | `Section(content:header:footer:)` | Future-deprecated |
| Manual `EnvironmentKey` | `@Entry` macro | Back-deploys (Xcode 16+) |
| `NavigationView` | `NavigationStack` / `NavigationSplitView` | iOS 16+ |
| `accentColor(_:)` | `tint(_:)` | iOS 16+ |
| `disableAutocorrection(_:)` | `autocorrectionDisabled(_:)` | iOS 16+ |
| `UIPasteboard.general` | `PasteButton` | iOS 16+ |
| `onChange(of:perform:)` | `onChange(of:) { }` or `onChange(of:) { old, new in }` | iOS 17+ |
| `UIImpactFeedbackGenerator` / `UISelectionFeedbackGenerator` / `UINotificationFeedbackGenerator` | `sensoryFeedback(_:trigger:)` | iOS 17+ |
| `MagnificationGesture` | `MagnifyGesture` | iOS 17+ |
| `RotationGesture` | `RotateGesture` | iOS 17+ |
| `coordinateSpace(name:)` | `coordinateSpace(.named(...))` | iOS 17+ |
| `ObservableObject` | `@Observable` | iOS 17+ |
| `tabItem(_:)` | `Tab` API | iOS 18+ |
| Manual `animatableData` | `@Animatable` macro | iOS 26+ |
| `presentationBackground(_:)` on sheets | Default Liquid Glass sheet material | iOS 26+ |
| Custom toolbar background hacks | `scrollEdgeEffectStyle(_:for:)` | iOS 26+ |
