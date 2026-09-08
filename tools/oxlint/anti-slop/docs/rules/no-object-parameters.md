# `no-object-parameters`

Do not accept the broad `object` type as a function input. It says almost nothing about what the function actually needs.

## Avoid

```ts
function save(value: object) {}
```

## Prefer a named owner type

```ts
interface SaveRequest {
  id: string;
  name: string;
}

function save(value: SaveRequest) {}
```

## Parse external input before calling domain code

```ts
const request = SaveRequestSchema.parse(input);
save(request);
```

The parsing boundary establishes the contract once. Internal functions then accept the domain type directly instead of repeatedly inspecting a broad representation.
