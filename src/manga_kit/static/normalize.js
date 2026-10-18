(() => {
  const $ = selector => document.querySelector(selector);
  const error = message => { $('#normalization-error').textContent = message; $('#normalization-error').hidden = false; };
  const post = async (url, body = {}) => {
    const response = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    let result;
    try { result = await response.json(); } catch { throw new Error('応答を読み取れませんでした。接続と認証を確認してください。'); }
    if (!response.ok) throw new Error(result.error || '処理を開始できませんでした。');
    return result;
  };
  $('#normalization-check')?.addEventListener('click', async event => {
    const button = event.currentTarget; button.disabled = true; $('#normalization-error').hidden = true;
    try { const job = await post('/api/normalization/checks'); location.assign(`/normalize?check=${encodeURIComponent(job.id)}`); }
    catch (reason) { error(reason.message); button.disabled = false; }
  });
  $('#normalization-reviewed')?.addEventListener('change', event => { $('#normalization-execute').disabled = !event.currentTarget.checked; });
  for (const action of ['execute', 'restore']) {
    $(`#normalization-${action}`)?.addEventListener('click', async event => {
      const button = event.currentTarget; button.disabled = true; $('#normalization-error').hidden = true;
      try {
        const job = await post(`/api/normalization/checks/${encodeURIComponent(button.dataset.checkId)}/${action}`, {confirm: button.dataset.confirm});
        location.assign(`/normalize?check=${encodeURIComponent(button.dataset.checkId)}&action=${encodeURIComponent(job.id)}`);
      } catch (reason) { error(reason.message); button.disabled = false; }
    });
  }
  for (const [selector, message] of [['#normalization-result', '#normalization-progress'], ['#normalization-action', '#normalization-action-status']]) {
    const element = $(selector);
    if (!element || !['queued', 'running'].includes(element.dataset.status)) continue;
    const poll = async () => {
      try {
        const response = await fetch(`/api/jobs/${encodeURIComponent(element.dataset.jobId)}`);
        if (!response.ok) throw new Error('進捗を取得できませんでした。処理履歴から確認できます。');
        const job = await response.json();
        if (!['queued', 'running'].includes(job.status)) { location.reload(); return; }
        if ($(message)) $(message).textContent = `処理中 · ${job.current}${job.total === null ? '' : ' / ' + job.total}冊`;
        setTimeout(poll, 1000);
      } catch (reason) { error(reason.message); }
    };
    poll();
  }
})();
