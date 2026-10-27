/* Pure DOM metadata read. Never returns conversation text or changes the page. */
() => {
  /*@conversation-root*/
  /* A readable DOM is not a synchronized conversation. The page's own Resource Timing buffer is read
   * (never written, cleared or extended) for the newest completed same-origin read of THIS
   * conversation: exactly /backend-api/conversation/<id> or /backend-api/conversations/<id>, with no
   * query, hash or other path shape. A newer success supersedes an older failure; entries with a
   * non-HTTP status, an unfinished response or a foreign origin never count. No entries (or no
   * access to them) is "unproven", not a failure, so existing behaviour is kept.
   */
  const sync = (() => {
    const none = { status: null, ended_ms_ago: null, duration_ms: null };
    const id = (location.pathname.match(/\/c\/([A-Za-z0-9_-]+)\/?$/) || [])[1];
    if (!id) return none;
    let entries;
    try {
      entries = performance.getEntriesByType("resource");
    } catch {
      return none;
    }
    if (!Array.isArray(entries)) return none;
    const paths = ["conversation", "conversations"].map(
      (path) => location.origin + "/backend-api/" + path + "/" + id,
    );
    let latest = null;
    for (const entry of entries) {
      if (!entry || typeof entry.name !== "string" || entry.name.includes("#")) continue;
      // Identity comes only from the raw origin + exact path; a query (real reads carry num_turns
      // and include_has_versions) is allowed after it and never read. No URL normalisation, so dot
      // segments or encodings cannot alias another path.
      if (!paths.some((p) => entry.name === p || entry.name.startsWith(p + "?"))) continue;
      const status = entry.responseStatus,
        end = entry.responseEnd;
      if (!Number.isInteger(status) || status < 100 || status > 599) continue;
      if (typeof end !== "number" || !Number.isFinite(end) || end <= 0) continue;
      if (!latest || end >= latest.end) latest = { status, end, start: entry.startTime };
    }
    if (!latest) return none;
    const ago = Math.max(0, Math.round(performance.now() - latest.end));
    const duration = Number.isFinite(latest.start)
      ? Math.max(0, Math.round(latest.end - latest.start))
      : null;
    return { status: latest.status, ended_ms_ago: ago, duration_ms: duration };
  })();
  const syncFailed =
    sync.status === 429 || sync.status === 401 || sync.status === 403 || sync.status >= 500;
  const found = conversationRoot();
  if (syncFailed) {
    const reason =
      "HTTP " + sync.status + " on latest conversation read, ended " + sync.ended_ms_ago + "ms ago";
    return {
      title: document.title,
      error: "conversation-sync-http-" + sync.status,
      error_detail: found.root ? reason : reason + "; " + found.error,
      conversation_sync: sync,
      ...(found.root ? {} : { root_candidates: found.candidates }),
    };
  }
  // The app's own load-error boundary: a visible leaf in main with exactly the error phrase, outside
  // turns, navigation, composer and quoted content, on a page with no conversation turn at all.
  // Resource Timing may be gone after an in-app retry, so this does not depend on it.
  const loadFailed =
    document.querySelector("[data-turn-key]") === null &&
    all("main *").some(
      (el) =>
        el.children.length === 0 &&
        el.textContent.trim().replace(/\.$/, "") === "Could not load this ChatGPT conversation" &&
        visible(el) &&
        !el.closest(
          'nav, aside, [contenteditable], [data-composer-markdown], [data-user-message-bubble], [data-message-author-role], [data-markdown-copy], .markdown, [role="textbox"], textarea',
        ),
    );
  if (loadFailed)
    return {
      title: document.title,
      error: "conversation-load-failed",
      error_detail: "load-error-ui",
      conversation_sync: sync,
    };
  if (!found.root)
    return {
      title: document.title,
      error: "conversation-root-unverified",
      error_detail: found.error,
      root_candidates: found.candidates,
    };
  const root = found.root;
  // The user bubble can contain copy controls; only assistant-side nodes outside it count.
  const outside = (el) => !el.closest("[data-user-message-bubble]");
  const withText = (el) => el.textContent.trim().length > 0;
  const nodes = all("[data-turn-key]", root).slice(-100);
  const turns = nodes.map((t) => {
    const states = all("[data-talvt-turn-state]", t);
    const users = all('[data-user-message-bubble="true"]', t);
    // Older pages mark the assistant role; the current page has no role attribute and
    // marks the agent turn start, markdown output or sub-agent activity instead.
    const legacy = t.querySelector(
      '[data-message-author-role="assistant"], [data-chatgpt-search-unit-key$=":assistant"]',
    );
    const body = all('[data-markdown-copy], [data-testid="chatgpt-subagent-activity"]', t).some(
      (el) => outside(el) && withText(el),
    );
    const started = !!legacy || body || all("[data-chatgpt-agent-turn-start]", t).some(outside);
    return {
      id: t.getAttribute("data-turn-key"),
      user_go: users.length === 1 && users[0].textContent.trim() === "\u0433\u043e",
      assistant_state:
        states.length === 1 ? states[0].getAttribute("data-talvt-turn-state") : "unknown",
      generation_active:
        states.length === 1 && states[0].getAttribute("data-talvt-turn-state") === "in_progress",
      assistant_started: started,
      assistant_present: (!!legacy && withText(legacy)) || body,
    };
  });
  const latest = nodes.at(-1),
    last = turns.at(-1);
  // Only the newest, uniquely keyed, cancelled turn can be a failure; see thinkingFailure.
  const unique = !!last && !!last.id && turns.filter((t) => t.id === last.id).length === 1;
  const cancelled = last?.assistant_state === "cancelled";
  const failure = latest ? thinkingFailure(latest) : { count: 0, shape: "" };
  const failed = unique && cancelled && failure.count === 1;
  const failureEvidence =
    failure.count > 1
      ? "ambiguous-status"
      : failure.count === 0
        ? ""
        : !unique
          ? "duplicate-turn"
          : !cancelled
            ? "not-cancelled"
            : failure.shape;
  // A Stop button elsewhere (a floating composer) is not this conversation running.
  const stopVisible = all('button[aria-label="Stop"]', root.closest("main")).some(visible);
  const running = last?.assistant_state === "in_progress" || stopVisible;
  let status = running
    ? "working"
    : last?.assistant_state === "complete"
      ? "complete"
      : failed
        ? "Thinking failed"
        : "unknown";
  const alerts = [...document.querySelectorAll('[role="alert"]')].filter(
    (x) => !x.closest("[data-turn-key]"),
  );
  const restricted = alerts.some((x) =>
    /quota|limit reached|too many requests|429/i.test(x.textContent),
  );
  if (restricted) status = "quota-or-rate-limit";
  // Elapsed-time labels are not evidence of task progress.
  const content = (latest ? latest.textContent : "")
    .replace(
      /\b\d+(?:[.,]\d+)?\s*(?:milliseconds?|seconds?|minutes?|hours?|ms|s|m|h)\b/gi,
      "<duration>",
    )
    .replace(/\b\d{1,2}:\d{2}(?::\d{2})?\b/g, "<clock>");
  let h = 2166136261;
  for (let i = 0; i < content.length; i++) {
    h ^= content.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return {
    title: document.title,
    conversation_id: found.id,
    root_match: found.match,
    turn: last?.id || "",
    turns,
    running,
    stop_visible: stopVisible,
    status,
    failure_evidence: failureEvidence,
    progress_token: JSON.stringify([last?.id, last?.assistant_state, content.length, h >>> 0]),
    model:
      document.querySelector('button[aria-label="Select ChatGPT model"]')?.textContent.trim() || "",
    effort: "",
    restricted,
    turn_window: 100,
    conversation_sync: sync,
  };
};
