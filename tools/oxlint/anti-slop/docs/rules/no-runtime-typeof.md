# `no-runtime-typeof`

Avoid ad hoc `typeof` checks as a substitute for establishing a real input contract.

## Avoid

```ts
if (typeof input === "string") {
  useName(input);
}
```

## Prefer parsing at the I/O boundary

```ts
const request = RequestSchema.parse(input);
useName(request.name);
```

The parser should turn an external representation into a meaningful domain value once. Internal code can then branch on domain state rather than repeatedly narrowing raw representations.

For domain variants, prefer discriminated unions and branch on the discriminant:

```ts
type Command =
  | { kind: "rename"; name: string }
  | { kind: "delete"; id: string };

if (command.kind === "rename") {
  useName(command.name);
}
```
