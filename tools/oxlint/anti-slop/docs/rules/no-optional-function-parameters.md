# `no-optional-function-parameters`

## Avoid

Using `?` on positional function parameters as an implicit second calling convention.

```ts
loadUser(id: UserId, locale?: Locale);
```

## Prefer

Put optional input in a named request/options contract. If a positional API is genuinely the right abstraction, make the uncertainty explicit as a required `T | undefined` value rather than silently shortening the argument list.

```ts
loadUser({ id, locale }: { id: UserId; locale?: Locale });
```

## Why

Optional positional parameters combine ordering with presence semantics. Named fields make omission visible, scale to additional options, and give schema/domain contracts a natural owner.

## Exceptions

Framework callbacks, standard-library-compatible signatures, generated declarations, overload implementations, and small low-level functions can legitimately use optional positional parameters. This rule is therefore off by default and belongs to a stricter API-shape profile rather than the universal anti-slop baseline.
