/* Guardian DOM contract: explicit account-verified UI selectors, never prose matching.
 * Accessed by PlaywrightAdapter for one synchronous snapshot/fill/check/click.
 * Missing/duplicate identity evidence fails closed; no cookies or tokens are read.
 */
({contract: c, expected = null, action = 'observe', ownedDraft = '', deadline = 0, render = null}) => {
  // Python supplies the deadline before evaluate; queued work cannot renew it.
  const timely = () => Number.isFinite(deadline) && performance.timeOrigin + performance.now() < deadline;
  const visible = el => {
    if (!el || !el.isConnected || !el.getClientRects().length) return false;
    for (let parent = el; parent; parent = parent.parentElement) {
      const style = getComputedStyle(parent);
      if (style.visibility === 'hidden' || style.display === 'none' || Number(style.opacity) === 0) return false;
    }
    return true;
  };
  const all = (selector, root = document) => selector ? [...root.querySelectorAll(selector)] : [];
  const one = (selector, root = document) => {
    const matches = all(selector, root).filter(visible);
    return matches.length === 1 ? matches[0] : null;
  };
  const chatIdentity = () => /^https:\/\/chatgpt\.com\/c\/[a-zA-Z0-9_-]+\/?$/.test(location.href.split(/[?#]/)[0])
    ? location.href.split(/[?#]/)[0].replace(/\/$/, '') : '';
  const snapshot = (ownDraft = '', api = false) => {
    const chat = chatIdentity();
    const out = {chat: api && location.origin === 'https://chatgpt.com' && location.pathname === '/' ? 'https://chatgpt.com/' : chat, turn:'', generation:'', status:'', active:false, verified:false,
      model:'', effort:'', running:false, draft:false, challenge:false, ambiguous:true, quota:false};
    out.draft = all(c.composer).filter(visible).some(el => {
      const text = el.tagName === 'TEXTAREA' ? el.value : el.textContent;
      return !!text && text !== ownDraft;
    }) || all(c.attachments).some(visible);
    if (!c.verified_account_ui) return out;
    const root = one(c.conversation);
    const composer = one(c.composer);
    const model = one(c.model);
    const auth = one(c.logged_in);
    out.challenge = all(c.challenge).some(visible) ||
      all('iframe[src*="captcha"],iframe[src*="challenges.cloudflare"], [role="dialog"]').some(visible) ||
      /verify you are human|checking your browser|just a moment/i.test(document.title);
    out.running = all(c.running).some(visible);
    out.quota = all(c.astra_quota).some(visible);
    if (!out.chat || !root || !composer || !model || !auth) return out;
    out.model = (model.innerText || '').trim();
    const effort = one(c.effort);
    out.effort = effort ? (effort.innerText || '').trim() : '';
    const text = composer.tagName === 'TEXTAREA' ? composer.value : composer.textContent;
    out.draft = !!text && text !== ownDraft;
    out.draft ||= all(c.attachments).some(visible);
    out.verified = ['Astra','Sol'].includes(out.model) &&
      (out.model !== 'Sol' || out.effort === 'Extra High');
    if (api) {
      out.ambiguous = !out.verified || !one(c.send);
      return out;
    }
    const turns = all(c.turn, root);
    const latest = turns.at(-1);
    if (!latest || !visible(latest)) return out;
    const ids = turns.map(el => el.getAttribute(c.turn_id_attribute));
    if (ids.some(id => !id) || new Set(ids).size !== ids.length) return out;
    out.turn = latest.getAttribute(c.turn_id_attribute) || '';
    out.generation = latest.getAttribute(c.generation_attribute) || '';
    const statuses = all(c.status, latest).filter(visible);
    const status = statuses.length === 1 ? statuses[0] : null;
    const bodies = all(c.body, latest);
    if (bodies.length !== 1 || !visible(bodies[0])) return out;
    const prose = status && bodies.some(body => body === status || body.contains(status));
    out.status = status && !prose ? (status.innerText || '').trim() : '';
    out.active = !!status && !prose && status.getAttribute('role') === 'status' &&
      status.getAttribute(c.active_attribute) === c.active_value &&
      latest.getAttribute(c.author_attribute) === 'assistant';
    out.verified = ['Astra','Sol'].includes(out.model) &&
      (out.model !== 'Sol' || out.effort === 'Extra High');
    out.ambiguous = !out.generation || !one(c.send) || statuses.length > 1;
    return out;
  };
  const same = (a,b) => Object.keys(a).every(key => a[key] === b[key]);
  const safe = o => o.chat && o.turn && o.generation && o.status === 'Thinking failed' &&
    o.active && o.verified && !o.running && !o.draft && !o.challenge && !o.ambiguous && (!o.quota || (o.model === 'Sol' && o.effort === 'Extra High'));
  const observed = snapshot();
  if (action === 'reply-baseline' || action === 'reply-identity' || action === 'reply') {
    const root = one(c.conversation);
    const turns = root ? all(c.turn, root) : [];
    const ids = turns.map(el => el.getAttribute(c.turn_id_attribute));
    if (!root || ids.some(id => !id) || new Set(ids).size !== ids.length) return null;
    const safety = snapshot('', true);
    const markdown = action === 'reply' && !safety.running && turns.length ? render(turns.at(-1)) : '';
    if (typeof markdown !== 'string') return null;
    return {chat: safety.chat, running: safety.running, challenge: safety.challenge,
      quota: safety.quota, markdown, turns: turns.map(el => ({
      turn: el.getAttribute(c.turn_id_attribute),
      generation: el.getAttribute(c.generation_attribute) || '',
      author: el.getAttribute(c.author_attribute) || '',
      text: one(c.body, el)?.innerText || '',
    }))};
  }
  if (action === 'observe') return observed;
  if (action === 'observe-api') return snapshot(ownedDraft, true);
  if (action === 'ack') {
    const root = one(c.conversation);
    const turns = root ? all(c.turn,root) : [];
    if (chatIdentity() !== expected.chat) return false;
    const ids = turns.map(el => el.getAttribute(c.turn_id_attribute)).filter(Boolean);
    if (new Set(ids).size !== ids.length) return false;
    const failed = turns.findIndex(el => el.getAttribute(c.turn_id_attribute) === expected.turn &&
      el.getAttribute(c.generation_attribute) === expected.generation &&
      el.getAttribute(c.author_attribute) === 'assistant');
    const user = failed >= 0 ? turns[failed + 1] : null;
    const body = user ? one(c.body,user) : null;
    return !!user && !!body && user.getAttribute(c.author_attribute) === 'user' &&
      !!user.getAttribute(c.turn_id_attribute) && user.getAttribute(c.turn_id_attribute) !== expected.turn &&
      (body.innerText || '') === 'го';
  }
  if (action === 'prepare-api') {
    const current = snapshot('', true);
    const composer = one(c.composer);
    if (!expected || !current.verified || current.ambiguous || current.running || current.draft || current.challenge ||
        (current.quota && current.model !== 'Sol') || !same(current,expected) || !composer || !ownedDraft) return {blocked: current};
    if (!timely()) return {blocked: current};
    if (composer.tagName === 'TEXTAREA') {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(composer,ownedDraft);
    } else if (composer.isContentEditable) composer.textContent = ownedDraft;
    else return {blocked: current};
    composer.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:ownedDraft}));
    const after = snapshot(ownedDraft, true);
    const currentComposer = one(c.composer);
    const text = currentComposer ? (currentComposer.tagName === 'TEXTAREA' ? currentComposer.value : currentComposer.textContent) : '';
    return (composer.isConnected && currentComposer === composer && text === ownedDraft && same(after,expected) && timely()) || {blocked: after};
  }
  if (action === 'send-api') {
    const current = snapshot(ownedDraft, true);
    const composer = one(c.composer), send = one(c.send);
    const text = composer ? (composer.tagName === 'TEXTAREA' ? composer.value : composer.textContent) : '';
    if (!expected || !current.verified || current.ambiguous || current.running || current.draft || current.challenge ||
        (current.quota && current.model !== 'Sol') || !same(current,expected) || text !== ownedDraft ||
        !send || send.disabled || send.getAttribute('aria-disabled') === 'true') return {blocked: current};
    if (!timely()) return {blocked: current};
    send.click();
    return true;
  }
  if (action !== 'send' || !expected || !safe(observed) || !same(observed,expected)) return {blocked: observed};
  const composer = one(c.composer);
  if (!timely()) return {blocked: observed};
  if (composer.tagName === 'TEXTAREA') {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(composer,'го');
  } else if (composer.isContentEditable) {
    composer.textContent = 'го';
  } else return {blocked: observed};
  composer.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:'го'}));
  const current = snapshot('го');
  const send = one(c.send);
  const ownedText = composer.tagName === 'TEXTAREA' ? composer.value : composer.textContent;
  if (!composer.isConnected || one(c.composer) !== composer || ownedText !== 'го' || !safe(current) || !same(current,expected) || !send || send.disabled ||
      send.getAttribute('aria-disabled') === 'true') return {blocked: current};
  if (!timely()) return {blocked: current};
  send.click();
  return true;
}
