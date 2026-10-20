(() => {
  const panel = document.querySelector('#komga-followup');
  if (!panel) return;
  const status = panel.querySelector('[data-followup-status]');
  const buttons = [...panel.querySelectorAll('[data-komga-followup-mode]')];
  let pending = false;
  const read = async response => {
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || '反映内容を確認できませんでした。');
    return value;
  };
  buttons.forEach(button => button.addEventListener('click', async () => {
    if (pending) return;
    pending = true; buttons.forEach(item => { item.disabled = true; }); status.hidden = false;
    try {
      let job = await read(await fetch('/api/tools/maintenance/komga/preview', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({followup:panel.dataset.id, mode:button.dataset.komgaFollowupMode})
      }));
      const link = document.createElement('a'); link.href = '/jobs/'+encodeURIComponent(job.id); link.textContent = '処理の詳細';
      while (['queued','running'].includes(job.status)) {
        status.textContent = job.status === 'queued' ? '順番待ち。' : 'Komgaへの反映内容を確認しています。';
        status.append('この画面を離れても確認は続きます。 ', link);
        await new Promise(resolve => setTimeout(resolve,1000));
        job = await read(await fetch('/api/jobs/'+encodeURIComponent(job.id)));
      }
      if (job.status !== 'succeeded') throw new Error(job.error || '反映内容を確認できませんでした。');
      const result = await read(await fetch('/api/jobs/'+encodeURIComponent(job.id)+'/result'));
      const next = new URL('/komga-sync', location.origin);
      next.searchParams.set('plan', result.plan.id);
      next.searchParams.set('followup', panel.dataset.id);
      next.searchParams.set('mode', button.dataset.komgaFollowupMode);
      location.assign(next.pathname+next.search);
    } catch (error) {
      status.textContent = error.message; pending = false;
      buttons.forEach(item => { item.disabled = false; });
    }
  }));
})();
