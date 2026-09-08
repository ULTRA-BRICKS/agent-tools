# `no-multiple-function-params`

## Avoid

Functions whose public or internal contract is expressed as several positional values when those values belong to one operation.

```ts
createUser(name, email, locale);
```

## Prefer

Use one named request/options object so the meaning is visible at the call site and the contract can evolve without parameter-order coupling.

```ts
createUser({ name, email, locale });
```

## Why

Positional parameters hide meaning at call sites and make adding/reordering fields an API change. A named input object keeps ownership and intent explicit and composes naturally with schema validation and domain types.

## Exceptions

This is intentionally an opinionated rule rather than a universal correctness invariant. Mathematical primitives, tiny callbacks whose signature is dictated by a platform API, constructors implementing an external interface, and performance-sensitive low-level code may reasonably keep positional parameters. Rig therefore keeps this rule off by default unless a stricter style profile enables it.
