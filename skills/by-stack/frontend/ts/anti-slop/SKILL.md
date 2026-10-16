---
name: anti-slop
description: Use when a frontend TypeScript/JavaScript repo adopts or hits anti-slop Oxlint rules. Explains preferred repairs, boundary parsing, the pinned vendored subrepo, and composition with type-aware Oxc rules.
---

# anti-slop inside the Mextner TypeScript policy

Anti-slop is a specialized semantic layer inside the broader policy documented in [`../README.md`](../README.md). Do not consume it as a remote runtime dependency. `agent-tools` pins the reviewed fork at `vendor/anti-slop`; Rig vendors the supported asset tree from that pinned source into each target repository.

This skill covers code. For AI tells in prose (issues, PR bodies, tg reports) use the universal `slop-check` skill.

## Preferred repairs

When a rule fires, preserve evidence rather than suppressing the rule:

- keep inference rather than widening a known value — [preserve inference](../rules/preserve-inference.md);
- use `satisfies` when a value must satisfy a broader contract without replacing its inferred type;
- parse untrusted input once at the I/O boundary — [parse at boundaries](../rules/parse-at-boundaries.md);
- use discriminated unions or named owner contracts — [illegal states](../rules/make-illegal-states-unrepresentable.md);
- replace module mocking with real dependency seams and reflection with direct typed operations — [typed reflection](../rules/no-reflection-for-typed-code.md);
- remove unsafe assertions; if TypeScript cannot express a runtime-checked invariant, document that exact invariant with `SAFETY:` — [unsafe assertions](../rules/no-unsafe-assertions.md).

The anti-slop subrepo contains the authoritative per-rule guides for every anti-slop rule. Do not duplicate or fork those implementations inside agent-tools.

## Complete Oxc assertion baseline

Anti-slop catches important structural assertion/evidence patterns. Type-aware Oxlint supplies the general semantic net around them:

- `typescript/no-unsafe-type-assertion` — reject assertions that narrow the actual type;
- `typescript/no-unnecessary-type-assertion` — remove assertions that do not change the type;
- `typescript/no-non-null-assertion` — reject postfix `!`;
- `typescript/ban-ts-comment` — ban `@ts-ignore`/`@ts-nocheck` and require a description for `@ts-expect-error`.

Run Oxlint in type-aware mode and keep `tsc --noEmit` as a separate compiler gate. Use Oxfmt for formatting; Biome is not part of the provisioned JS/TS baseline.

## Target layout

```text
tools/oxlint/anti-slop/
  index.ts
  rules/
  shared/
oxlint.config.ts
```

See `docs/typescript-policy-vs-anti-slop.md` for the complete overlap/addition/replacement matrix.
