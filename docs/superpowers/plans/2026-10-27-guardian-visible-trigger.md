# Guardian visible model-trigger implementation plan

**Goal:** Restore verified model probing when a responsive hidden duplicate exists, without weakening recovery admission. Refs #1034.

**Architecture:** Keep the existing LiveAdapter and DOM identity proof. Filter the Python model-trigger locator to rendered visibility before requiring exactly one candidate. Do not infer a model from the closed Pro label. No changes to sends, quota, stop, account, turn or model-selection rules.

**Verification:** Synthetic browser fixtures cover a hidden duplicate before or after the visible control, no visible matches, two visible matches, running turns and owner drafts. Every probe-only case asserts no chat send or model switch. The existing live-adapter suite retains node replacement, quota, stop and model-proof coverage.

**Implementation sequence:** Reproduce the verified-model failure with duplicate-trigger fixtures; change only the Python locator; run the new regressions and existing adapter tests; pass normal review and release gates; verify installed live recovery separately. A sent message is not proof of resumed generation.

**Scope decisions:** Keep the small regression file separate instead of growing the existing large test module. Reuse that module's synthetic HTML and runner under the repository's existing pytest collection mode. Do not introduce a second fixture implementation, a generalized control abstraction, model aliases, or relaxed authorization rules for this selector defect.
