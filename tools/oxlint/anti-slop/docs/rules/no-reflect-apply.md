# `no-reflect-apply`

Avoid `Reflect.apply` when a normal typed call can express the operation.

## Avoid

```ts
const value = Reflect.apply(operation, owner, args);
```

## Prefer a typed function call

```ts
const value = operation(...args);
```

If the receiver matters, model that explicitly in the function type or adapter instead of bypassing the ordinary call surface.

## Prefer a named dispatch interface for genuinely dynamic calls

```ts
interface Operation {
  run(args: readonly string[]): Result;
}

const value = operation.run(args);
```

The interface keeps dynamic dispatch explicit while preserving useful type evidence.
