# UIKit Accessibility Patterns — Complete Reference

UIKit accessibility patterns for custom elements, containers, traits, notifications, and modal handling.

## Table of Contents

1. [UIAccessibilityElement](#uiaccessibilityelement)
2. [UIAccessibilityContainer](#uiaccessibilitycontainer)
3. [Trait Combinations](#trait-combinations)
4. [TableView/CollectionView](#tableviewcollectionview)
5. [Custom Properties](#custom-properties)
6. [Attributed Labels](#attributed-labels)
7. [Navigation and Grouping](#navigation-and-grouping)

## UIAccessibilityElement

For views with custom drawing (drawRect, CALayer, Metal):

```swift
class GraphView: UIView {
    override var isAccessibilityElement: Bool {
        get { false }  // Container, not element itself
        set { }
    }

    override var accessibilityElements: [Any]? {
        get {
            return dataPoints.enumerated().map { index, point in
                let element = UIAccessibilityElement(accessibilityContainer: self)
                element.accessibilityLabel = "\(point.label)"
                element.accessibilityValue = "\(point.value) units"
                element.accessibilityFrame = UIAccessibility.convertToScreenCoordinates(
                    point.frame, in: self
                )
                element.accessibilityTraits = .staticText
                return element
            }
        }
        set { }
    }
}
```

## UIAccessibilityContainer

```swift
class CustomContainer: UIView {
    func accessibilityElementCount() -> Int { return items.count }

    func accessibilityElement(at index: Int) -> Any? { return items[index] }

    func index(ofAccessibilityElement element: Any) -> Int {
        return items.firstIndex { $0 === element as AnyObject } ?? NSNotFound
    }

    // Container type (iOS 11+)
    override var accessibilityContainerType: UIAccessibilityContainerType {
        get { .list }  // .none, .dataTable, .list, .landmark, .semanticGroup
        set { }
    }
}
```

## Trait Combinations

```swift
// Selected tab button:
tab.accessibilityTraits = [.button, .selected]

// Disabled link:
link.accessibilityTraits = [.link, .notEnabled]

// Section header that's also a button:
headerButton.accessibilityTraits = [.header, .button]

// Adjustable custom slider (MUST implement increment/decrement):
customSlider.accessibilityTraits = [.adjustable]

// Live timer:
timerLabel.accessibilityTraits = [.updatesFrequently]
```

**Critical reminder:** When CHANGING state (e.g., selecting a tab), use `.insert()` / `.remove()`:
```swift
// ✅ State change — insert/remove
tab.accessibilityTraits.insert(.selected)
tab.accessibilityTraits.remove(.selected)

// ❌ State change — assignment DESTROYS other traits
tab.accessibilityTraits = .selected  // LOSES .button!
```

## TableView/CollectionView

```swift
// Section headers: set trait
headerLabel.accessibilityTraits = .header

// Cells with actions: use custom actions, not multiple buttons
func tableView(_ tableView: UITableView,
               cellForRowAt indexPath: IndexPath) -> UITableViewCell {
    let cell = tableView.dequeueReusableCell(withIdentifier: "Cell", for: indexPath)
    cell.accessibilityLabel = item.title
    cell.accessibilityHint = "Double tap to view details"
    cell.accessibilityCustomActions = [
        UIAccessibilityCustomAction(name: "Delete",
            target: self, selector: #selector(deleteItem(_:))),
        UIAccessibilityCustomAction(name: "Share",
            target: self, selector: #selector(shareItem(_:)))
    ]
    return cell
}

// Group cell content
cell.contentView.shouldGroupAccessibilityChildren = true
```

**Don'ts:**
- Don't set `isAccessibilityElement = false` on cells
- Don't put multiple visible buttons in a cell — use custom actions instead

## Custom Properties

### accessibilityActivationPoint
```swift
// For controls where accessible area doesn't match tap target
customControl.accessibilityActivationPoint = CGPoint(x: 50, y: 50)
```

### accessibilityPath
```swift
// For non-rectangular hit areas
circularButton.accessibilityPath = UIBezierPath(ovalIn: circularButton.bounds)
```

### accessibilityLanguage
```swift
// For mixed-language content
spanishQuote.accessibilityLanguage = "es"
// VoiceOver switches to Spanish voice for this element
```

### accessibilityNavigationStyle
```swift
// .automatic (default), .combined, .separate
view.accessibilityNavigationStyle = .combined
```

## Attributed Labels

For inline language changes or pronunciation control:

```swift
let attr = NSMutableAttributedString(string: "The word is hola")
attr.addAttribute(.accessibilitySpeechLanguage,
                  value: "es",
                  range: NSRange(location: 13, length: 4))
label.accessibilityAttributedLabel = attr
```

## Navigation and Grouping

### shouldGroupAccessibilityChildren
```swift
// Groups children so VoiceOver reads them as a unit
containerView.shouldGroupAccessibilityChildren = true
// Without this: VoiceOver may interleave elements from adjacent containers
```

### Custom element order
```swift
view.accessibilityElements = [headerLabel, contentView, actionButton, footerLabel]
```

**Warning:** Setting `accessibilityElements` completely overrides default ordering — you become responsible for ALL elements in the container.

### Custom action images (iOS 14+)
```swift
override var accessibilityCustomActions: [UIAccessibilityCustomAction]? {
    get {
        [UIAccessibilityCustomAction(name: "Pin",
            image: UIImage(systemName: "pin"),
            target: self, selector: #selector(pinAction))]
    }
    set { }
}
```
