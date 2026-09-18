# WCAG 2.2 to iOS API Mapping

Maps WCAG 2.2 AA success criteria to specific iOS APIs. Identifies what's automatically handled by standard UIKit/SwiftUI controls vs what requires manual implementation.

## Automatically Handled by Standard Controls

These criteria are satisfied when using standard UIKit/SwiftUI controls without modification:

| WCAG | Criterion | Why It's Handled |
|---|---|---|
| 4.1.2 | Name, Role, Value | Standard controls auto-set traits, labels, values |
| 2.5.2 | Pointer Cancellation | UIKit fires on `.touchUpInside`, not down |
| 2.1.1/2.1.2 | Keyboard | Standard controls work with VoiceOver, Switch Control, keyboards |
| 3.1.1 | Language | iOS respects NSLocale |
| 3.2.1/3.2.2 | On Focus/Input | Standard controls don't trigger context changes |
| 1.3.2 | Meaningful Sequence | Default left-to-right, top-to-bottom reading order |

## Requires Manual Implementation

| WCAG | Criterion | iOS API | Priority | Notes |
|---|---|---|---|---|
| **1.1.1** | Non-text Content | `accessibilityLabel` on images; `accessibilityHidden(true)` for decorative | 🔴 | #1 AI failure — always missing |
| **1.3.1** | Info & Relationships | `.header` trait; `accessibilityElements`; `.accessibilityElement(children:)` | 🟡 | Headings, grouping, tables |
| **1.3.4** | Orientation | Don't lock orientation in Info.plist | 🟢 | |
| **1.3.5** | Input Purpose | `.textContentType(.emailAddress)` on TextFields | 🟢 | Enables autofill |
| **1.4.1** | Use of Color | `accessibilityDifferentiateWithoutColor` + icons/text | 🟡 | Never color-only indicators |
| **1.4.3** | Contrast (Minimum) | 4.5:1 normal, 3:1 large; `accessibilityContrast` trait | 🟡 | Test both light and dark mode |
| **1.4.4** | Resize Text | `preferredFont(forTextStyle:)` + `adjustsFontForContentSizeCategory` | 🟡 | AI always uses hardcoded sizes |
| **1.4.10** | Reflow | Dynamic Type accessibility sizes + HStack→VStack adaptation | 🟡 | ViewThatFits, AnyLayout |
| **1.4.11** | Non-text Contrast | 3:1 for UI components and borders | 🟢 | Design concern |
| **1.4.13** | Content on Hover | Popovers/tooltips must be dismissible and persistent | 🟢 | |
| **2.2.1** | Timing | Timeout warnings with extend/disable options | 🟢 | |
| **2.2.2** | Pause, Stop, Hide | `isReduceMotionEnabled`; pause controls for auto-playing | 🟡 | |
| **2.3.1** | Three Flashes | No content >3 flashes/sec | 🔴 | Seizure risk |
| **2.4.3** | Focus Order | `accessibilityElements`; `.accessibilitySortPriority` | 🟡 | |
| **2.4.6** | Headings and Labels | `.accessibilityAddTraits(.isHeader)` | 🟡 | Enables rotor navigation |
| **2.5.1** | Pointer Gestures | Single-tap alternatives for multi-touch gestures | 🟢 | |
| **2.5.3** | Label in Name | `accessibilityLabel` must contain visible text | 🟡 | Critical for Voice Control |
| **2.5.7** | Dragging (NEW 2.2) | Single-tap alternatives; `accessibilityCustomActions` | 🟢 | |
| **2.5.8** | Target Size (NEW 2.2) | Min 24x24pt (Apple HIG: 44x44pt) | 🟢 | |
| **3.3.1** | Error Identification | `AccessibilityNotification.Announcement` for errors | 🟡 | |
| **3.3.8** | Auth (NEW 2.2) | `.password` textContentType; enable paste; biometric auth | 🟢 | Support password managers |
| **4.1.2** | Name/Role/Value | Manual for custom controls only | 🔴 | Standard controls handled |

## WCAG 2.2 New Criteria Affecting iOS

| Criterion | Requirement | iOS Implementation |
|---|---|---|
| **2.4.11** Focus Not Obscured | Sticky headers/footers must not cover focused elements | Ensure scroll-to-visible on focus |
| **2.4.13** Focus Appearance | 3:1 contrast focus indicator for custom controls | Custom focus rings on non-standard controls |
| **2.5.7** Dragging Movements | Non-drag alternatives for all drag operations | `accessibilityCustomActions` for reorder |
| **2.5.8** Target Size Minimum | 24x24pt minimum (Apple HIG: 44x44pt) | `.frame(minWidth: 44, minHeight: 44)` |
| **3.2.6** Consistent Help | Help in same position across screens | Consistent help button placement |
| **3.3.7** Redundant Entry | Auto-populate previously entered info | `textContentType` for autofill |
| **3.3.8** Accessible Auth | Support password managers, paste, biometrics | `.password` textContentType, no paste blocking |

## Criteria That DON'T Apply to Native iOS Apps

| WCAG | Criterion | Why Not Applicable |
|---|---|---|
| 2.4.1 | Bypass Blocks | Within individual app, not applicable |
| 4.1.1 | Parsing | "Always satisfied" per 2023 WCAG errata |
| 1.4.12 | Text Spacing | Only for markup-based content (WebViews), not native views |

## Decision Table: System Settings

| Setting | Check | Action When Enabled |
|---|---|---|
| Dynamic Type | `dynamicTypeSize.isAccessibilitySize` | Switch HStack→VStack, add ScrollView |
| Increase Contrast | `contrast == .increased` | Use 7:1 ratios, darker borders |
| Reduce Transparency | `reduceTransparency` | Solid backgrounds, no blur |
| Reduce Motion | `reduceMotion` | Crossfade/disable animations |
| Differentiate w/o Color | `diffWithoutColor` | Add shapes/icons to color indicators |
| Bold Text | `legibilityWeight == .bold` | Use bolder font weights |
| Smart Invert | `invertColors` | `accessibilityIgnoresInvertColors` on photos/video |
| VoiceOver Active | `voiceOverEnabled` | Avoid disruptive UI changes (rare use) |
