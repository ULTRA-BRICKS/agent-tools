# `no-unknown-returns`

Do not expose `unknown` or `Promise<unknown>` as a function's explicit return contract.

## Avoid

```ts
function loadUser(): unknown {
  return input;
}
```

## Prefer parsing before returning

```ts
function loadUser(): User {
  return UserSchema.parse(input);
}
```

For asynchronous boundaries:

```ts
async function loadUser(): Promise<User> {
  const payload = await fetchUserPayload();
  return UserSchema.parse(payload);
}
```

A boundary function should turn an uncertain representation into the named domain type it promises to callers.
