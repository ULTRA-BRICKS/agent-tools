# `no-module-mocking`

Avoid replacing whole modules through Vitest or Jest mocking APIs. Tests should replace dependencies through real seams that production code also understands.

## Avoid

```ts
vi.mock("./user-store");
```

## Prefer dependency injection through an interface

```ts
interface UserStore {
  load(id: string): Promise<User | null>;
}

async function getUser(id: string, store: UserStore) {
  return store.load(id);
}
```

A test can pass a faithful in-memory implementation:

```ts
const store: UserStore = {
  async load(id) {
    return id === "42" ? testUser : null;
  },
};
```

Service layers, adapters, and other named dependency seams are equally valid. The point is to make replacement an explicit part of the design rather than a test-runner side effect.
