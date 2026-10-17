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
  - id: deepseek/deepseek-v4-pro         # concrete, provider-resolvable id (never an alias)
    provider: commandcode                # one of: anthropic openai gemini commandcode zai kimi-code fireworks
    capabilities: [reasoning, code]      # closed set; NO `vision` — deepseek-v4-pro is text-only
    context: 128000                      # advertised input window (tokens), optional
    notes: "text-only, NO vision (#3681)"
  - id: kimi-k2p6-turbo
    provider: commandcode
    capabilities: [vision, code, reasoning]   # HAS vision — the vision-capable Kimi seat
    context: 256000

roles:                                   # symbolic lens -> a concrete id present in `models`
  architect: claude-fable-5-1
  fast: gemini-3.8-flash
  code: moonshotai/Kimi-K3
  reasoning: claude-opus-5-5
  vision: kimi-k2p6-turbo                # MUST resolve to a vision-capable entry (#3681)

aliases:                                 # convenience + `<provider>:latest` pointers
  openai:latest: gpt-6.1-sol
  commandcode:latest: moonshotai/Kimi-K3
  # ...
```

### Capability tags

`capabilities` is a **closed set**: `vision`, `code`, `reasoning`, `tools`, `embeddings`,
`audio`. The load-bearing one is **`vision`**:

- The image-review path (#3681) filters the board to **vision-capable** models. A model
  without real image input MUST NOT carry `vision`.
- The canonical example: **`deepseek/deepseek-v4-pro`** is text-only and has **no**
  vision; **`kimi-k2p6-turbo`** and **`moonshotai/Kimi-K3`** **have** vision. Tag them
  accordingly or image-review routes to a model that can't see.
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
   is caught and recorded, not raised. Every request sends a `User-Agent` naming the checker:
   Cloudflare-fronted hosts (models.dev, `api.commandcode.ai`) answer urllib's default one
   with HTTP 403.
2b. **Keyless harness catalogs** (#934). An OAuth-only machine (Claude Code, Codex/ChatGPT and
   omp subscriptions, no API keys) skips anthropic and openai above, so each poll is merged
   with the catalogs the logged-in harnesses already cache:
   | source | what it reads | covers |
   |---|---|---|
   | `omp` | `omp models --json` | every provider omp is logged into (`openai-codex` counts as openai) |
   | `codex` | `$CODEX_HOME/models_cache.json` (default `~/.codex`) | openai, the codex account's own list |
   | `claude` | `$CLAUDE_CONFIG_DIR/cache/model-catalog/*.json` (default `~/.claude`): `models[].id` + the CLI alias table `provider_alias_targets.*.default` | anthropic |
   | `models.dev` | `GET https://models.dev/api.json` (keyless; `MODELS_DEV_URL` overrides) | fallback, fetched once and only for a provider no keyed endpoint and no local catalog listed |

   A provider is polled when any source answered; `--json` lists the `sources` per provider
   and each catalog's per-provider counts or its `error` (missing file, `omp` not installed).
   A codex or Claude Code cache whose own fetch time (`fetched_at` / `fetchedAt`) is older
   than 7 days counts as unavailable, so the provider falls through to models.dev instead of
   being judged against an old list; an unreadable Claude cache file is skipped without
   hiding the readable ones. Ids outside `[A-Za-z0-9][A-Za-z0-9._:/@+-]*` are dropped from
   every catalog, so nothing a catalog serves can inject markup into the report.
3. **Compute proposals**: for each pin, find the newest **same-family** id the endpoint
   advertises that is **strictly newer** than the pin. Family = the id with its version runs
   (digits joined by `.` **or** `-`) replaced, so `gpt-5.6` is newer than `gpt-5.5` and
   `claude-fable-5-1` / `claude-opus-5-5` are newer than `claude-fable-5` / `claude-opus-4-8`,
   but `gemini-3.0` is never a bump for `gpt-5.5` (different family) and a different variant
   (`gpt-6.1-sol` for `gpt-5.5`, `Kimi-K3` for `Kimi-K2.7-Code`) is never auto-proposed. Such a
   pin is listed instead under "pins with newer variants" (human output, `--json`
   `pin_variants`, and the report, which a non-dry run writes for them even when `gh` opened
   the PRs), so a stale pin is never silent. On a version tie the bare id wins over a dated
   snapshot. A hyphen joins only a whole numeric segment, so a size (`Qwen3.8-27B`,
   `-1.7B`) is not read as a minor version.
3b. **review-cli seats** (#934, skip with `--no-review-cli`). review-cli's board does not read
   models.yaml, so a models.yaml bump alone never reaches a review. The checker scans the seat
   strings (`claude:…`, `codex:…`, `commandcode:…`, `zai:…`, `gemini:…`, and `oc:`/`omp:`
   `<provider>/<model>`) in review-cli's code defaults (`reviewlib/config.py` of the checkout
   the installed `review` resolves into, or `--review-cli-root DIR`) and in the user's
   `~/.config/review-cli/config.yaml`, against the same catalogs (models.dev covers providers
   such as `xai` and `opencode-go` that the manifest does not pin). It reports:
   - `STALE <seat> -> <seat>` with `file:line` for a same-family successor;
   - `newer variants for <seat>` (e.g. `codex:gpt-5.6-terra` -> `gpt-6-luna`, `gpt-6.1-sol`):
     which variant replaces the seat is a human choice, so these are listed, never patched;
   - seats whose provider no catalog lists, as `unchecked`.

   Comments and version-pinned alias entries (a dict key with a digit, like
   `"opus48": "claude:claude-opus-4-8"`) are skipped.

   **Writing the code defaults: a patch, not a PR.** `--review-cli-patch FILE` (`-` = stdout)
   writes a `git apply`-able diff of `reviewlib/config.py` that swaps every stale
   same-family seat literal; a non-dry run also drops it next to the report as
   `reports/<date>-review-cli.patch`. Under `--dry-run` only `-` produces output (a FILE is
   never written), and nothing is written when no seat is stale. A version-pinned alias that
   points at a bumped constant (`"fable5": FABLE_SEAT`) is rewritten to the constant's old
   literal, so it keeps its release. That is the smallest robust design: review-cli has its
   own ticket, review and ship flow (and tests that pin seat ids and vision hints), so the
   patch goes in through a normal review-cli PR whose CI catches a bump that needs more than
   an id swap; the checker never pushes into another repo. The user's `config.yaml` is
   personal and is only reported, never patched.
4. **Propose** (never merge):
   - **`gh` + auth present** → open a PR against agent-tools that **surgically repoints the
     `id:`** in `models.yaml`, with the diff + the new model's (copied-from-pin, **unverified**)
     capabilities in the body. **Idempotent**: the PR branch is deterministic per
     `(provider, new id)` (`model-freshness/<slug>`); if an open PR for that bump already
     exists, the run is a no-op for it (no duplicate).
   - **`gh`/auth absent** → write a dated report to `reports/<YYYY-MM-DD>-model-freshness.md`
     (the path is deterministic per day; a same-day re-run overwrites). Reports are git-ignored.
   - Stale review-cli seats have no PR path here, so a non-dry run writes them to that report
     (plus the patch) even when the models.yaml bumps went out as PRs.

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
python3 lib/checker/model_freshness.py --dry-run  # poll + compute, open nothing, write no report
python3 lib/checker/model_freshness.py --report   # force the dated-report path even if gh is available
python3 lib/checker/model_freshness.py --json      # machine-readable summary (sources, catalogs, review_cli)
python3 lib/checker/model_freshness.py --dry-run --review-cli-patch - | git -C <review-cli> apply
python3 lib/checker/model_freshness.py --review-cli-root ~/work/review-cli   # check that checkout's seats
python3 lib/checker/model_freshness.py --no-review-cli                      # models.yaml only
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
hook, `gh` via the injected `gh_available` hook, harness catalogs via `harness_sources` /
`fallback_sources` (an autouse fixture empties the real ones), `omp` via a mocked
`subprocess.run`, codex/Claude Code caches via temp `CODEX_HOME`/`CLAUDE_CONFIG_DIR` (no
network, no real `gh`). Covers manifest load/validate (incl. the vision cross-reference + the
vision examples), version comparison, proposal computation, the report fallback, gh
idempotency/dry-run, key harvesting, endpoint parsers, the harness catalog readers and their
merge/fallback order, the review-cli seat scan and its patch (applied with `git apply`), and
schema conformance.

```sh
uv run --with pytest --with pyyaml --with jsonschema python -m pytest tests/test_model_freshness.py -q
```
