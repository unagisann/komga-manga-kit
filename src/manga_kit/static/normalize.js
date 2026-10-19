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
  const form = $('#normalization-form');
  const fields = form?.querySelector('fieldset');
  const saved = JSON.parse(form?.dataset.scope || '{}');
  let stale = false;
  const options = () => ({library: form.elements.library.value,
    subpath: form.elements.library.value === 'all' ? '' : form.elements.subpath.value.trim(),
    limit: Number(form.elements.limit.value), recheck: form.elements.recheck.checked});
  const updateExecution = () => {
    if ($('#normalization-execute')) $('#normalization-execute').disabled = stale || !$('#normalization-reviewed')?.checked;
  };
  const update = () => {
    const current = options();
    form.elements.subpath.disabled = current.library === 'all';
    $('#normalization-target-summary').textContent = `対象：${form.elements.library.selectedOptions[0]?.textContent || '全体'}${current.subpath ? ' / ' + current.subpath : ''}`;
    $('#normalization-options-summary').textContent = `確認上限 ${current.limit.toLocaleString('ja-JP')}冊${current.recheck ? '・確認済みも再確認' : ''}`;
    const auto = $('#normalization-auto').checked && !$('#normalization-auto').disabled;
    $('#normalization-check-label').textContent = auto ? '差分確認して実行' : '差分を確認';
    $('#normalization-mode-hint').textContent = auto
      ? '内部の差分・衝突を確認し、要確認項目がなければバックアップ後に実行します。'
      : '差分の確認だけでは漫画を変更しません。';
    if ($('#normalization-result')?.dataset.status === 'succeeded') {
      const changed = ['library', 'subpath', 'limit', 'recheck'].some(key => String(current[key]) !== String(saved[key]));
      if (changed && $('#normalization-reviewed')) $('#normalization-reviewed').checked = false;
      $('#normalization-stale').hidden = !changed && !stale;
      if (changed) stale = true;
    }
    updateExecution();
  };
  form?.addEventListener('input', update);
  form?.addEventListener('change', update);
  form?.addEventListener('invalid', () => { $('#normalization-options').open = true; }, true);
  form?.addEventListener('submit', async event => {
    event.preventDefault();
    const body = {...options(), auto_execute: $('#normalization-auto').checked && !$('#normalization-auto').disabled};
    fields.disabled = true; $('#normalization-error').hidden = true;
    try {
      const job = await post('/api/normalization/checks', body);
      const target = new URL('/normalize', location.origin);
      target.searchParams.set('check', job.id);
      if (job.action_id) target.searchParams.set('action', job.action_id);
      for (const [key, value] of Object.entries(body)) if (key !== 'auto_execute') target.searchParams.set(key, value);
      location.assign(target);
    } catch (reason) { error(reason.message); fields.disabled = false; update(); }
  });
  $('#normalization-libraries-refresh')?.addEventListener('click', async event => {
    const button = event.currentTarget; button.disabled = true;
    try {
      const data = await post('/api/normalization/libraries/refresh');
      const select = form.elements.library, previous = select.value;
      select.replaceChildren(new Option('全体', 'all'));
      data.libraries.forEach(library => {
        const option = new Option(library.label + (library.available ? '' : '（パス対応なし）'), library.id);
        option.disabled = !library.available;
        select.append(option);
      });
      if ([...select.options].some(option => option.value === previous && !option.disabled)) select.value = previous;
      if ($('#normalization-result')?.dataset.status === 'succeeded') { stale = true; $('#normalization-stale').hidden = false; }
      update();
    } catch (reason) { error(reason.message); }
    finally { button.disabled = false; }
  });
  $('#normalization-reviewed')?.addEventListener('change', updateExecution);
  if (form) update();
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
        if ($(message)) $(message).textContent = job.status === 'queued'
          ? `順番待ち${job.queue_position > 1 ? ' · 前に'+(job.queue_position-1)+'件' : ''}`
          : `処理中 · ${job.current}${job.total === null ? '' : ' / ' + job.total}冊`;
        setTimeout(poll, 1000);
      } catch (reason) { error(reason.message); }
    };
    poll();
  }
})();
