# `no-shape-in-symbol-names`

Avoid `shape` in symbol names when it merely describes structural form rather than domain ownership.

## Avoid

```ts
interface UserShape {
  id: string;
}
```

## Prefer the domain role

```ts
interface User {
  id: string;
}
```

For transport or storage variants, name the owner and role explicitly:

```ts
interface UserResponse {
  id: string;
}

interface UserRecord {
  id: string;
}
```

Names should answer what the value is for or who owns the contract, not just that it has a particular structural shape.
