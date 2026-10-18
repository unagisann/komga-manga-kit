(() => {
  // Reorder pages with buttons or drag and drop, mark pages for deletion, then review the change on the server.
  const form = document.querySelector('#page-editor'); if (!form) return;
  const list = form.querySelector('#page-cards'), status = document.querySelector('#tool-status');
  const dirty = document.querySelector('#page-dirty'), info = document.querySelector('#page-delete-info');
  const original = [...list.children];
  let submitting = false, dragged = null;
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
    const selected_pages = [...list.children].filter(card => !card.classList.contains('is-deleted')).map(card => card.dataset.name);
    try {
      const response = await fetch('/api/tools/pages/'+encodeURIComponent(form.dataset.book)+'/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({selected_pages,fingerprint:form.dataset.fingerprint})});
      const data = await response.json(); if (!response.ok) throw new Error(data.error || '変更内容を確認できませんでした。');
      submitting = true;
      const next = new URL(location.href); next.searchParams.set('plan', data.plan.id);
      location.href = next.pathname + next.search;
    } catch(error) { status.hidden = false; status.textContent = error.message; status.scrollIntoView({block:'nearest'}); }
  });
})();
