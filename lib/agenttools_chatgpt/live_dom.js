/* Observed ChatGPT DOM, called by LiveAdapter only on enrolled dedicated pages.
 * Probe/prepare handles retain node and URL identity across asynchronous rendering.
 * Cancelled alone and generic Pro labels cannot authorize recovery.
 */
({action = 'observe', probe = null, prepared = null, expected = null, deadline = 0, labels = {}, model = '', effort = '', target = ''}) => {
  const visible = el => {
    if (!el?.isConnected || !el.getClientRects().length) return false;
    for (let p = el; p; p = p.parentElement) {
      const s = getComputedStyle(p);
      if (p.hidden || p.getAttribute('aria-hidden') === 'true' || s.display === 'none' || s.visibility === 'hidden' || Number(s.opacity) === 0) return false;
    }
    return true;
  };
  const all = (s, root = document) => [...root.querySelectorAll(s)];
  const one = s => { const a = all(s).filter(visible); return a.length === 1 ? a[0] : null; };
  const rootSelector = 'main [data-chatgpt-conversation-selection-target="true"][data-thread-find-target="conversation"]';
  const composerSelector = '[contenteditable="true"][data-composer-markdown][role="textbox"][aria-label="Ask ChatGPT"]';
  const triggerSelector = 'button[aria-label="Select ChatGPT model"][data-codex-intelligence-trigger="true"]';
  const signature = el => el ? JSON.stringify([el.textContent, el.getAttribute('data-selected-reasoning-effort'), el.getAttribute('aria-label')]) : '';
  const canonical = () => {
    const m = location.href.match(/^https:\/\/chatgpt\.com\/(?:g\/g-p-[a-f0-9]{32}(?:-[a-zA-Z0-9]+(?:-[a-zA-Z0-9]+)*)?\/)?c\/([A-Za-z0-9_-]+)\/?$/);
    if (location.pathname.startsWith('/g/') && (!m || !/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(m[1]))) return '';
    return m ? 'https://chatgpt.com/c/' + m[1] : '';
  };
  const timely = () => performance.timeOrigin + performance.now() < deadline;
  const menuItems = selector => {const menu = one('[role="menu"]'); return menu ? all(selector,menu).filter(visible) : [];};
  if (action === 'menu-state') return {
    open: !!one('[role="menu"]'),
    selected: menuItems('[role="menuitemradio"][aria-checked="true"][data-model-selected="true"]').map(el=>el.textContent.trim()),
    effort: menuItems('[role="status"]').map(el=>el.textContent.trim()).filter(text=>/^(Pro, 5 of 5\.|Extra High, 4 of 5\.)$/.test(text)),
    signature: signature(one(triggerSelector)),
    label: one(triggerSelector)?.innerText.trim() || ''
  };
  if (action === 'probe') {
    const selected = menuItems('[role="menuitemradio"][aria-checked="true"][data-model-selected="true"]');
    const trigger = one(triggerSelector);
    const label = selected.length === 1 ? selected[0].textContent.trim() : '';
    return {url:location.href, trigger, signature:signature(trigger), model:labels[label] || '', at:performance.timeOrigin + performance.now()};
  }
  if (action === 'finalize-probe') {
    const trigger = one(triggerSelector), closed = trigger?.innerText.trim();
    const valid = probe?.url === location.href && probe.trigger === trigger && probe.model === model && !one('[role="menu"]') && ((model === 'Astra' && closed === 'Pro') || (model === 'Sol' && effort === 'Extra High' && closed === '5.6 Extra High'));
    return valid ? {...probe, signature:signature(trigger), effort, at:performance.timeOrigin + performance.now()} : null;
  }
  const roots = all(rootSelector), root = roots.length === 1 && visible(roots[0]) ? roots[0] : null;
  const composer = one(composerSelector), trigger = one(triggerSelector);
  const snapshot = (owned = false) => {
    const o = {chat:canonical(), turn:'', generation:'', status:'', active:false, verified:false, model:'', effort:'', running:false, draft:false, challenge:false, ambiguous:true, quota:false};
    o.draft = all(composerSelector).filter(visible).some(el => !!el.textContent && !(owned && prepared?.composer === el && el.textContent === 'го')) || all('[data-testid="attachment"], [data-testid="attachment-preview"], input[type="file"]').some(el => visible(el) && (el.tagName !== 'INPUT' || el.files?.length));
    o.challenge = !one('button[aria-label="Open profile menu"]') || all('iframe[src*="captcha"],iframe[src*="challenges.cloudflare"], [role="dialog"], a[href="/auth/login"]').some(visible) || /verify you are human|checking your browser|just a moment/i.test(document.title);
    o.running = all('button[aria-label="Stop"]').some(visible);
    o.quota = all('[role="alert"]').some(el => visible(el) && !el.closest('[data-turn-key]') && /(?:GPT-6|Astra).*(?:limit|quota)|(?:limit|quota).*GPT-6/i.test(el.textContent));
    if (probe && probe.url === location.href && probe.trigger === trigger && probe.signature === signature(trigger) && performance.timeOrigin + performance.now() - probe.at <= 10000) {
      o.model = probe.model;
      o.effort = probe.effort || '';
      o.verified = o.model === 'Astra' || (o.model === 'Sol' && o.effort === 'Extra High');
    }
    if (!o.chat || !root || !composer) return o;
    const turns = all('[data-turn-key]', root), latest = turns.at(-1);
    const keys = turns.map(el => el.getAttribute('data-turn-key'));
    if (!latest || !visible(latest) || keys.some(k => !/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(k)) || new Set(keys).size !== keys.length) return o;
    for (let p = root; p; p = p.parentElement) {
      if (/(auto|scroll)/.test(getComputedStyle(p).overflowY) && p.scrollHeight - p.scrollTop - p.clientHeight > 4) return o;
    }
    const scroll = document.scrollingElement;
    if (scroll && scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight > 4) return o;
    const states = all('[data-talvt-turn-state]', latest).filter(visible);
    o.running ||= states.some(el => el.getAttribute('data-talvt-turn-state') === 'in_progress');
    if (states.length !== 1) return o;
    o.turn = keys.at(-1); o.generation = o.turn;
    const statuses = all('div', latest).filter(el => el.classList.contains('group/activity-header')).flatMap(el => all('span', el)).filter(el => visible(el) && el.children.length === 0 && el.textContent === 'Thinking failed');
    o.status = statuses.length === 1 ? 'Thinking failed' : '';
    o.active = statuses.length === 1 && states[0].getAttribute('data-talvt-turn-state') === 'cancelled' && !statuses[0].closest('[data-user-message-bubble]');
    o.ambiguous = statuses.length > 1;
    if (all('[role="alert"]').some(el => visible(el) && !el.closest('[data-turn-key]') && /(?:GPT-5\.6 Sol|\bSol\b).*(?:limit|quota)|(?:limit|quota).*(?:GPT-5\.6 Sol|\bSol\b)/i.test(el.textContent))) {
      o.status = 'Sol quota exhausted'; o.active = false;
    }
    return o;
  };
  const same = (a,b) => b && Object.keys(a).every(k => a[k] === b[k]);
  const safe = o => o.chat && o.turn && o.generation && o.active && o.status === 'Thinking failed' && o.verified && !o.running && !o.draft && !o.challenge && !o.ambiguous && !(o.quota && o.model === 'Astra');
  if (action === 'observe') return snapshot();
  if (action === 'selection-start') {
    const current = snapshot();
    if (!expected || current.chat !== expected.chat || current.turn !== expected.turn || !current.active || current.status !== 'Thinking failed' || current.running || current.draft || current.challenge || current.ambiguous) return null;
    return {url:location.href, root, composer, trigger, latest:root ? all('[data-turn-key]',root).at(-1) : null, turn:current.turn, chat:current.chat};
  }
  if (action === 'selection-step') {
    const valid = () => {
      const current = snapshot();
      return prepared && current.chat === prepared.chat && current.turn && prepared.url === location.href && prepared.root === one(rootSelector) && prepared.composer === one(composerSelector) && prepared.trigger === one(triggerSelector) && prepared.latest === (root ? all('[data-turn-key]',root).at(-1) : null) && prepared.turn === current.turn && current.active && current.status === 'Thinking failed' && !current.running && !current.draft && !current.challenge && !current.ambiguous && timely();
    };
    if (!valid()) return false;
    let control = null;
    if (target === 'trigger') { if (one('[role="menu"]')) return false; control = trigger; }
    if (target === 'full') control = one('[role="menuitem"][aria-label="Select model"]');
    if (target === 'sol') {
      const matches = menuItems('[role="menuitemradio"]').filter(el=>el.textContent.trim() === 'GPT-5.6 Sol');
      if (matches.length === 1) control = matches[0];
    }
    if (target === 'power') {
      const status = menuItems('[role="status"]').filter(el=>el.textContent.trim() === 'Pro, 5 of 5.');
      control = one('[role="menuitem"][aria-label="Power"][data-reasoning-slider="true"]');
      if (status.length !== 1 || !control || !control.getAttribute('aria-keyshortcuts')?.includes('ArrowLeft')) return false;
      control.focus();
      if (!valid() || document.activeElement !== control || !visible(control)) return false;
      control.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowLeft',code:'ArrowLeft',keyCode:37,which:37,bubbles:true}));
      return true;
    }
    if (!control || control.disabled || control.getAttribute('aria-disabled') === 'true') return false;
    control.click(); return true;
  }
  if (action === 'prepare') {
    const current = snapshot();
    const result = {url:location.href, root, composer, trigger, latest:root ? all('[data-turn-key]',root).at(-1) : null, blocked:current, ok:false};
    if (!safe(current) || !same(current, expected) || !timely()) return result;
    composer.focus();
    if (!composer.isConnected || one(composerSelector) !== composer || !same(snapshot(), expected) || !timely()) return result;
    // Native synchronous editor input avoids overwriting a draft between check and fill.
    const selection = getSelection(), range = document.createRange();
    range.selectNodeContents(composer); selection.removeAllRanges(); selection.addRange(range);
    if (!composer.isConnected || one(composerSelector) !== composer || composer.textContent || !same(snapshot(), expected) || !timely()) return result;
    result.ok = document.execCommand('insertText', false, 'го');
    return result;
  }
  if (action === 'send') {
    const current = snapshot(true), send = one('button[aria-label="Send"]');
    if (!prepared?.ok || prepared.url !== location.href || prepared.root !== root || prepared.composer !== composer || prepared.trigger !== trigger || (root ? all('[data-turn-key]',root).at(-1) : null) !== prepared.latest || !composer?.isConnected || composer.textContent !== 'го' || !safe(current) || !same(current,expected) || !send || send.disabled || send.getAttribute('aria-disabled') === 'true' || !timely()) return {blocked:current};
    send.click(); return true;
  }
  if (action === 'ack') {
    if (!prepared || prepared.url !== location.href || prepared.root !== root || canonical() !== expected.chat) return false;
    const turns = root ? all('[data-turn-key]',root) : [], keys = turns.map(el=>el.getAttribute('data-turn-key'));
    if (new Set(keys).size !== keys.length) return false;
    const index = keys.indexOf(expected.turn), next = turns[index + 1];
    if (index < 0 || !next || turns[index] !== prepared.latest) return false;
    const units = all('[data-chatgpt-search-unit-key$=":user"]',next).filter(el => el.getAttribute('data-chatgpt-search-message-ids') === keys[index+1]);
    const bubbles = units.length === 1 ? all('[data-user-message-bubble="true"]',units[0]) : [];
    return bubbles.length === 1 && visible(bubbles[0]) && bubbles[0].textContent === 'го';
  }
  return false;
}
