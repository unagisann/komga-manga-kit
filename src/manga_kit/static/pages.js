(() => {
  // Reorder pages with buttons or drag and drop, mark pages for deletion, then review the change on the server.
  const form = document.querySelector('#page-editor'); if (!form) return;
  const list = form.querySelector('#page-cards'), status = document.querySelector('#tool-status');
  const dirty = document.querySelector('#page-dirty'), info = document.querySelector('#page-delete-info');
  const original = [...list.children];
  let submitting = false, pending = false, dragged = null;
  const refresh = () => {
    const cards = [...list.children];
    cards.forEach((card, index) => { card.querySelector('.page-position').textContent = `#${index + 1}`; });
    const removed = cards.filter(card => card.classList.contains('is-deleted')).length;
    dirty.hidden = !(removed || cards.some((card, index) => card !== original[index]));
    info.hidden = !removed;
    info.textContent = `削除予定 ${removed}ページ`;
  };
  const setDeleted = (card, deleted) => {
    const button = card.querySelector('[data-page-delete]');
    card.classList.toggle('is-deleted', deleted);
    button.setAttribute('aria-pressed', String(deleted));
    button.textContent = deleted ? '削除を取り消す' : '削除';
  };
  list.addEventListener('click', event => {
    const button = event.target.closest('button'); if (!button) return;
    const card = button.closest('li'), action = button.dataset.pageMove;
    if (action === 'first') list.prepend(card);
    else if (action === 'last') list.append(card);
    else if (action === 'up' && card.previousElementSibling) list.insertBefore(card, card.previousElementSibling);
    else if (action === 'down' && card.nextElementSibling) card.nextElementSibling.after(card);
    else if ('pageDelete' in button.dataset) setDeleted(card, !card.classList.contains('is-deleted'));
    button.focus();
    refresh();
  });
  list.addEventListener('dragstart', event => {
    dragged = event.target.closest('li');
    if (!dragged) return;
    dragged.classList.add('is-dragging');
    event.dataTransfer.effectAllowed = 'move';
  });
  list.addEventListener('dragover', event => {
    const target = event.target.closest('li');
    if (!dragged || !target) return;
    event.preventDefault();
    if (target === dragged) return;
    const box = target.getBoundingClientRect();
    if (event.clientX - box.left > box.width / 2) target.after(dragged); else target.before(dragged);
  });
  list.addEventListener('dragend', () => { dragged?.classList.remove('is-dragging'); dragged = null; refresh(); });
  document.querySelector('#page-reset')?.addEventListener('click', () => {
    original.forEach(card => { setDeleted(card, false); list.append(card); });
    refresh();
  });
  window.addEventListener('beforeunload', event => { if (!submitting && !dirty.hidden) event.preventDefault(); });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (pending) return;
    pending = true; form.inert = true;
    const selected_pages = [...list.children].filter(card => !card.classList.contains('is-deleted')).map(card => card.dataset.name);
    try {
      const response = await fetch('/api/tools/pages/'+encodeURIComponent(form.dataset.book)+'/preview/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({selected_pages,fingerprint:form.dataset.fingerprint})});
      let job = await response.json(); if (!response.ok) throw new Error(job.error || '変更内容を確認できませんでした。');
      submitting = true;
      const link = document.createElement('a'); link.href = '/jobs/'+encodeURIComponent(job.id); link.textContent = '処理の詳細';
      while (['queued','running'].includes(job.status)) {
        status.hidden = false;
        status.textContent = job.status === 'queued' ? `順番待ち${job.queue_position > 1 ? ' · 前に'+(job.queue_position-1)+'件' : ''}。` : '変更内容を確認しています。';
        status.append('この画面を離れても処理は続きます。 ', link);
        await new Promise(resolve => setTimeout(resolve,1000));
        const progress = await fetch('/api/jobs/'+encodeURIComponent(job.id));
        if (!progress.ok) throw new Error('進捗を確認できませんでした。処理の詳細から確認できます。');
        job = await progress.json();
      }
      if (job.status !== 'succeeded') throw new Error(job.error || '変更内容を確認できませんでした。');
      const result = await fetch('/api/jobs/'+encodeURIComponent(job.id)+'/result');
      if (!result.ok) throw new Error('結果を確認できませんでした。処理の詳細から確認できます。');
      const data = await result.json();
      const next = new URL(location.href); next.searchParams.set('plan', data.plan.id);
      location.href = next.pathname + next.search;
    } catch(error) { submitting = false; status.hidden = false; status.textContent = error.message; status.scrollIntoView({block:'nearest'}); }
    finally { pending = false; form.inert = false; }
  });
})();
