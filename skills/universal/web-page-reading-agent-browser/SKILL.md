---
name: web-page-reading-agent-browser
description: Use when reading a web page, documentation, or an API reference — ESPECIALLY a long page or JS-rendered content. Prefer the `agent-browser` CLI (`open` + `get text body` / `eval`) over a fetch-and-summarize tool, which truncates long pages and silently drops content. Also covers DOM inspection, extracting one section, screenshots, and browser automation.
---

# Read web pages with agent-browser, not a truncating fetch tool

A built-in "fetch this URL and summarize it" tool is convenient but lossy: it
**truncates long pages** and renders only static HTML, so JS-rendered docs come
back empty. The failure is silent — you get *a* answer that looks complete and
miss the part of the page that was cut off. This bites hardest on exactly the
pages you most need to read in full: API references, framework docs, long
changelogs, anything past the first screen.

`agent-browser` (a real headless Chrome via CDP) loads the whole page, runs its
JavaScript, and hands you the complete text or any slice you ask for. It's a
standalone third-party CLI ([vercel-labs/agent-browser](https://github.com/vercel-labs/agent-browser)),
installed separately — `npm i -g agent-browser && agent-browser install` (or
`cargo install agent-browser && agent-browser install`).

> **Read `agent-browser-concurrency` too, before running any command below.**
> You can't tell whether another agent or a human is also using
> `agent-browser` on this machine, so plain `agent-browser open` (the shared
> `default` session) risks colliding with them — including another agent
> reading a different page at the same time. Run this at the start of
> **every** command for this read (identical whether it's your first call or
> a later one — it reuses the session it already created, or creates one if
> this is the first call). Replace `<your-task-label>` in both places with
> something that distinguishes this read from a sibling task's, in case one
> happens to run alongside it in the same worktree:
>
> ```bash
> raw='<your-task-label>'
> label="$(printf '%s' "$raw" | LC_ALL=C tr -c 'A-Za-z0-9._-' '_')_$(printf '%s' "$raw" | cksum | cut -d' ' -f1)"
> d="$(git rev-parse --absolute-git-dir 2>/dev/null)" || { d="${TMPDIR:-/tmp}/agent-browser-$(id -u)"; mkdir -m 700 -p "$d" 2>/dev/null; }
> f="$d/agent-browser-session.$label"
> umask 077
> ( set -C; echo "$label-$(date +%s)-$RANDOM-$$" > "$f" ) 2>/dev/null
> s=""; for _ in 1 2 3 4 5; do s="$(cat "$f" 2>/dev/null)"; [ -n "$s" ] && break; sleep 0.05; done
> if [ -z "$s" ] && [ -e "$f" ]; then
>   rm -f "$f"; ( set -C; echo "$label-$(date +%s)-$RANDOM-$$" > "$f" ) 2>/dev/null
>   s="$(cat "$f" 2>/dev/null)"
> fi
> if [ -n "$s" ]; then export AGENT_BROWSER_SESSION="$s"
> else unset AGENT_BROWSER_SESSION; echo "no session name for label '$label' — not exporting a session var" >&2
> fi
> ```
>
> (None of this is decoration — `tr` alone can collapse two different labels
> to the same filename (the checksum suffix fixes that), a plain
> `[ -s "$f" ] || echo … > "$f"` is a check-then-act race between two
> parallel Bash calls (`set -C` fixes that), and even with `set -C` a reader
> can land in the instant between file creation and the write landing (the
> retry loop closes that window). See `agent-browser-concurrency` Rule 1 for
> the full reasoning on all three, plus the per-uid temp directory.)
>
> Every plain `agent-browser` command below then uses it automatically — no
> per-command flag needed, including in the examples that follow, exactly as
> written. **Close when you're done reading** — unlike the setup snippet
> above, don't regenerate the name if the file is already gone; that would
> "close" a session that was never opened and hide the fact that nothing was
> actually cleaned up:
>
> ```bash
> raw='<your-task-label>'
> label="$(printf '%s' "$raw" | LC_ALL=C tr -c 'A-Za-z0-9._-' '_')_$(printf '%s' "$raw" | cksum | cut -d' ' -f1)"
> d="$(git rev-parse --absolute-git-dir 2>/dev/null)" || d="${TMPDIR:-/tmp}/agent-browser-$(id -u)"
> f="$d/agent-browser-session.$label"
> s="$(cat "$f" 2>/dev/null)"
> if [ -n "$s" ]; then
>   export AGENT_BROWSER_SESSION="$s"
>   agent-browser close && rm -f "$f"
> else
>   echo "no session recorded for label '$label' — nothing to close" >&2
> fi
> ```
>
> See `agent-browser-concurrency` for the full contract (why a file instead
> of remembering a value, older CLI versions, retrying after a crash).

## Rule

- **Reading a web page / docs / API reference → reach for `agent-browser`,** not
  a fetch-and-summarize tool. Open the page, wait for it to settle, then pull text:

  ```bash
  agent-browser open "https://core.telegram.org/bots/api"
  agent-browser wait --load networkidle     # let JS-rendered content settle first
  ```

  > **Don't blindly dump a huge page to stdout.** `agent-browser get text body`
  > returns the *whole* page (a real API doc is easily 400 KB+). `agent-browser`
  > itself won't truncate it, but **your own tool/shell output limit will** — and
  > then you're back to a silent partial read, the exact failure this skill exists
  > to prevent. For anything large, extract a bounded slice (below) or save the
  > full text to a file and read sections from it:
  >
  > ```bash
  > agent-browser get text body > /tmp/page.txt   # full text, no stdout truncation
  > wc -l /tmp/page.txt && grep -n "sendMessage" /tmp/page.txt
  > ```

  For a short page, or a known small container, reading directly is fine —
  `get text` needs a selector, so scope it:

  ```bash
  agent-browser get text "#dev_page_content"     # one container, not the whole DOM
  ```

  > Also: `agent-browser` has a `--max-output <chars>` flag (and
  > `AGENT_BROWSER_MAX_OUTPUT` env) that caps page output. It's off by default, but
  > if you've set `AGENT_BROWSER_MAX_OUTPUT` globally, **unset it** (and don't pass
  > `--max-output`) when you need the complete page — a non-empty cap, `0` included,
  > truncates at the source.

- **Want just one section, not the whole page? (the default for big docs)**
  Extract it with `eval` so you read only what you need and never overrun an
  output limit:

  ```bash
  agent-browser open "https://core.telegram.org/bots/api"
  agent-browser wait --load networkidle
  # Heading text + everything under it up to the next heading at the SAME or
  # HIGHER level (so an h3's nested h4 subsections are kept, not cut off).
  # Assumes a flat doc where headings and content are DOM siblings (most API
  # references). If sections are wrapped in <section>/<article> containers,
  # scope to that container instead, e.g. h.closest("section").textContent.
  agent-browser eval '
    const h = [...document.querySelectorAll("h1,h2,h3,h4,h5,h6")]
      .find(e => e.textContent.includes("sendMessage"));
    if (!h) throw new Error("heading not found");
    const lvl = +h.tagName[1];
    let out = h.textContent + "\n", n = h.nextElementSibling;
    const stop = e => /^H[1-6]$/.test(e.tagName) && +e.tagName[1] <= lvl;
    while (n && !stop(n)) { out += n.textContent + "\n"; n = n.nextElementSibling; }
    out;
  '
  ```

- **Inspect structure** before extracting: `agent-browser snapshot` (the full
  accessibility tree — includes headings and text) or `snapshot -s "#main"` to
  scope it. (Reserve `snapshot -i`, interactive-elements-only, for clicking/filling
  workflows — on a static doc it hides the headings you're trying to find.) Or read
  raw markup with `agent-browser get html <selector>`.

- **Need a picture of the rendered page** (a chart, a layout, a diagram):
  `agent-browser screenshot out.png` — it renders JS, a static fetch can't.

- **Anything interactive** — log in, click through, fill a form, paginate to load
  more content, then read — is also `agent-browser`. Snapshot, then act on refs
  (`click @e3`, `fill @e2 "..."`, `press Enter`), or use semantic locators
  (`find role button click --name "Load more"`, `find text "Next" click`). A fetch
  tool can only read one static response.

- **A fetch-and-summarize tool is still fine** for a short, static page or a raw
  JSON/text endpoint where truncation and JS-rendering aren't a concern.

- **Close the session when your read is done**, using the full close snippet
  near the top of this skill (setup lines included, not just the bare
  `close`). Skipping this leaves a Chrome process tree running — see
  `agent-browser-concurrency` Rule 2 for how long it lingers before an idle
  timeout reclaims it.

## Learn it properly before using it

`agent-browser` ships its own version-matched skills — read the core guide once
instead of guessing flags:

```bash
agent-browser skills get core          # overview + common patterns
agent-browser skills get core --full   # full command reference + templates
agent-browser skills list              # specialized: electron, slack, dogfood, ...
```

## Why

The expensive failure mode isn't "the tool errored" — it's "the tool returned a
plausible answer from the top of a page and you never learned the rest existed."
A truncated API doc that omits the parameter you needed costs a wrong
implementation and a debugging session. Loading the real page in a real browser
gets the *complete* content into reach. But note the truncation can bite at two
points — at the source (a fetch tool, or `--max-output`) **and** downstream (your
own tool/shell output cap when you dump a 400 KB page to stdout) — so the win is
only real if you also read it in bounded pieces: a scoped selector, an `eval`
slice, or full text saved to a file and grepped. Then the same tool also covers
DOM inspection, screenshots, and full automation — one tool for everything web.

Pairs with `semantic-code-search` (prefer the index-backed tool over grep + read
whole file): same principle, a purpose-built reader beats the lossy default.
