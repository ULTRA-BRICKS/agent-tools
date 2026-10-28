/* Shared by monitor_dom.js and live_dom.js; scripts.py splices it in at their marker comment.
 * One contract decides which element is "this page's conversation":
 * exactly one visible root inside main (hidden clones are ignored), whose selection attribute is either the legacy
 * literal `true` or the conversation id of the current /c/<id> URL. Anything else
 * (no root, several roots, another conversation's id) is refused, never guessed.
 */
const all = (s, root = document) => [...root.querySelectorAll(s)];
const visible = el => {
  if (!el?.isConnected || !el.getClientRects().length) return false;
  for (let p = el; p; p = p.parentElement) {
    const s = getComputedStyle(p);
    if (p.hidden || p.getAttribute('aria-hidden') === 'true' || s.display === 'none' || s.visibility === 'hidden' || Number(s.opacity) === 0) return false;
  }
  return true;
};
const rootSelector = 'main [data-chatgpt-conversation-selection-target][data-thread-find-target="conversation"]';
const conversationRoot = () => {
  const id = (location.pathname.match(/\/c\/([A-Za-z0-9_-]+)\/?$/) || [])[1] || '';
  const candidates = all(rootSelector);
  // Hidden clones (display:none, hidden/aria-hidden ancestors, no client rects) are never authority;
  // they are dropped before counting, so only visible roots can make the page ambiguous.
  const shown = candidates.filter(visible);
  const refuse = error => ({root: null, error, id, candidates: candidates.length, visible: shown.length, match: ''});
  if (!id) return refuse('conversation-id-missing');
  if (candidates.length === 0) return refuse('root-missing');
  if (shown.length === 0) return refuse('root-not-visible');
  if (shown.length > 1) return refuse('root-ambiguous');
  const value = shown[0].getAttribute('data-chatgpt-conversation-selection-target');
  if (value !== 'true' && value !== id) return refuse('root-attribute-mismatch');
  return {root: shown[0], error: '', id, candidates: candidates.length, visible: 1, match: value === 'true' ? 'legacy' : 'conversation-id'};
};
/* One structural definition of the UI status `Thinking failed`, shared by the monitor and the
 * recovery adapter so they cannot disagree about what a failed turn looks like.
 * Two UI shapes are accepted: the legacy `div.group/activity-header` and the current thinking
 * `button[aria-expanded=false|true]`. Either way the status is a visible leaf SPAN, outside
 * the user bubble, markdown output and sub-agent output: message text quoting the phrase is content.
 * Returns {count, shape}; shape is only set when exactly one status node qualifies.
 */
const failureContent = '[data-user-message-bubble], [data-message-author-role="user"], [data-markdown-copy], .markdown, [data-testid="chatgpt-subagent-activity"]';
const thinkingFailure = turn => {
  const shapeOf = span => {
    const header = span.closest('div.group\\/activity-header');
    if (header && turn.contains(header)) return 'activity-header';
    const button = span.closest('button[aria-expanded="false"], button[aria-expanded="true"]');
    if (!button || !turn.contains(button)) return '';
    return button.getAttribute('aria-expanded') === 'true' ? 'expanded-button' : 'collapsed-button';
  };
  const found = all('span', turn)
    .filter(el => el.children.length === 0 && el.textContent.trim() === 'Thinking failed' && visible(el) && !el.closest(failureContent))
    .map(shapeOf).filter(Boolean);
  return {count: found.length, shape: found.length === 1 ? found[0] : ''};
};
/* The native platform approval card: exactly one VISIBLE `data-codex-approval-surface` div in the
 * whole conversation, inside the latest turn, holding exactly one visible form (its direct child)
 * with a visible button. A lookalike inside a user bubble, markdown output or a quote is content,
 * never a card; a hidden responsive copy is ignored; a second visible card (any turn) is ambiguous.
 * It only reports that structure; approval.py decides whether the page may be called paused.
 */
const approvalLookalike = failureContent + ', blockquote, q, pre, code';
const platformApprovalCard = (root, latest) => {
  const cards = all('[data-codex-approval-surface]').filter(el => visible(el) && !el.closest(approvalLookalike));
  if (cards.length !== 1) return false;
  const card = cards[0];
  if (!root.contains(card) || !latest.contains(card)) return false;
  const forms = all('form', card).filter(visible);
  return forms.length === 1 && forms[0].parentElement === card && all('button', forms[0]).some(visible);
};
/* Passive card facts for approval.merge, evaluated in the SAME page.evaluate as live_dom.js
 * (approval.observe_script), so both describe one instant. Read-only: no click, focus or
 * navigation, and no page text leaves the page, only booleans and the latest turn key.
 */
const approvalFacts = () => {
  const root = conversationRoot().root;
  const latest = root ? all('[data-turn-key]', root).at(-1) : null;
  return {
    card: !!latest && visible(latest) && platformApprovalCard(root, latest),
    stop: all('button[aria-label="Stop"]').some(visible),
    turn: latest ? latest.getAttribute('data-turn-key') : '',
  };
};
