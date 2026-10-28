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
  const noRead = { status: null, ended_ms_ago: null, duration_ms: null };
  const resourceEntries = () => {
    try {
      const entries = performance.getEntriesByType("resource");
      return Array.isArray(entries) ? entries : [];
    } catch {
      return [];
    }
  };
  const validStatus = (status) => Number.isInteger(status) && status >= 100 && status <= 599;
  const validEnd = (end) => typeof end === "number" && Number.isFinite(end) && end > 0;
  // Identity comes only from the raw origin + exact path; a query (real reads carry num_turns
  // and include_has_versions) is allowed after it and never read. No URL normalisation, so dot
  // segments or encodings cannot alias another path.
  const ownRead = (entry, paths) =>
    !!entry &&
    typeof entry.name === "string" &&
    !entry.name.includes("#") &&
    paths.some((p) => entry.name === p || entry.name.startsWith(p + "?"));
  const completedRead = (entry) =>
    validStatus(entry.responseStatus) && validEnd(entry.responseEnd)
      ? { status: entry.responseStatus, end: entry.responseEnd, start: entry.startTime }
      : null;
  const newestRead = (latest, read) =>
    read && (!latest || read.end >= latest.end) ? read : latest;
  const readSync = () => {
    const id = (location.pathname.match(/\/c\/([A-Za-z0-9_-]+)\/?$/) || [])[1];
    if (!id) return noRead;
    const paths = ["conversation", "conversations"].map(
      (path) => location.origin + "/backend-api/" + path + "/" + id,
    );
    let latest = null;
    for (const entry of resourceEntries()) {
      if (ownRead(entry, paths)) latest = newestRead(latest, completedRead(entry));
    }
    if (!latest) return noRead;
    const ago = Math.max(0, Math.round(performance.now() - latest.end));
    const duration = Number.isFinite(latest.start)
      ? Math.max(0, Math.round(latest.end - latest.start))
      : null;
    return { status: latest.status, ended_ms_ago: ago, duration_ms: duration };
  };
  const sync = readSync();
  const syncFailedStatus = (status) =>
    status === 429 || status === 401 || status === 403 || status >= 500;
  const found = conversationRoot();
  const syncFailureResult = () => {
    const reason =
      "HTTP " + sync.status + " on latest conversation read, ended " + sync.ended_ms_ago + "ms ago";
    return {
      title: document.title,
      error: "conversation-sync-http-" + sync.status,
      error_detail: found.root ? reason : reason + "; " + found.error,
      conversation_sync: sync,
      ...(found.root ? {} : { root_candidates: found.candidates }),
    };
  };
  if (syncFailedStatus(sync.status)) return syncFailureResult();
  // The app's own load-error boundary: a visible leaf in main with exactly the error phrase, outside
  // turns, navigation, composer and quoted content, on a page with no conversation turn at all.
  // Resource Timing may be gone after an in-app retry, so this does not depend on it.
  const loadErrorShown = () =>
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
  if (loadErrorShown())
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
  const assistantEvidence = (t) => {
    // Older pages mark the assistant role; the current page has no role attribute and
    // marks the agent turn start, markdown output or sub-agent activity instead.
    const legacy = t.querySelector(
      '[data-message-author-role="assistant"], [data-chatgpt-search-unit-key$=":assistant"]',
    );
    const body = all('[data-markdown-copy], [data-testid="chatgpt-subagent-activity"]', t).some(
      (el) => outside(el) && withText(el),
    );
    const started = !!legacy || body || all("[data-chatgpt-agent-turn-start]", t).some(outside);
    const present = (!!legacy && withText(legacy)) || body;
    // Assistant-side media is the only non-text answer. It is read only for a turn without a text
    // answer, so a long answered conversation never pays for the scan.
    const media =
      !present && all("img, picture, video, audio, canvas, iframe, object, embed", t).some(outside);
    return { started, present, media };
  };
  const readTurn = (t) => {
    const states = all("[data-talvt-turn-state]", t);
    const users = all('[data-user-message-bubble="true"]', t);
    const { started, present, media } = assistantEvidence(t);
    return {
      id: t.getAttribute("data-turn-key"),
      user_go: users.length === 1 && users[0].textContent.trim() === "\u0433\u043e",
      assistant_state:
        states.length === 1 ? states[0].getAttribute("data-talvt-turn-state") : "unknown",
      generation_active:
        states.length === 1 && states[0].getAttribute("data-talvt-turn-state") === "in_progress",
      assistant_started: started,
      assistant_present: present,
      assistant_media: media,
      // Structural proof that this turn holds a user message and no assistant side at all.
      assistant_absent: users.length > 0 && !started && !present && !media,
    };
  };
  const nodes = all("[data-turn-key]", root).slice(-100);
  const turns = nodes.map(readTurn);
  const latest = nodes.at(-1),
    last = turns.at(-1);
  // Only the newest, uniquely keyed, cancelled turn can be a failure; see thinkingFailure.
  const uniqueTurn = () =>
    !!last && !!last.id && turns.filter((t) => t.id === last.id).length === 1;
  const unique = uniqueTurn();
  const cancelled = last?.assistant_state === "cancelled";
  const failure = latest ? thinkingFailure(latest) : { count: 0, shape: "" };
  const failed = unique && cancelled && failure.count === 1;
  const failureEvidenceOf = () => {
    if (failure.count > 1) return "ambiguous-status";
    if (failure.count === 0) return "";
    if (!unique) return "duplicate-turn";
    return cancelled ? failure.shape : "not-cancelled";
  };
  // A Stop button elsewhere (a floating composer) is not this conversation running.
  const stopVisible = all('button[aria-label="Stop"]', root.closest("main")).some(visible);
  const runningTurn = () => last?.assistant_state === "in_progress" || stopVisible;
  const running = runningTurn();
  // The complete marker alone is not an answer. An answer is assistant text, or assistant media
  // inside a started assistant side (the same text evidence the continuation correlation needs).
  // A newest turn with only the user's message is `unanswered` (a valid observation, never an
  // error); a turn whose lack of an answer cannot be proven stays `unknown`.
  const completionOf = () => {
    if (last?.assistant_present || (last?.assistant_started && last?.assistant_media))
      return "complete";
    return unique && last?.assistant_absent ? "unanswered" : "unknown";
  };
  const turnStatus = () => {
    if (running) return "working";
    if (last?.assistant_state === "complete") return completionOf();
    return failed ? "Thinking failed" : "unknown";
  };
  const quotaAlertShown = () =>
    [...document.querySelectorAll('[role="alert"]')]
      .filter((x) => !x.closest("[data-turn-key]"))
      .some((x) => /quota|limit reached|too many requests|429/i.test(x.textContent));
  const restricted = quotaAlertShown();
  // A native approval card is the platform waiting for a human, not generation. Claimed only on
  // the same proof the recovery adapter uses (one visible card in the latest, uniquely keyed,
  // in_progress turn, no Stop) and only when nothing else owns the page. Any doubt keeps `working`.
  const composerSelector =
    '[contenteditable="true"][data-composer-markdown][role="textbox"][aria-label="Ask ChatGPT"]';
  const humanDraft = () =>
    all(composerSelector).some((el) => visible(el) && !!el.textContent) ||
    all('[data-testid="attachment"], [data-testid="attachment-preview"], input[type="file"]').some(
      (el) => visible(el) && (el.tagName !== "INPUT" || el.files?.length),
    );
  const pageClaimed = () =>
    humanDraft() ||
    all('[role="dialog"], [role="alertdialog"]').some(visible) ||
    all('[role="alert"]').some((el) => visible(el) && !el.closest("[data-turn-key]")) ||
    all('iframe[src*="captcha"], iframe[src*="challenges.cloudflare"], a[href^="/auth/"]').some(
      visible,
    ) ||
    /verify you are human|checking your browser|just a moment/i.test(document.title) ||
    !all('button[aria-label="Open profile menu"]').some(visible);
  const awaitingApproval = () => {
    if (!running || stopVisible || restricted || failure.count !== 0) return false;
    if (!unique || last?.assistant_state !== "in_progress") return false;
    const facts = approvalFacts();
    return facts.card && !facts.stop && facts.turn === last.id && !pageClaimed();
  };
  const approvalWait = awaitingApproval();
  const status = restricted ? "quota-or-rate-limit" : turnStatus();
  // Elapsed-time labels are not evidence of task progress.
  const content = (latest ? latest.textContent : "")
    .replace(
      /\b\d+(?:[.,]\d+)?\s*(?:milliseconds?|seconds?|minutes?|hours?|ms|s|m|h)\b/gi,
      "<duration>",
    )
    .replace(/\b\d{1,2}:\d{2}(?::\d{2})?\b/g, "<clock>");
  const fnv = (text) => {
    let h = 2166136261;
    for (let i = 0; i < text.length; i++) {
      h ^= text.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return h >>> 0;
  };
  return {
    title: document.title,
    conversation_id: found.id,
    root_match: found.match,
    turn: last?.id || "",
    turns,
    running: running && !approvalWait,
    stop_visible: stopVisible,
    status: approvalWait ? "awaiting-approval" : status,
    ...(approvalWait ? { approval_wait: true } : {}),
    failure_evidence: failureEvidenceOf(),
    progress_token: JSON.stringify([last?.id, last?.assistant_state, content.length, fnv(content)]),
    model:
      document.querySelector('button[aria-label="Select ChatGPT model"]')?.textContent.trim() || "",
    effort: "",
    restricted,
    turn_window: 100,
    conversation_sync: sync,
  };
};
