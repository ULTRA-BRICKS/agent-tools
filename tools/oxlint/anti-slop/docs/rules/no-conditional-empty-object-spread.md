# `no-conditional-empty-object-spread`

Avoid conditional object spreads that use `{}` only as an omission sentinel.

## Avoid

```ts
const options = {
  ...(timeout !== undefined ? { timeout } : {}),
};
```

## Prefer

```ts
const options: Options = {};

if (timeout !== undefined) {
  options.timeout = timeout;
}
```

The intent is to add a property only when it is present. Express that control flow directly instead of manufacturing an empty object for the false branch.

## Preserve omission semantics

Do not mechanically replace the original with this unless the API treats an omitted property and an explicitly `undefined` property identically:

```ts
const options = { timeout };
```

Those shapes differ for operations such as `"timeout" in options`, `Object.keys(options)`, and APIs that distinguish omission from explicit `undefined`.
