---
name: result-report
description: Use when a completed task/PR needs a visual, interactive completion report published to a static GitHub Pages "reports" site — motivation, product changes, file impact, review findings, commit timeline, architecture diagrams, screenshots. Covers gathering the data (git/gh/ticket-tracker CLIs), capturing UI screenshots with `agent-browser` (not a browser MCP), populating the shared report template, publishing, and posting the link back to the ticket and the PR. Ported from HyperIDE's retired `.claude/commands/result-report.md`; HyperIDE's `hyperide.github.io` repo is the reference implementation.
---

# Result report: publish a visual completion report

A result report is a single static page (`reports/<TASK-ID>/index.html`) on a GitHub
Pages site that tells the story of one finished task: why it was done, what changed,
what the review found, and — for UI work — what it looks like. It renders from a plain
JS object (`REPORT_DATA`) against a shared template (`reports/shared/styles.css` +
`reports/shared/components.js`) that already lives in the target reports repo — you are
not re-implementing that template, only populating and publishing it.

**Prerequisite**: a reports repo with `reports/shared/{styles.css,components.js}`,
`_template/report.html`, and (ideally) `scripts/new-report.sh` for scaffolding new
report directories. HyperIDE's is `hyperide/hyperide.github.io`, published at
`https://hyperide.github.io/`. If your project doesn't have one yet, that's a
prerequisite to set up first (fork the shape of `hyperide.github.io`), not something
this skill builds for you.

Skip this skill entirely if the task has no story worth telling on its own page — most
routine fixes don't need one. Reach for it on substantial features, migrations, or
anything the team will want to reference later (a PR with >10 files changed, >300 lines,
or >5 commits is a reasonable trigger).

## Step 1 — Gather the data

```bash
MERGE_BASE=$(git merge-base main HEAD)

# Commits on this branch
git log --format='{"sha": "%H", "short": "%h", "message": "%s", "date": "%ci"}' $MERGE_BASE..HEAD

# File stats
git diff --numstat $MERGE_BASE..HEAD
git diff --stat $MERGE_BASE..HEAD

# PR details
gh pr view --json title,url,number,state
```

- **Ticket**: look it up with your ticket-tracker CLI, not an MCP tool — e.g. `linear
  issue view HYP-XXX` for Linear, or `task read HYP-XXX` — for title, description,
  acceptance criteria. (HyperIDE's Linear MCP tools — `list_issues`/`get_issue` — were
  retired; `linear`/`task` CLI is the current path everywhere in this project.)
- **Review findings**: from the current conversation's review-cli output, or re-run
  `review diff -C <repo>` against the merge base.
- **Line numbers**: for every function/symbol referenced in findings or product-change
  entries, find the real line numbers with Grep/Read so file links can carry
  `#L<N>-L<M>` anchors — GitHub renders those as a highlighted range.

## Step 2 — UI screenshots (skip if no UI changes)

Use the **`agent-browser`** CLI — not a Playwright/browser MCP tool. (The original
version of this workflow called `playwright_screenshot(selector=...)` via an MCP
server; that MCP is no longer how this project drives a browser. `agent-browser` covers
the same ground natively, with selector-scoped screenshots built in.)

**Start the dev server** on the target branch (use your project's `dev` process-runner
skill/CLI if it has one — `dev start` — instead of hand-rolling `pkill`/background job
management; HyperIDE's own dev server is `bun scripts/dev.ts`):

```bash
# Confirm nothing dirty is about to get clobbered
git status --porcelain   # STOP and ask if non-empty — never stash/discard silently

dev start                # or: bun scripts/dev.ts & (project-specific fallback)
```

Poll the app URL until it responds (max ~30s) before touching the browser.

**Capture element-level screenshots:**

```bash
agent-browser open https://local.hyperi.de/
agent-browser set viewport 1280 800

# Navigate to the changed UI area (open the relevant panel/tab first)
agent-browser click '[data-testid="settings-tab"]'

# Scroll changed elements into view before capturing
agent-browser scrollintoview '[data-testid="diagnostic-logs-viewer"]'
agent-browser wait '[data-testid="diagnostic-logs-viewer"]'

# Selector-scoped screenshot — direct equivalent of the old
# playwright_screenshot(selector=...) call
agent-browser screenshot '[data-testid="diagnostic-logs-viewer"]' 01-feature-name.png
agent-browser screenshot '.filter-bar' 02-filter-bar.png
agent-browser screenshot '#logs-panel' 03-logs-panel.png
```

- Save into `<reports-repo-clone>/reports/<TASK-ID>/` as PNG/JPEG (`--screenshot-format
  jpeg --screenshot-quality 80` for smaller files).
- Naming: `01-feature-name.jpg`, `02-filter-bar.jpg`, etc. — reference each in
  `REPORT_DATA.screenshots` as `{ src: '01-feature-name.jpg', caption: '...' }`.
- For each UI change, pick the best selector: `data-testid` / `role` / a stable class.
  Modified components — `scrollintoview` first. Large components — capture subparts
  separately rather than one giant screenshot.
- Dark theme is the project default; screenshots will match the report's own dark theme.
- If anything goes wrong (login wall, page won't load, element not found after 2-3
  tries) — stop and ask, don't keep retrying blindly. Check `agent-browser console` and
  `agent-browser errors` for a root cause before giving up.

**Cleanup:**

```bash
dev stop   # or: kill $DEV_PID / pkill -f 'bun scripts/dev.ts'
```

## Step 3 — Prepare the reports repo + scaffold the report

```bash
if [ -d /tmp/<reports-repo> ]; then
  cd /tmp/<reports-repo> && git pull
else
  cd /tmp && git clone https://github.com/<org>/<reports-repo>.git
fi
```

If the repo ships `scripts/new-report.sh` (HyperIDE's does), use it instead of
hand-copying the template — it stamps `_template/report.html` into
`reports/<TASK-ID>/index.html` with the task ID/title/date already substituted:

```bash
./scripts/new-report.sh HYP-XXX "Task Title"
```

If there's no scaffold script yet, copy `_template/report.html` (or the plain
`<!DOCTYPE html>` skeleton below) to `reports/<TASK-ID>/index.html` by hand.

## Step 4 — Populate the report

### Page structure

```html
<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>HYP-XXX — Task Title</title>
    <link rel="stylesheet" href="../shared/styles.css" />
  </head>
  <body>
    <div class="container">
      <!-- Standard sections — rendered by components.js from REPORT_DATA -->
      <div data-section="header"></div>
      <div data-section="motivation"></div>
      <div data-section="summary"></div>
      <div data-section="product-changes"></div>

      <!-- Custom sections (e.g. architecture diagram) go as inline HTML -->
      <section class="fade-in">
        <h2>Architecture</h2>
        <div class="arch-container"><svg>...</svg></div>
      </section>

      <div data-section="file-impact"></div>
      <div data-section="findings"></div>
      <div data-section="timeline"></div>
      <div data-section="codex-review"></div>
      <div data-section="adr"></div>
    </div>

    <footer
      style="text-align:center;padding:40px 24px;color:#484f58;font-size:.8rem;border-top:1px solid #21262d;margin-top:40px"
    >
      Generated YYYY-MM-DD &middot; <a href="https://hyperide.github.io/">Hyperide Reports</a>
    </footer>

    <script src="../shared/components.js"></script>
    <script>
      Report.init({
        // All report data — see REPORT_DATA schema below
      });
    </script>
  </body>
</html>
```

### REPORT_DATA schema

```js
Report.init({
  // Core metadata
  repo: 'https://github.com/hyperide/hyper-saas',
  headSha: '<full commit sha>',

  // Header
  title: 'HYP-XXX: Task Title',
  subtitle: 'Short description',
  date: 'YYYY-MM-DD',
  badge: { label: 'feat', type: 'feat' }, // type: feat|fix|refactor|docs
  branch: 'branch-name',
  links: [
    { label: 'PR #N', href: 'https://github.com/...' },
    { label: 'Linear HYP-XXX', href: 'https://linear.app/...' },
  ],

  // Motivation — WHY we did this (shown at top, supports HTML)
  motivation: '<strong>Why:</strong> explain the problem...<ul><li>...</li></ul>',

  // Summary
  stats: { files: N, insertions: N, deletions: N, commits: N },

  // Product Changes — three categories
  productChanges: {
    new: [
      { title: '...', desc: '...', files: [{ path: 'file.ts', lines: 'L10-L50', label: 'file.ts:10 (functionName)' }] },
    ],
    improvements: [
      /* same structure */
    ],
    fixes: [
      /* same structure */
    ],
  },

  // Files
  files: [{ path: 'relative/path.ts', dir: 'relative', add: N, del: N, status: 'new|modified|deleted' }],

  // Findings — with line number anchors
  findings: [
    {
      id: N,
      severity: 'P1|P2|P3',
      status: 'resolved|accepted',
      title: '...',
      file: 'path.ts',
      lines: 'L38-L42',
      desc: '...',
      fix: '...',
    },
  ],

  // Commits
  commits: [{ sha: 'short-sha', message: 'commit message', date: 'YYYY-MM-DD' }],

  // Review rounds — with individual findings per round
  reviewRounds: [
    {
      title: 'Round 1 — Initial Review (N/10)',
      desc: 'Summary...',
      badges: [
        { label: 'P1: N', cls: 'badge-p1' },
        { label: 'N resolved', cls: 'badge-resolved' },
      ],
      items: [
        {
          severity: 'P1',
          status: 'resolved',
          title: '...',
          file: 'path.ts',
          lines: 'L10-L20',
          resolution: '...',
        },
      ],
    },
  ],

  // Architecture Decision Records
  adrs: [
    {
      title: 'ADR-N: Decision title',
      problem: '...',
      options: '...',
      decision: '...',
      rationale: '...',
    },
  ],
});
```

(HyperIDE's live template names this field `codexRounds` for historical reasons — same
shape as `reviewRounds` above; check the `components.js` you're rendering against and
match its actual field name, since the renderer reads `data.codexRounds` /
`data.reviewRounds` literally.)

### Available `data-section` types

| Section           | Description                                                                       |
| ------------------ | ---------------------------------------------------------------------------------- |
| `header`          | Task title, metadata, links                                                       |
| `motivation`      | Why block — problem statement at top of report                                    |
| `summary`         | Four stat cards (files, +, -, commits)                                            |
| `product-changes` | What's New / Improvements / Fixes with file links                                 |
| `specs`           | Spec/plan viewer — card grid + modal with markdown rendering, fetches from GitHub |
| `file-impact`     | Files grouped by directory, expandable, with GitHub links                         |
| `findings`        | Filterable by severity, expandable cards with file#line links                     |
| `timeline`        | Horizontal commit dots, **clickable → opens GitHub commit**, tooltips             |
| `codex-review`    | Review rounds with expandable per-finding details                                 |
| `adr`             | Collapsible architecture decision cards                                           |

**`specs` data structure:**

```js
specs: [
  {
    id: 'spec', // short label shown on card
    title: 'Spec Title',
    file: 'docs/specs/2026-03-24-foo.md', // path in repo, fetched via raw.githubusercontent.com
    lines: 150, // optional, shown as metadata
    date: 'YYYY-MM-DD', // optional
    type: 'spec' | 'plan', // 'plan' uses green color scheme, 'spec' uses blue
  },
];
```

Modal features: markdown renderer (headers, bold, code blocks, tables, lists, links),
tabs for switching between specs, ← → keyboard nav, Esc to close, click outside to close.
Content is fetched lazily on first open and cached.

**`timeline` note:** commits are clickable — each dot opens `data.repo + '/commit/' + c.sha`
in a new tab. No extra configuration needed — just make sure `repo` and `headSha` are
set in `Report.init()`.

### Inline custom sections (use between standard sections)

For richer reports, add inline `<section class="fade-in">` blocks between `data-section`
divs. Recommended for complex infrastructure tasks:

**Architecture Deep Dive** — per-module cards after the SVG diagram:

- One card per key module: name, directory, purpose paragraph, non-obvious details paragraph
- Color-coded borders matching module role (green=service, indigo=shared, amber=extension, red=critical fix)
- Include a reference table for any naming/convention ambiguities (e.g. column 0-based vs 1-based table)
- Pattern: `grid-template-columns: repeat(auto-fill, minmax(420px, 1fr))` card grid

**Migration Obstacles** — bugs that appeared during implementation (not reasons for the task):

- Separate from `findings` (code review findings). These are runtime/integration bugs found while building.
- Color-coded by severity: P1=red, P2=amber, P3=blue
- Each card: severity badge + title + symptom + fix in 2-3 sentences
- Add this section when the task involved a non-trivial migration or infrastructure rewrite

**Technical Deep Dives** — for 2-4 hardest problems worth preserving in detail:

- Use `<details open>` for the most important one, `<details>` (collapsed) for the rest
- Structure per problem: Symptom → Investigation → Root cause (with code snippet) → Fix → Lesson
- Include actual before/after code snippets in `<pre>` blocks
- Worth adding when: the bug was non-obvious, investigation took >30min, or the fix has a counterintuitive reason

### Key requirements

- **Motivation block MUST appear at top.** Explain WHY the work was done, not just what.
  **Do not fabricate technical reasons** — read the spec/plan file for the real
  motivation. Common mistake: attributing "bundler X doesn't support Y" when the real
  reason is architectural.
- **File links carry line numbers.** ALL file references MUST link to GitHub with
  `#L<start>-L<end>`. Use Grep/Read to find actual line numbers for key functions.
- **Architecture diagram**: interactive SVG inside `<div class="arch-container">`. Every
  clickable SVG element (`<rect>`, `<g>`, etc.) MUST carry:
  - `data-href` — full GitHub URL to the source file (relative path also works)
  - `data-lines` — line range, e.g. `L10-L125`
  - `data-desc` — short description shown in tooltip

  `components.js` auto-creates hover tooltips pinned below the node (not
  mouse-following) with a "View source" link. Nodes must NOT be draggable.
  **Style reference:**
  - Primary boxes: `fill="url(#gMcp)"` with indigo gradient (`#4f46e5` at 0.3→0.08 opacity)
  - Service boxes: green tones (`#10b981` at 0.08-0.12 opacity)
  - External/agent boxes: amber tones (`#f59e0b` at 0.08-0.12 opacity)
  - Highlight boxes: red tones (`#ef4444` at 0.08 opacity) or blue (`#3b82f6`)
  - Borders: same color as fill at 0.3-0.5 opacity, `rx="6-12"` for rounded corners
  - Text: matching lighter color variants (e.g. `#a78bfa` for indigo, `#34d399` for green)
  - Arrows: `<marker>` with `fill="#888"`, lines at `stroke-opacity="0.5"`
  - Subtext/labels: `fill="#9ca3af"` at `font-size="10-11"`
- **Review details**: each round lists individual findings with severity, status, file
  link, and resolution. Findings expand on click.
- **Product-change file refs**: clickable file links with line numbers, never plain text.

## Step 5 — Update the landing page

Add a new entry to `index.html` between `<!-- REPORTS_START -->` and `<!-- REPORTS_END -->`:

```html
<li class="report-card">
  <a href="reports/HYP-XXX/">
    <div class="report-title">HYP-XXX: Task Title</div>
    <div class="report-meta">
      <span>YYYY-MM-DD</span>
      <span class="badge">feat|fix|refactor</span>
      <span>N files changed</span>
      <span>+X / −Y</span>
    </div>
  </a>
</li>
```

New entries go at the TOP of the list (newest first).

## Step 6 — Publish

```bash
cd /tmp/<reports-repo>
git add .
git commit -m "report: HYP-XXX — Task Title"
git push
```

If the reports repo has a CI validation script (HyperIDE's is
`scripts/ci/validate-reports.mjs` — structural HTML checks, local asset paths, a
headless-browser smoke render of `Report.init`, internal link resolution) it will run on
the push; treat a red run as a real defect in the page, not noise.

## Step 7 — Post links (mandatory, do not skip)

Post the report link to **both** the ticket and the PR immediately after publishing —
before verification, not as an afterthought.

```bash
# Ticket tracker (Linear CLI, not MCP)
linear issue comment HYP-XXX --body "📊 [Result Report](https://<org>.github.io/reports/HYP-XXX/)"
# or, if this project uses task-cli:
task change HYP-XXX --comment "📊 [Result Report](https://<org>.github.io/reports/HYP-XXX/)"

# GitHub PR
gh pr comment <PR_NUMBER> --body "📊 [Result Report](https://<org>.github.io/reports/HYP-XXX/)"
```

If either fails — retry once, then report the failure to the user. Do NOT silently skip.

## Step 8 — Verify

- Landing page loads, new report listed
- Report page renders correctly
- Motivation block visible at top
- Architecture diagram: tooltips pinned below nodes (not following the mouse), links clickable
- File links: all file references have `#L<N>-L<M>` line anchors
- Findings: filter by severity, expand on click, file links with lines
- Review section: rounds expandable, individual findings listed
- Timeline: commits clickable, tooltips visible
- Product-changes section: all three subsections with clickable file links

Report the URL to the user when done.
