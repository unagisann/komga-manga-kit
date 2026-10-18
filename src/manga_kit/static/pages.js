(() => {
  const form = document.querySelector('#page-editor'); if (!form) return;
  const list = form.querySelector('.page-grid'), status = document.querySelector('#tool-status');
  form.querySelectorAll('[data-page-move]').forEach(button => button.addEventListener('click',() => {
    const item = button.closest('li');
    if (button.dataset.pageMove === 'first') { list.prepend(item); item.querySelector('[name=keep]').checked = true; }
    else if (button.dataset.pageMove === 'up' && item.previousElementSibling) list.insertBefore(item,item.previousElementSibling);
    else if (button.dataset.pageMove === 'down' && item.nextElementSibling) item.nextElementSibling.after(item);
  }));
  form.addEventListener('submit',async event => {
    event.preventDefault();
    const selected_pages = [...list.children].filter(item => item.querySelector('[name=keep]').checked).map(item => item.dataset.name);
    try {
      const response = await fetch('/api/tools/pages/'+encodeURIComponent(form.dataset.book)+'/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({selected_pages,fingerprint:form.dataset.fingerprint})});
      const data = await response.json(); if (!response.ok) throw new Error(data.error || '変更内容を確認できませんでした。');
      location.href = location.pathname+'?plan='+encodeURIComponent(data.plan.id);
    } catch(error) { status.hidden = false; status.textContent = error.message; }
  });
})();
