# `no-reflect-get`

Avoid `Reflect.get` when ordinary typed property access can express the operation.

## Avoid

```ts
const value = Reflect.get(owner, key);
```

## Prefer typed property access

```ts
const value = owner.name;
```

For a dynamic but bounded key, encode the key set in the type:

```ts
type UserField = keyof User;

function readUserField(user: User, key: UserField) {
  return user[key];
}
```

For untrusted dynamic input, parse it into a named domain type before property access rather than carrying an untyped representation inward.
