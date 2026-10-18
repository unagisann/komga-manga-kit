(() => {
  const form = document.querySelector('#auto-enrichment');
  if (!form) return;
  document.querySelectorAll('.enrichment-filter select').forEach(select => {
    select.addEventListener('change', () => select.form.requestSubmit());
  });
  const errorBox = document.querySelector('#enrichment-error');
  const showError = message => { errorBox.textContent = message; errorBox.hidden = false; };
  const post = async (url, body) => {
    const response = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    let result;
    try { result = await response.json(); } catch { throw new Error('応答を読み取れませんでした。接続と認証を確認してください。'); }
    if (!response.ok) throw new Error(result.error || '処理を完了できませんでした。');
    return result;
  };
  const choices = Array.from(form.querySelectorAll('[name=series_ids]:not(:disabled)'));
  const selectAll = document.querySelector('#enrichment-select-all');
  const count = document.querySelector('#enrichment-selection');
  const update = () => {
    const selected = choices.filter(input => input.checked).length;
    count.textContent = selected ? `${selected}作品を選択中` : '対象を選んでください。';
    selectAll.checked = choices.length > 0 && selected === choices.length;
    selectAll.indeterminate = selected > 0 && selected < choices.length;
  };
  selectAll.disabled = choices.length === 0;
  selectAll.addEventListener('change', () => { choices.forEach(input => { input.checked = selectAll.checked; }); update(); });
  choices.forEach(input => input.addEventListener('change', update));
  let submitting = false;
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (submitting) return;
    const seriesIds = choices.filter(input => input.checked).map(input => input.value);
    if (!seriesIds.length) { showError('候補を取得する作品を選んでください。'); return; }
    submitting = true; errorBox.hidden = true;
    const button = form.querySelector('[type=submit]'); button.disabled = true; button.textContent = '候補の取得を開始しています…';
    try {
      const job = await post('/api/enrichment/batches', {series_ids: seriesIds, provider: form.dataset.provider, link_only: form.dataset.linkOnly === 'true'});
      location.assign(`/metadata?provider=${encodeURIComponent(form.dataset.provider)}&batch=${encodeURIComponent(job.id)}`);
    } catch (error) { showError(error.message); submitting = false; button.disabled = false; button.textContent = '選んだ作品の候補を自動取得'; }
  });
  const batch = document.querySelector('#enrichment-batch');
  if (batch && ['queued', 'running'].includes(batch.dataset.status)) {
    const poll = async () => {
      try {
        const response = await fetch(`/api/jobs/${encodeURIComponent(batch.dataset.jobId)}`);
        if (!response.ok) throw new Error('進捗を取得できませんでした。処理履歴から確認できます。');
        const job = await response.json();
        if (!['queued', 'running'].includes(job.status)) { location.reload(); return; }
        document.querySelector('#enrichment-progress').textContent = `候補を取得しています · ${job.current}${job.total === null ? '' : ' / ' + job.total}作品`;
        setTimeout(poll, 1000);
      } catch (error) { showError(error.message); }
    };
    poll();
  }
  document.querySelector('#enrichment-apply')?.addEventListener('click', async event => {
    const button = event.currentTarget;
    if (button.disabled) return;
    button.disabled = true; errorBox.hidden = true;
    try { await post(`/api/enrichment/batches/${encodeURIComponent(button.dataset.jobId)}/apply`, {confirm: button.dataset.confirm}); location.reload(); }
    catch (error) { showError(error.message); button.disabled = false; }
  });
})();
