# `no-unknown-parameters`

Do not make ordinary application functions responsible for interpreting `unknown` input.

## Avoid

```ts
function handle(input: unknown) {}
```

## Prefer a parsed domain type

```ts
function handle(input: Command) {}
```

Parse or decode the external value at the I/O boundary before calling the function:

```ts
const command = CommandSchema.parse(input);
handle(command);
```

This keeps `unknown` visible where uncertainty actually enters the system and prevents it from spreading through internal contracts. The rule intentionally permits the conventional `cause: unknown` error-enrichment parameter.
