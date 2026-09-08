# `require-safety-comment-for-type-assertion`

Every non-`const` type assertion must document the checked invariant TypeScript cannot express.

## Avoid

```ts
const userId = value as UserId;
```

## Prefer removing the assertion

When a parser, type guard, or more precise upstream type can establish the contract, prefer that and keep the evidence in the type system.

## When an assertion is genuinely necessary

State the invariant immediately before the assertion or its containing statement:

```ts
// SAFETY: parseUserId validated the identifier before branding it.
const userId = value as UserId;
```

The comment is not a generic waiver. It should identify the specific runtime fact, parser, protocol guarantee, or library invariant that makes the assertion sound.

`as const` is intentionally exempt because it narrows a value rather than asserting an unrelated runtime contract.
