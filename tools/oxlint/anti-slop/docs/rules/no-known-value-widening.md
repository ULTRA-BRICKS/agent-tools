# `no-known-value-widening`

Do not discard useful type evidence by annotating an already-known value with a broader target type.

## Avoid

```ts
const handlers: Record<string, Handler> = {
  start: startHandler,
};
```

The explicit `Record<string, Handler>` loses the known `start` key.

## Prefer inference

```ts
const handlers = {
  start: startHandler,
};
```

## Prefer `satisfies` when you also need validation

```ts
const handlers = {
  start: startHandler,
} satisfies Record<string, Handler>;
```

`satisfies` checks compatibility without replacing the value's inferred type.

## Prefer a named owner contract when the broader abstraction is real

If the value genuinely crosses an abstraction boundary, give that boundary a domain-owned type instead of widening to an anonymous dictionary or top type.
