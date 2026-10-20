(() => {
  const status = document.querySelector('#tool-status');
  const show = message => { if (status) { status.hidden = false; status.textContent = message; } };
  const post = async (url, body) => {
    const response = await fetch(url, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '処理を受け付けられませんでした。');
    return data;
  };
  const showJob = job => {
    const message = job.status === 'queued'
      ? `順番待ち${job.queue_position > 1 ? ' · 前に'+(job.queue_position-1)+'件' : ''}`
      : `処理中 · ${job.current}${job.total === null ? '' : ' / '+job.total}`;
    show(message+'。この画面を離れても処理は続きます。');
    if (status) {
      const link = document.createElement('a');
      link.href = '/jobs/'+encodeURIComponent(job.id); link.textContent = '処理の詳細';
      status.append(' ', link);
    }
  };
  const follow = async (job,reviewUrl) => {
    showJob(job);
    while (['queued','running'].includes(job.status)) {
      await new Promise(resolve => setTimeout(resolve,1000));
      const response = await fetch(`/api/jobs/${encodeURIComponent(job.id)}`);
      if (!response.ok) throw new Error('進捗を確認できませんでした。管理の処理履歴から確認できます。');
      job = await response.json();
      if (['queued','running'].includes(job.status)) showJob(job);
    }
    if (job.status !== 'succeeded') throw new Error(job.error || '処理を完了できませんでした。');
    const response = await fetch(`/api/jobs/${encodeURIComponent(job.id)}/result`);
    if (!response.ok) throw new Error('結果を確認できませんでした。処理の詳細から確認できます。');
    const result = await response.json();
    if (reviewUrl && result.plan) {
      const next = new URL(reviewUrl,location.origin); next.searchParams.set('plan',result.plan.id);
      location.assign(next.pathname+next.search); return;
    }
    if (result.komga_followup) {
      const next = new URL(location.href); next.searchParams.set('followup',result.komga_followup);
      location.assign(next.pathname+next.search); return;
    }
    location.reload();
  };
  document.querySelectorAll('[data-tool-action]').forEach(button => button.addEventListener('click', async () => {
    if (button.dataset.confirm && !confirm(button.dataset.confirm)) return;
    button.disabled = true;
    try {
      const data = await post(button.dataset.toolAction, JSON.parse(button.dataset.payload || '{}'));
      if (button.dataset.reviewUrl && data.plan) { location.href = button.dataset.reviewUrl+'?plan='+encodeURIComponent(data.plan.id); return; }
      if (data.id && data.status) await follow(data,button.dataset.reviewUrl);
      else if (button.dataset.next) location.href = button.dataset.next;
      else { show('保存しました。'); location.reload(); }
    } catch (error) { show(error.message); button.disabled = false; }
  }));
  document.querySelectorAll('.tool-match').forEach(form => {
    let timer, generation = 0;
    form.querySelector('[data-owned-search]')?.addEventListener('input', event => {
      clearTimeout(timer); const value = event.target.value, expected = ++generation;
      timer = setTimeout(async () => {
        try {
          const response = await fetch('/api/tools/owned?q='+encodeURIComponent(value));
          if (!response.ok) throw new Error('蔵書を検索できませんでした。');
          const data = await response.json(); if (expected !== generation) return;
          const select = form.elements.series_id;
          select.replaceChildren(new Option('未所持',''));
          data.items.forEach(item => select.append(new Option(`${item.title} · ${item.author || '著者未設定'}`,item.id)));
        } catch(error) { show(error.message); }
      },250);
    });
    form.addEventListener('submit',async event => {
      event.preventDefault();
      try { await post('/api/tools/matches',{source:form.dataset.source,entry_id:form.dataset.entry,series_id:form.elements.series_id.value || null}); location.reload(); }
      catch(error) { show(error.message); }
    });
  });
  document.querySelectorAll('.tool-facts[data-series]').forEach(form => form.addEventListener('submit',async event => {
    event.preventDefault();
    const body = Object.fromEntries(new FormData(form));
    body.revision = Number(form.dataset.revision);
    body.original_year = body.original_year === '' ? null : Number(body.original_year);
    body.rating = body.rating === '' ? null : Number(body.rating);
    try { await post('/api/tools/series/'+encodeURIComponent(form.dataset.series)+'/facts',body); location.reload(); }
    catch(error) { show(error.message); }
  }));
  document.querySelector('#tool-ai-generate')?.addEventListener('submit', async event => {
    event.preventDefault(); const form = event.target;
    try { await follow(await post('/api/tools/ai/generate',{series_id:form.elements.series_id.value,fields:[...form.querySelectorAll('[name=fields]:checked')].map(input => input.value),web_search:form.elements.web_search.checked})); }
    catch(error) { show(error.message); }
  });
  document.querySelector('#tool-automation')?.addEventListener('submit',async event => {
    event.preventDefault(); const form = event.target;
    const body = {revision:Number(form.dataset.revision),time:form.elements.time.value,steps:[...form.querySelectorAll('[name=steps]:checked')].map(input => input.value)};
    ['enabled','auto_apply'].forEach(key => body[key] = form.elements[key].checked);
    try { await post('/api/tools/automation/settings',body); location.reload(); } catch(error) { show(error.message); }
  });
  document.querySelector('#tool-move')?.addEventListener('submit',async event => {
    event.preventDefault(); const series_ids = [...event.target.querySelectorAll('[name=series_ids]:checked')].map(input => input.value);
    try { await follow(await post('/api/tools/maintenance/move/preview',{series_ids}),'/moves'); } catch(error) { show(error.message); }
  });
  document.querySelectorAll('[data-check-all]').forEach(toggle => toggle.addEventListener('change', () => {
    toggle.closest('form').querySelectorAll('[name=series_ids]:not(:disabled)').forEach(input => { input.checked = toggle.checked; });
  }));
  const syncForm = document.querySelector('#tool-komga-sync');
  const updateSyncFields = () => {
    if (!syncForm) return;
    const metadata = syncForm.elements.mode.value === 'metadata';
    syncForm.querySelector('[data-metadata-fields]').hidden = !metadata;
    syncForm.querySelector('[data-metadata-overwrite]').hidden = !metadata;
  };
  syncForm?.elements.mode.addEventListener('change',updateSyncFields); updateSyncFields();
  syncForm?.addEventListener('submit',async event => {
    event.preventDefault(); const form = event.target, mode = form.elements.mode.value;
    const body = {mode,replace_locked:form.elements.replace_locked.checked,replace_lists:form.elements.replace_lists.checked};
    if (form.dataset.followup) body.followup = form.dataset.followup;
    if (mode === 'metadata') { body.fields = [...form.querySelectorAll('[name=fields]:checked')].map(input => input.value); body.overwrite = form.elements.overwrite.checked; }
    const next = new URL('/komga-sync',location.origin);
    if (body.followup) { next.searchParams.set('followup',body.followup); next.searchParams.set('mode',mode); }
    try { await follow(await post('/api/tools/maintenance/komga/preview',body),next.pathname+next.search); } catch(error) { show(error.message); }
  });
  document.querySelectorAll('.tool-ai-proposal').forEach(form => form.addEventListener('submit',async event => {
    event.preventDefault(); const fields = {};
    form.querySelectorAll('[name=selected]:checked').forEach(input => fields[input.value] = input.value === 'genres' ? form.elements[input.value].value.split('\n').map(value => value.trim()).filter(Boolean) : form.elements[input.value].value);
    try {
      const saved = await post('/api/tools/ai/'+encodeURIComponent(form.dataset.id)+'/save',{confirm:form.dataset.confirm,fields});
      const next = new URL(location.href); next.searchParams.delete('followup');
      if (saved.komga_followup) next.searchParams.set('followup',saved.komga_followup);
      location.assign(next.pathname+next.search);
    }
    catch(error) { show(error.message); }
  }));
})();
