(() => {
  // Settings save in place without reloading, so a section saved first never discards what another section has unsaved.
  const request = async (url,body) => {
    const response = await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    let data;
    try { data = await response.json(); } catch (_) { throw new Error('応答を読み取れませんでした。認証と接続を確認してください。'); }
    if (!response.ok) throw new Error(data.error || '保存できませんでした。');
    return data;
  };
  const say = (target, text) => { target.hidden = !text; target.textContent = text; };
  const node = (tag, text) => { const result = document.createElement(tag); result.textContent = text; return result; };
  // Inputs keep the saved values as their defaults, so a form has unsaved changes exactly when it differs from them.
  const edited = form => [...form.elements].some(element => element.type === 'checkbox' ? element.checked !== element.defaultChecked
    : element.tagName === 'SELECT' ? [...element.options].some(option => option.selected !== option.defaultSelected)
    : 'defaultValue' in element && element.value !== element.defaultValue);
  const remember = form => [...form.elements].forEach(element => {
    if (element.type === 'password') element.value = '';
    if (element.type === 'checkbox') element.defaultChecked = element.checked;
    else if (element.tagName === 'SELECT') [...element.options].forEach(option => { option.defaultSelected = option.selected; });
    else if ('defaultValue' in element) element.defaultValue = element.value;
  });
  window.addEventListener('beforeunload', event => {
    if (!document.querySelector('form.is-dirty')) return;
    event.preventDefault(); event.returnValue = '';
  });

  // Sections and the side contents stop below the sticky header, whose height changes as its menu wraps.
  const appHeader = document.querySelector('.app-header');
  if (appHeader && 'ResizeObserver' in window) new ResizeObserver(() => document.documentElement.style.setProperty('--header-height', appHeader.offsetHeight + 'px')).observe(appHeader);
  // The contents always mark one section: the last whose top has passed the upper part of the screen, the first
  // at the top of the page, the last at its end, and the one just chosen while it is still in view.
  const contents = document.querySelector('.settings-toc');
  if (contents) {
    const links = [...contents.querySelectorAll('a')].map(link => [link, document.getElementById(link.hash.slice(1))]).filter(([, section]) => section);
    let chosen = null, frame = 0;
    const mark = current => links.forEach(([link]) => link === current ? link.setAttribute('aria-current', 'true') : link.removeAttribute('aria-current'));
    const spy = () => {
      frame = 0;
      const line = (appHeader?.offsetHeight || 0) + innerHeight * .25, top = chosen?.[1].getBoundingClientRect().top;
      if (chosen && top >= 0 && top < innerHeight * .6) return mark(chosen[0]);
      chosen = null;
      const end = innerHeight + scrollY >= document.documentElement.scrollHeight - 2;
      mark((end ? links.at(-1) : links.findLast(([, section]) => section.getBoundingClientRect().top <= line) || links[0])[0]);
    };
    const later = () => { frame ||= requestAnimationFrame(spy); };
    contents.addEventListener('click', event => { chosen = links.find(([link]) => link === event.target.closest('a')) || null; later(); });
    addEventListener('scroll', later, {passive:true}); addEventListener('resize', later);
    spy();
  }

  const form = document.querySelector('#tools-settings');
  if (form) {
    const status = document.querySelector('#tool-status'), revert = document.querySelector('#tools-revert');
    const refresh = () => {
      form.querySelectorAll('[data-when]').forEach(part => { part.hidden = !part.dataset.when.split(' ').some(name => form.elements[name].checked); });
      const dirty = edited(form);
      if (dirty || form.classList.contains('is-dirty')) say(status, dirty ? '未保存の変更があります。' : '');
      form.classList.toggle('is-dirty', dirty); revert.disabled = !dirty;
    };
    form.addEventListener('input', refresh); form.addEventListener('change', refresh);
    form.addEventListener('reset', () => setTimeout(refresh));
    refresh();
    // A key typed but not saved yet: sent only to check or save it, never echoed back.
    const typed = name => { const field = name && form.elements[name]; return field && !field.disabled ? field.value : ''; };
    const sources = {env:['ongoing','環境変数で設定済み'], file:['completed','保存済み'], '':['unknown','未設定']};
    const show = view => {
      form.dataset.revision = view.revision;
      form.querySelectorAll('[data-credential]').forEach(chip => { const [tone, label] = sources[view.credentials[chip.dataset.credential] || '']; chip.className = 'chip ' + tone; chip.textContent = label; });
      const komga = document.querySelector('#komga-state');
      const connected = Boolean(view.komga_url && view.credentials[komga.dataset.key]);
      komga.className = 'chip ' + (connected ? 'completed' : 'unknown'); komga.textContent = connected ? '接続設定あり' : '未設定';
      const rakuten = document.querySelector('#rakuten-state');
      if (rakuten.dataset.keys) rakuten.textContent = '楽天Books: ' + (view.rakuten.enabled && rakuten.dataset.keys.split(' ').every(name => view.credentials[name]) ? '設定済み' : '未設定');
    };
    form.addEventListener('submit',async event => {
      event.preventDefault(); const tools = {}, credentials = {};
      ['kobo_enabled','anime_enabled','discovery_enabled','rankings_enabled','mangaupdates_enabled','ai_enabled','notify_enabled'].forEach(key => tools[key] = form.elements[key].checked);
      ['ai_provider','ai_url','ai_model','public_komga_url'].forEach(key => tools[key] = form.elements[key].value);
      form.querySelectorAll('[data-secret]').forEach(field => { if (!field.disabled && field.value) credentials[field.name] = field.value; });
      try {
        show(await request('/api/tools/settings',{revision:Number(form.dataset.revision),komga_url:form.elements.komga_url.value,
          rakuten:{enabled:form.elements.rakuten_enabled.checked,origin:form.elements.rakuten_origin.value},anilist:{enabled:form.elements.anilist_enabled.checked},tools,credentials}));
        remember(form); refresh(); say(status, '保存しました。');
      } catch(error) { say(status, error.message); }
    });

    const komgaButton = document.querySelector('#test-komga');
    komgaButton?.addEventListener('click',async () => {
      const result = document.querySelector('#komga-result'), libraries = document.querySelector('#komga-libraries');
      // Only what differs from the saved settings, so checking them as saved still works without admin access.
      const url = form.elements.komga_url, key = typed(komgaButton.closest('section').querySelector('[data-secret]')?.name), body = {};
      if (!url.disabled && url.value !== url.defaultValue) body.komga_url = url.value;
      if (key) body.api_key = key;
      komgaButton.disabled = true; result.hidden = false; result.className = 'connection-result';
      result.textContent = 'Komgaへ接続しています…'; libraries.replaceChildren();
      try {
        const data = await request('/api/komga/test', body);
        result.textContent = `接続できました。${data.library_count}件のライブラリを確認しました。` + (edited(form) ? '設定はまだ保存していません。' : '');
        libraries.replaceChildren(...data.libraries.map(library => node('li', library.name || '名称未設定')));
      } catch (error) { result.classList.add('error'); result.textContent = error.message; }
      finally { komgaButton.disabled = false; }
    });

    const modelsButton = document.querySelector('#ai-models-refresh');
    modelsButton?.addEventListener('click',async () => {
      const target = document.querySelector('#ai-models-status'), key = typed(modelsButton.closest('section').querySelector('[data-secret]')?.name);
      const body = {tools:{ai_enabled:form.elements.ai_enabled.checked,ai_provider:form.elements.ai_provider.value,ai_url:form.elements.ai_url.value}};
      if (key) body.api_key = key;
      modelsButton.disabled = true; say(target, 'モデルを確認しています…');
      try {
        const data = await request('/api/tools/ai/models', body);
        document.querySelector('#ai-models').replaceChildren(...data.models.map(model => new Option(model,model)));
        say(target, `${data.models.length}件のモデルを確認しました。モデル名の欄から選べます。`);
      } catch(error) { say(target, error.message); }
      finally { modelsButton.disabled = false; }
    });
  }

  // Header and portal menus: move rows, then save the order and the checked items.
  const menus = document.querySelector('#menu-settings');
  if (menus) {
    const status = document.querySelector('#menu-status');
    const rows = (name) => [...menus.querySelectorAll(`[data-menu="${name}"] > li`)].map(item => ({id:item.dataset.id,shown:item.querySelector('input').checked}));
    const snapshot = () => JSON.stringify([rows('header'), rows('portal')]);
    let saved = snapshot();
    const refresh = () => {
      const dirty = snapshot() !== saved;
      if (dirty || menus.classList.contains('is-dirty')) say(status, dirty ? '未保存の変更があります。' : '');
      menus.classList.toggle('is-dirty', dirty);
    };
    // The header shows the saved menu at once; other pages read it when they open.
    const header = () => {
      const nav = document.querySelector('.app-header .nav'), active = nav?.querySelector('a.active');
      if (!nav) return;
      nav.replaceChildren(...[...menus.querySelectorAll('[data-menu="header"] > li')].filter(item => item.querySelector('input').checked).map(item => {
        const link = document.createElement('a'); link.href = item.dataset.href;
        link.append(item.querySelector('svg').cloneNode(true), item.querySelector('label > span').textContent);
        if (active && item.dataset.href === active.getAttribute('href')) { link.className = 'active'; link.setAttribute('aria-current', active.getAttribute('aria-current') || 'page'); }
        return link;
      }));
    };
    const show = view => {
      menus.dataset.revision = view.revision;
      ['header','portal'].forEach(name => view[name].forEach(row => {
        const item = menus.querySelector(`[data-menu="${name}"] > [data-id="${CSS.escape(row.id)}"]`);
        if (!item) return;
        item.parentElement.append(item); item.querySelector('input').checked = row.shown;
      }));
      saved = snapshot(); refresh(); header();
    };
    const saveMenus = async (body, done) => { try { show(await request('/api/menus',{revision:Number(menus.dataset.revision),...body})); say(status, done); } catch(error) { say(status, error.message); } };
    menus.addEventListener('change', refresh);
    menus.addEventListener('click',event => {
      const button = event.target.closest('[data-move]'); if (!button) return;
      const item = button.closest('li'), step = Number(button.dataset.move);
      const target = step < 0 ? item.previousElementSibling : item.nextElementSibling?.nextElementSibling;
      if (step < 0 && !target) return;
      item.parentElement.insertBefore(item, target || null);
      button.focus(); refresh();
    });
    menus.addEventListener('submit',event => { event.preventDefault(); saveMenus({header:rows('header'),portal:rows('portal')}, '保存しました。ヘッダーにも反映しました。'); });
    document.querySelector('#menu-reset')?.addEventListener('click',() => saveMenus({header:[],portal:[]}, '既定の並びに戻しました。'));
  }
})();
