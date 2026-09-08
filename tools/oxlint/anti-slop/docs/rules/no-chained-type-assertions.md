# `no-chained-type-assertions`

Avoid assertion chains that erase evidence and then manufacture a new type.

## Avoid

```ts
const user = input as object as User;
```

## Prefer: keep precise evidence

```ts
const user: User = loadUser();
```

When the value is already typed, carry that precise type through the program instead of widening and reasserting it.

## Prefer: parse untrusted input once

```ts
const user = UserSchema.parse(input);
```

For external input, establish the contract at the I/O boundary with the expected parser or schema. Code after that boundary should receive `User`, not an unparsed value that requires assertion chains.

Chains made only of `as const` are intentionally allowed by the rule.
