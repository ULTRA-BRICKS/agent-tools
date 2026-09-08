# `no-unsafe-dictionary-type`

Avoid dictionary contracts whose values are `unknown`, `any`, `object`, `{}`, or unions/aliases containing those escape hatches.

## Avoid

```ts
type Metadata = Record<string, unknown>;
type OtherMetadata = { [key: string]: object };
```

## Prefer an owner- or schema-derived value type

```ts
type MetadataValue = string | number | boolean;
type Metadata = Record<string, MetadataValue>;
```

When the payload is external, parse it before insertion:

```ts
const metadata = MetadataSchema.parse(payload);
```

The dictionary should state what values it owns. If the values are heterogeneous, model the allowed variants explicitly rather than using a top type as an escape hatch.
