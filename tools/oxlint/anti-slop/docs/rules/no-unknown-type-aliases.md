# `no-unknown-type-aliases`

Do not hide `unknown` behind a name that makes an unparsed value look domain-specific.

## Avoid

```ts
type ExternalValue = unknown;
```

## Prefer explicit `unknown` at the boundary

```ts
const payload: unknown = await response.json();
const user = UserSchema.parse(payload);
```

## Prefer the parsed owner type afterward

```ts
type User = z.infer<typeof UserSchema>;
```

Keeping `unknown` explicit makes the parsing boundary visible. Once parsing succeeds, use the real domain type rather than an alias that merely renames uncertainty.
