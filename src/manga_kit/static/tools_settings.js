(() => {
  const status = document.querySelector('#tool-status');
  const save = async (url,body) => { const response = await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); const data = await response.json(); if (!response.ok) throw new Error(data.error || '保存できませんでした。'); location.reload(); };
  const show = (error, target = status) => { target.hidden = false; target.textContent = error.message; target.scrollIntoView({block:'nearest'}); };
  document.querySelector('#tools-settings')?.addEventListener('submit',async event => {
    event.preventDefault(); const form = event.target, tools = {};
    try {
      ['kobo_enabled','anime_enabled','discovery_enabled','rankings_enabled','mangaupdates_enabled','ai_enabled','notify_enabled'].forEach(key => tools[key] = form.elements[key].checked);
      ['ai_provider','ai_url','ai_model','public_komga_url'].forEach(key => tools[key] = form.elements[key].value);
      tools.reading_accounts = JSON.parse(form.elements.reading_accounts.value);
      await save('/api/tools/settings',{revision:Number(form.dataset.revision),komga_url:form.elements.komga_url.value,
        rakuten:{enabled:form.elements.rakuten_enabled.checked,origin:form.elements.rakuten_origin.value},anilist:{enabled:form.elements.anilist_enabled.checked},tools});
    } catch(error) { show(error); }
  });
  document.querySelector('#ai-models-refresh')?.addEventListener('click',async () => {
    try { const response = await fetch('/api/tools/ai/models',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); const data = await response.json(); if (!response.ok) throw new Error(data.error || 'モデルを取得できませんでした。'); document.querySelector('#ai-models').replaceChildren(...data.models.map(model => new Option(model,model))); show(new Error(`${data.models.length}件のモデルを確認しました。`)); } catch(error) { show(error); }
  });
  document.querySelector('#tools-credentials')?.addEventListener('submit',async event => {
    event.preventDefault(); const values = Object.fromEntries([...new FormData(event.target)].filter(([,value]) => value !== ''));
    try { await save('/api/tools/credentials',{values}); } catch(error) { show(error); }
  });

  // Header and portal menus: move rows, then save the order and the checked items.
  const menus = document.querySelector('#menu-settings');
  if (menus) {
    const menuStatus = document.querySelector('#menu-status');
    const rows = (name) => [...menus.querySelectorAll(`[data-menu="${name}"] > li`)].map(item => ({id:item.dataset.id,shown:item.querySelector('input').checked}));
    const saveMenus = async (body) => { try { await save('/api/menus',{revision:Number(menus.dataset.revision),...body}); } catch(error) { show(error, menuStatus); } };
    menus.addEventListener('click',event => {
      const button = event.target.closest('[data-move]'); if (!button) return;
      const item = button.closest('li'), step = Number(button.dataset.move);
      const target = step < 0 ? item.previousElementSibling : item.nextElementSibling?.nextElementSibling;
      if (step < 0 && !target) return;
      item.parentElement.insertBefore(item, target || null);
      button.focus();
    });
    menus.addEventListener('submit',event => { event.preventDefault(); saveMenus({header:rows('header'),portal:rows('portal')}); });
    document.querySelector('#menu-reset')?.addEventListener('click',() => saveMenus({header:[],portal:[]}));
  }
})();
