# CodeQL (CI) — GitHub's semantic code-analysis engine

**Standard engine = [github/codeql-action](https://github.com/github/codeql-action).**
CodeQL is GitHub's own SAST. Use it for deep, semantic security analysis (taint tracking,
data-flow) that pattern scanners (see [`../sast/`](../sast/), semgrep) can't do. Run both —
they are complementary, not redundant.

## Two variants — pick by your repo's plan

| File | Use when | How it gates |
| ---- | -------- | ------------ |
| `workflow.yml` | **Public repo**, OR private repo **with** GitHub Advanced Security | Uploads SARIF to the Code Scanning dashboard; gate merges via branch protection on the `Code scanning results / CodeQL` check. |
| `workflow-selfgate.yml` | **Private repo WITHOUT** GitHub Advanced Security, **or a Forgejo / Gitea runner** | Runs the same analysis with the CodeQL CLI, keeps the SARIF local, and **fails the job in-line on findings** — no dashboard, no GHAS, no GitHub API, $0. |

The self-gate variant exists because, without GHAS, the dashboard upload returns
`403 "Advanced Security must be enabled"`. Rather than lose CodeQL entirely on a private
repo, the self-gate parses the SARIF itself and fails the check. This is the high-value,
non-obvious piece — most CodeQL templates silently assume GHAS.

The self-gate drives the **CodeQL CLI** from the pinned release bundle (`CODEQL_VERSION`), not
`github/codeql-action`: the action calls GitHub's code-scanning / feature-flag API while it
initializes, so on a Forgejo / Gitea runner it dies with `404 page not found` before analyzing
anything. Since the self-gate never uploads, it needs no GitHub API at all — the same engine
runs on GitHub-hosted and self-hosted runners (bash, curl, tar, python3; `zstd` makes it fetch
the ~100 MB per-language bundle instead of the ~660 MB full one).

## Quick start

```bash
# Public repo / GHAS:
cp ci/codeql/workflow.yml .github/workflows/codeql.yml

# Private repo, no GHAS:
cp ci/codeql/workflow-selfgate.yml .github/workflows/codeql.yml
```

Then edit the `language` matrix for your stack.

## Knobs

- **Languages** — the `language:` matrix. `javascript-typescript` is the *combined* JS+TS
  language (don't also list them separately). Compiled languages (`java-kotlin`, `cpp`,
  `csharp`, `go`) typically need `build-mode: autobuild`.
- **Query suite** — `queries: security-extended` (broad). Use `security-and-quality` for
  even more (adds maintainability), or drop the line for the default suite. In the self-gate
  this is the `QUERY_SUITE` env (`security-extended` / `security-and-quality` / `code-scanning`).
- **Scope** (self-gate) — `CODEQL_CONFIG_FILE` names a code-scanning config whose
  `paths` / `paths-ignore` apply at database creation (empty = the whole repo).
- **Severity floor** (self-gate only) — `GATE_LEVELS` env. `"error warning"` (default,
  recommended) gates on error+warning; `error` gates only the highest severity.
- **Suppression** (self-gate only) — a `codeql[<rule-id>]` marker on the flagged line, or
  anywhere in the contiguous comment block directly above it, suppresses a single justified
  false positive (so a multi-line justification block works; the scan stops at the first
  blank/non-comment line so a marker can't leak across statements). Both comment styles work:
  `// codeql[<rule-id>]` (JS/TS) and `# codeql[<rule-id>]` (YAML for the `actions` language,
  Python, shell). Greppable and reviewable — not a silent baseline.
- **Language not in the repo** (self-gate only) — the matrix ships a broad default
  (`javascript-typescript` + `actions`). A preflight step checks whether the repo actually
  contains source for each matrix language; if not (e.g. a Python-only repo has zero JS/TS),
  that language is **skipped cleanly** with a notice instead of hard-failing on CodeQL's
  "No source code seen" error. Trim the matrix to your real stack to drop the notice. Where
  source IS present nothing changes — the gate runs and blocks on findings as before.

## Pinning / supply-chain

The self-gate pins the CodeQL **bundle version** (`CODEQL_VERSION`); bump it deliberately to a
[CodeQL bundle release](https://github.com/github/codeql-action/releases). In `workflow.yml`,
`github/codeql-action` is a **first-party GitHub action** pinned by **major tag (`@v4`)** —
GitHub's own docs recommend this because the bundled CodeQL CLI rotates frequently and a
stale SHA pins you to old analysis. If your supply-chain policy requires SHA pins even for
first-party actions, resolve `github/codeql-action`'s SHA and pin it, accepting the manual
bump cost. `actions/checkout` IS SHA-pinned here.

## When to use

Always, for any repo with real source code — CodeQL is the deepest free SAST GitHub offers.
Pair it with semgrep ([`../sast/`](../sast/)) for fast pattern rules and with secret-scan
([`../secret-scan/`](../secret-scan/)) for credentials.
