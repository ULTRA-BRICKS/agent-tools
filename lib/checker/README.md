# model-freshness checker

Keeps [`lib/contracts/models.yaml`](../contracts/models.yaml) — the ecosystem's **model
board** — current. A daily job polls each provider's model-list endpoint and, when a newer
version of a pinned model appears, **proposes a bump** (a PR, or a dated report). It is
**semi-automatic**: it PROPOSES, a human confirms. A newer model can regress (a "turbo"
variant may drop vision, a point release may be cheaper-but-worse), so nothing is
auto-merged.

This pairs with **#3681** (models marked with capabilities so the image-review path can
filter to vision-capable models) and the CTO's #3685 direction (a central manifest + a daily
checker + rig provisioning the checker as a cron).

---

## The manifest — `lib/contracts/models.yaml`

The single source of truth. Validated by
[`models.schema.json`](../contracts/models.schema.json) (JSON-Schema) **plus** the checker's
cross-reference `--validate` (the vision/`:latest` invariants a schema can't express).

### Shape

```yaml
version: 1

models:                                  # concrete pins, strongest-first within a provider
  - id: moonshotai/Kimi-K2.7-Code        # concrete, provider-resolvable id (never an alias)
    provider: commandcode                # one of: anthropic openai gemini commandcode zai kimi-code fireworks
    capabilities: [code, reasoning]      # closed set; NO `vision` — Kimi-K2.7-Code is code-only
    context: 256000                      # advertised input window (tokens), optional
    notes: "code-specialized, NO vision (#3681)"
  - id: kimi-k2p6-turbo
    provider: commandcode
    capabilities: [vision, code, reasoning]   # HAS vision — the vision-capable Kimi seat
    context: 256000

roles:                                   # symbolic lens -> a concrete id present in `models`
  architect: claude-fable-5
  fast: gemini-2.5-flash
  code: moonshotai/Kimi-K2.7-Code
  reasoning: claude-opus-4-8
  vision: kimi-k2p6-turbo                # MUST resolve to a vision-capable entry (#3681)

aliases:                                 # convenience + `<provider>:latest` pointers
  openai:latest: gpt-5.5
  commandcode:latest: moonshotai/Kimi-K2.7-Code
  # ...
```

### Capability tags

`capabilities` is a **closed set**: `vision`, `code`, `reasoning`, `tools`, `embeddings`,
`audio`. The load-bearing one is **`vision`**:

- The image-review path (#3681) filters the board to **vision-capable** models. A model
  without real image input MUST NOT carry `vision`.
- The canonical example: **`moonshotai/Kimi-K2.7-Code`** is code-specialized and has **no**
  vision; **`kimi-k2p6-turbo`** **has** vision. Tag them accordingly or image-review routes
  to a model that can't see.
- The `vision` **role** (and any `vision` alias) MUST point at a vision-capable entry. The
  schema can't enforce that cross-reference; `model_freshness.py --validate` does.

### Invariants the `--validate` enforces (beyond the schema)

- every `roles:`/`aliases:` target is a **concrete id present in `models:`** (no dangling
  symbolic pointers);
- the **`vision`** role/alias resolves **only** to a vision-capable entry;
- a **`<provider>:latest`** alias points at an entry of **that** provider;
- capabilities are drawn from the known set.

```sh
python3 lib/checker/model_freshness.py --validate
# manifest OK — 11 models, 5 roles, 7 aliases
```

---

## The checker contract — `model_freshness.py`

Stdlib-first (only `yaml` is lazy-imported, for the manifest; HTTP via `urllib`). No provider
SDKs, no third-party HTTP client.

### What a run does

1. **Load + validate** the manifest (a fail-loud `ManifestError` if invalid — a silently
   empty manifest would "find" everything as new).
2. **Poll** each provider that exposes a model list, using a key **harvested** from the
   existing CLI/env config (env vars first, then the same `.env` fallback files review-cli
   reads — never hardcoded). Endpoints:
   | provider | endpoint | key (env, then `.env` fallback) |
   |---|---|---|
   | openai | `GET {OPENAI_BASE_URL}/v1/models` | `OPENAI_API_KEY` |
   | anthropic | `GET {ANTHROPIC_BASE_URL}/v1/models` | `ANTHROPIC_API_KEY` |
   | gemini | `GET {GEMINI_BASE_URL}/v1beta/models` | `GEMINI_API_KEY` / `GOOGLE_API_KEY` |
   | commandcode | `GET {COMMANDCODE_BASE_URL}/models` | `COMMANDCODE_API_KEY` |
   | zai | `GET {ZAI_BASE_URL}/models` | `ZAI_API_KEY` / `ZHIPU_API_KEY` |
   | kimi-code | `GET {KIMI_CODE_BASE_URL}/models` | `KIMI_API_KEY`, else the omp `kimi-code` OAuth login (`omp token kimi-code`, with a read-only `agent.db` peek as fallback; OAuth token only ever goes to the canonical endpoint) |
   | fireworks | *(routed via commandcode — no direct list, skipped)* | — |

   A provider whose key is **absent** is **skipped**, never a crash. A network/parse error
   is caught and recorded, not raised.
3. **Compute proposals**: for each pin, find the newest **same-family** id the endpoint
   advertises that is **strictly newer** than the pin. Family = the id with its numeric
   version runs stripped, so `gpt-5.6` is newer than `gpt-5.5` but `gemini-3.0` is never a
   bump for `gpt-5.5` (different family).
4. **Propose** (never merge):
   - **`gh` + auth present** → open a PR against agent-tools that **surgically repoints the
     `id:`** in `models.yaml`, with the diff + the new model's (copied-from-pin, **unverified**)
     capabilities in the body. **Idempotent**: the PR branch is deterministic per
     `(provider, new id)` (`model-freshness/<slug>`); if an open PR for that bump already
     exists, the run is a no-op for it (no duplicate).
   - **`gh`/auth absent** → write a dated report to `reports/<YYYY-MM-DD>-model-freshness.md`
     (the path is deterministic per day; a same-day re-run overwrites). Reports are git-ignored.

### Why PROPOSE, never auto-merge

Capabilities are **copied from the current pin and NOT verified** against the new model — the
endpoint rarely advertises capability flags. A point/turbo release can silently drop `vision`
or shrink context. The proposal surfaces the unverified-capabilities caveat so the human
checks the new model's real capabilities, fixes `capabilities:`/`context:` if they changed,
**then** merges.

### CLI

```sh
python3 lib/checker/model_freshness.py            # poll + propose (PR if gh, else report)
python3 lib/checker/model_freshness.py --validate # validate the manifest and exit
python3 lib/checker/model_freshness.py --dry-run  # poll + compute, but open nothing / write nothing-but-report
python3 lib/checker/model_freshness.py --report   # force the dated-report path even if gh is available
python3 lib/checker/model_freshness.py --json      # machine-readable summary
```

The daily cron rig provisions runs exactly `python3 <checkout>/lib/checker/model_freshness.py`
at **12:00 (noon)** — launchd on macOS, crontab on Linux. See
[rig-cli](https://git.hyperide.ai/ultrabricks/rig-cli) (`models:` block / `rig status`).

#### Exit codes (the cron / a wrapper script branches on these)

The CLI uses the shared [`agenttools_errors`](../agenttools_errors/README.md) contract — a
failure prints the three-part `error:` / `why:` / `fix:` block on stderr and exits with a
stable, per-class code:

| Code | Meaning |
| --- | --- |
| `0` | success (proposed / validated / nothing to do) |
| `2` | the manifest is malformed or violates an invariant (`EXIT_CONFIG`) — run `--validate` for the full list |
| `127` | PyYAML is not installed (`EXIT_MISSING_DEP`) — the error carries the install command |

An *unexpected* crash (a bug) still propagates as a traceback + exit `1`, kept distinct from a
*diagnosed* config error so a wrapper can tell "the manifest is bad" from "the checker broke".

### Tests

`tests/test_model_freshness.py` — hermetic: endpoints mocked via the injected `pollers`
hook, `gh` via the injected `gh_available` hook (no network, no real `gh`). Covers manifest
load/validate (incl. the vision cross-reference + the Kimi vision example), version
comparison, proposal computation, the report fallback, gh idempotency/dry-run, key
harvesting, endpoint parsers, and schema conformance.

```sh
uv run --with pytest --with pyyaml --with jsonschema python -m pytest tests/test_model_freshness.py -q
```
