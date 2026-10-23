/* Pure DOM metadata read. Never returns conversation text or changes the page. */
() => {
  /*@conversation-root*/
  const found = conversationRoot();
  if (!found.root) return {title:document.title, error:'conversation-root-unverified', error_detail:found.error, root_candidates:found.candidates};
  const root = found.root;
  // The user bubble can contain copy controls; only assistant-side nodes outside it count.
  const outside = el => !el.closest('[data-user-message-bubble]');
  const withText = el => el.textContent.trim().length > 0;
  const nodes = all('[data-turn-key]', root).slice(-100);
  const turns = nodes.map(t => {
    const states = all('[data-talvt-turn-state]', t);
    const users = all('[data-user-message-bubble="true"]', t);
    // Older pages mark the assistant role; the current page has no role attribute and
    // marks the agent turn start, markdown output or sub-agent activity instead.
    const legacy = t.querySelector('[data-message-author-role="assistant"], [data-chatgpt-search-unit-key$=":assistant"]');
    const body = all('[data-markdown-copy], [data-testid="chatgpt-subagent-activity"]', t).some(el => outside(el) && withText(el));
    const started = !!legacy || body || all('[data-chatgpt-agent-turn-start]', t).some(outside);
    return {id:t.getAttribute('data-turn-key'), user_go:users.length===1 && users[0].textContent.trim()==='\u0433\u043e',
      assistant_state:states.length===1 ? states[0].getAttribute('data-talvt-turn-state') : 'unknown',
      generation_active:states.length===1 && states[0].getAttribute('data-talvt-turn-state')==='in_progress',
      assistant_started:started,
      assistant_present:(!!legacy && withText(legacy)) || body};
  });
  const latest = nodes.at(-1), last = turns.at(-1);
  // Only the newest, uniquely keyed, cancelled turn can be a failure; see thinkingFailure.
  const unique = !!last && !!last.id && turns.filter(t=>t.id===last.id).length===1;
  const cancelled = last?.assistant_state==='cancelled';
  const failure = latest ? thinkingFailure(latest) : {count:0, shape:''};
  const failed = unique && cancelled && failure.count===1;
  const failureEvidence = failure.count>1 ? 'ambiguous-status' : failure.count===0 ? '' : !unique ? 'duplicate-turn' : !cancelled ? 'not-cancelled' : failure.shape;
  // A Stop button elsewhere (a floating composer) is not this conversation running.
  const stopVisible = all('button[aria-label="Stop"]', root.closest('main')).some(visible);
  const running = last?.assistant_state==='in_progress' || stopVisible;
  let status = running ? 'working' : last?.assistant_state==='complete' ? 'complete' : failed ? 'Thinking failed' : 'unknown';
  const alerts = [...document.querySelectorAll('[role="alert"]')].filter(x=>!x.closest('[data-turn-key]'));
  const restricted = alerts.some(x=>/quota|limit reached|too many requests|429/i.test(x.textContent));
  if (restricted) status='quota-or-rate-limit';
  // Elapsed-time labels are not evidence of task progress.
  const content = (latest ? latest.textContent : '')
    .replace(/\b\d+(?:[.,]\d+)?\s*(?:milliseconds?|seconds?|minutes?|hours?|ms|s|m|h)\b/gi,'<duration>')
    .replace(/\b\d{1,2}:\d{2}(?::\d{2})?\b/g,'<clock>');
  let h=2166136261;
  for(let i=0;i<content.length;i++) { h^=content.charCodeAt(i); h=Math.imul(h,16777619); }
  return {title:document.title, conversation_id:found.id, root_match:found.match, turn:last?.id || '', turns, running, stop_visible:stopVisible, status,
    failure_evidence:failureEvidence,
    progress_token:JSON.stringify([last?.id,last?.assistant_state,content.length,h>>>0]),
    model:document.querySelector('button[aria-label="Select ChatGPT model"]')?.textContent.trim() || '',
    effort:'', restricted, turn_window:100};
}
