# `no-widen-then-assert`

Do not erase a known local type and later recreate it with an assertion.

## Avoid

```ts
const loaded: User = loadUser();
const stored: unknown = loaded;
const user = stored as User;
```

## Prefer carrying the precise type through the flow

```ts
const loaded: User = loadUser();
const stored = loaded;
const user = stored;
```

If uncertainty enters at a real boundary, parse it once there instead of widening a known value and asserting it back later.

The rule targets immutable local flows where the original evidence is syntactically available, so the repair is usually to remove the unnecessary broad annotation or assertion rather than add another check.
