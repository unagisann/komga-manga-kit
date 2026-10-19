(() => {
  const $ = (selector) => document.querySelector(selector);
  const node = (tag, className, text) => {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (text !== undefined) result.textContent = text;
    return result;
  };
  const announce = (text) => {
    const target = $('#notification');
    target.textContent = text;
    target.hidden = false;
    clearTimeout(announce.timer);
    announce.timer = setTimeout(() => { target.hidden = true; }, 5000);
  };
  const post = async (url, body = {}) => {
    const response = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    let result;
    try { result = await response.json(); } catch (_) { throw new Error('応答を読み取れませんでした。認証と接続を確認してください。'); }
    if (!response.ok) throw new Error(result.error || '処理に失敗しました。');
    return result;
  };
  const get = async (url) => {
    const response = await fetch(url);
    let result;
    try { result = await response.json(); } catch (_) { throw new Error('応答を読み取れませんでした。認証と接続を確認してください。'); }
    if (!response.ok) throw new Error(result.error || '処理に失敗しました。');
    return result;
  };
  const jobStates = { queued: '待機中', running: '処理中', succeeded: '完了', failed: '失敗', interrupted: '中断' };
  const jobState = job => job.status === 'queued' ? `順番待ち${job.queue_position > 1 ? '（前に'+(job.queue_position-1)+'件）' : ''}` : jobStates[job.status];
  const toolJobKinds = { scan_refresh: '蔵書の表示更新', pages_preview: 'ページ修正の差分確認', shelf_preview: 'スマート本棚の差分確認', automation: '自動更新・取り込み後の処理', cover_screening: '表紙の確認', ai_generate: 'AIの提案取得', shelf_execute: 'スマート本棚の同期', shelf_restore: 'スマート本棚の復元', pages_execute: '表紙・ページの修正', pages_restore: 'ページ修正の復元', maintenance_preview: '管理操作の差分確認', maintenance_execute: '管理操作の実行', maintenance_restore: '管理操作の復元', source_refresh: '情報源の更新', inventory_refresh: '蔵書情報の更新', new_volume_check: '新刊チェック' };
  const jobKinds = { normalization_check: '追加・変更分の確認', normalize: 'ノーマライズ', normalization_execute: 'ノーマライズの実行', normalization_restore: 'ノーマライズの復元', enrichment: '自動補完の候補取得', scan: '蔵書スキャン', preview: '整理プレビュー', organize: '整理の実行', restore: '整理の復元', conversion: 'CBZ生成', conversion_restore: 'CBZ生成の取り消し', komga_metadata: 'Komga作品情報の反映', komga_metadata_restore: 'Komga作品情報の復元', komga_authors: 'Komga著者情報の反映', komga_authors_restore: 'Komga著者情報の復元' };
  const statusLabels = { ongoing: '連載中', completed: '完結', unknown: '未分類' };
  Object.assign(jobKinds, toolJobKinds);
  const waitJob = async (job, update, { result = true } = {}) => {
    while (['queued', 'running'].includes(job.status)) {
      update?.(job);
      await new Promise((resolve) => setTimeout(resolve, 700));
      job = await get(`/api/jobs/${encodeURIComponent(job.id)}`);
    }
    update?.(job);
    if (job.status !== 'succeeded') throw new Error(job.error || '処理を完了できませんでした。');
    return result ? get(`/api/jobs/${encodeURIComponent(job.id)}/result?limit=200`) : job;
  };
  document.querySelectorAll('.local-time').forEach((element) => {
    element.textContent = new Date(element.dateTime).toLocaleString('ja-JP');
  });

  document.addEventListener('keydown', (event) => {
    if (event.key !== '/' || event.defaultPrevented || event.isComposing || event.ctrlKey || event.metaKey || event.altKey) return;
    const target = event.target;
    if (target instanceof HTMLElement && (target.closest('input, textarea, select') || target.isContentEditable)) return;
    const search = $('#header-search');
    if (!search?.getClientRects().length) return;
    event.preventDefault();
    search.focus();
  });

  $('#theme-toggle')?.addEventListener('click', () => {
    const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem('manga-kit-theme', theme); } catch (_) { /* Still switch theme when storage is unavailable. */ }
    announce(`${theme === 'dark' ? 'ダーク' : 'ライト'}表示に切り替えました。`);
  });

  $('#refresh-library')?.addEventListener('click', async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      button.textContent = '読み取り開始…';
      await waitJob(await post('/api/jobs', { kind: 'scan' }), (job) => {
        button.textContent = `読み取り中 ${job.current}冊`;
      });
      location.reload();
    } catch (error) { announce(error.message); button.textContent = '読み直す'; button.disabled = false; }
  });

  $('#test-komga')?.addEventListener('click', async (event) => {
    const button = event.currentTarget, result = $('#komga-result'), libraries = $('#komga-libraries');
    button.disabled = true;
    result.hidden = false;
    result.className = 'connection-result';
    result.textContent = 'Komgaへ接続しています…';
    libraries.replaceChildren();
    try {
      const data = await post('/api/komga/test');
      result.textContent = `接続できました。${data.library_count}件のライブラリを確認しました。`;
      data.libraries.forEach((library) => libraries.append(node('li', '', library.name || '名称未設定')));
    } catch (error) { result.classList.add('error'); result.textContent = error.message; }
    finally { button.disabled = false; }
  });

  $('#layout-preset')?.addEventListener('change', (event) => {
    const form = $('#layout-form');
    const patterns = { kana: '{status}/{kana_row}/{series}', status: '{status}/{series}', author: '{author}/{series}', flat: '{series}' };
    const pattern = patterns[event.target.value];
    if (!pattern) return;
    ['ongoing', 'completed', 'unknown'].forEach((key) => { form.elements.namedItem(key).value = pattern; });
    form.dispatchEvent(new Event('input'));
    announce('プリセットを選びました。「配置を確認」で結果を更新できます。');
  });

  $('#layout-form')?.addEventListener('input', () => {
    if ($('#plan-list')) $('#plan-stale').hidden = false;
  });
  $('#layout-form')?.addEventListener('invalid', () => {
    $('#layout-fields details').open = true;
  }, true);
  $('#layout-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.currentTarget, button = form.querySelector('button[type=submit]');
    const errorBox = $('#plan-error'), fields = $('#layout-fields');
    const layout = Object.fromEntries(new FormData(form));
    button.disabled = true; fields.disabled = true;
    errorBox.hidden = true;
    try {
      const submitted = await post('/api/jobs', { kind: 'preview', layout });
      const job = await waitJob(submitted, (current) => {
        const status = $('#plan-job-status');
        const text = current.status === 'queued' ? '順番を待っています…' : current.total !== null ? '全冊の配置を確認しています…' : `蔵書を確認中 · ${current.current}冊`;
        const link = node('a', '', '処理履歴を見る'); link.href = `/jobs/${encodeURIComponent(current.id)}`;
        status.replaceChildren(document.createTextNode(`${text} · `), link);
        button.textContent = '確認中…';
      }, { result: false });
      location.assign(`/organize?preview=${encodeURIComponent(job.id)}`);
    } catch (error) {
      errorBox.textContent = error.message; errorBox.hidden = false;
      $('#plan-job-status').textContent = '配置を確認できませんでした。前回の結果を表示しています。処理履歴も確認できます。';
      button.disabled = false; fields.disabled = false; button.textContent = '配置を確認';
    }
  });

  const editor = $('#metadata-form');
  if (editor) {
    const save = $('#metadata-save'), errorBox = $('#metadata-error');
    const labels = { title: '作品名', author: '著者', reading: '読み', status: '状態', summary: 'あらすじ', publisher: '出版社', genres: 'ジャンル', tags: 'タグ' };
    const display = (value) => Array.isArray(value) ? value.join(' / ') || '未設定' : value || '未設定';
    let pending = null, generation = 0;
    const resetDisabled = $('#metadata-reset').disabled;
    const review = async (body) => {
      const expectedGeneration = generation;
      pending = null; save.disabled = true; errorBox.hidden = true;
      editor.querySelectorAll('button').forEach((button) => { button.disabled = true; });
      try {
        const result = await post(`/api/metadata/${encodeURIComponent(editor.dataset.seriesId)}/preview`, body);
        if (generation !== expectedGeneration) { $('#metadata-message').textContent = '編集内容が変わりました。変更内容を再確認してください。'; return; }
        const rows = $('#metadata-diff'); rows.replaceChildren();
        result.changes.forEach((change) => {
          const row = node('tr');
          const before = change.field === 'status' ? statusLabels[change.before] : change.before;
          const after = change.field === 'status' ? statusLabels[change.after] : change.after;
          row.append(node('td', '', labels[change.field] || change.field), node('td', 'metadata-value', display(before)), node('td', 'metadata-value', display(after)));
          rows.append(row);
        });
        $('#metadata-message').textContent = `${result.changes.length}項目の変更 · 五十音分類: ${result.before_row}行 → ${result.after_row}行${result.reset ? ' · 保存した編集を解除します' : ''}`;
        pending = { ...body, revision: result.revision, base_fingerprint: result.base_fingerprint };
        save.disabled = !result.changes.length && !result.reset;
      } catch (error) { errorBox.textContent = error.message; errorBox.hidden = false; }
      finally { editor.querySelectorAll('button').forEach((button) => { button.disabled = false; }); $('#metadata-reset').disabled = resetDisabled; }
    };
    editor.addEventListener('submit', (event) => {
      event.preventDefault(); const fields = Object.fromEntries(new FormData(editor));
      ['genres', 'tags'].forEach((key) => { fields[key] = fields[key].split('\n').map((item) => item.trim()).filter(Boolean); });
      review({ fields });
    });
    editor.addEventListener('input', () => {
      generation += 1; pending = null; save.disabled = true; $('#metadata-message').textContent = '編集内容が変わりました。変更内容を再確認してください。';
    });
    $('#metadata-reset').addEventListener('click', () => review({ reset: true }));
    save.addEventListener('click', async () => {
      if (!pending) return;
      save.disabled = true; errorBox.hidden = true;
      try { await post(`/api/metadata/${encodeURIComponent(editor.dataset.seriesId)}`, pending); location.reload(); }
      catch (error) { pending = null; errorBox.textContent = error.message; errorBox.hidden = false; }
    });
  }

  if ($('#jobs-panel')) {
    let timer;
    const updateJobs = async () => {
      clearTimeout(timer);
      try {
        const [data, history] = await Promise.all([get('/api/jobs'), get('/api/history')]);
        const rows = $('#jobs-table'); rows.replaceChildren();
        data.jobs.forEach((job) => {
          const row = node('tr'), result = node('td'), link = node('a', '', '詳細を見る');
          link.href = `/jobs/${encodeURIComponent(job.id)}`; result.append(link);
          row.append(node('td', '', jobKinds[job.kind] || job.kind), node('td', '', jobState(job)), node('td', '', `${job.current}${job.total === null ? '' : ` / ${job.total}`}`), node('td', '', new Date(job.created).toLocaleString('ja-JP')), result);
          rows.append(row);
        });
        if (!data.jobs.length) { const row = node('tr'), cell = node('td', '', 'まだ処理の記録はありません。'); cell.colSpan = 5; row.append(cell); rows.append(row); }
        const historyRows = $('#history-table'); historyRows.replaceChildren();
        history.events.forEach((event) => {
          const row = node('tr');
          const labels = { metadata_enrichment: '不足情報の一括補完', metadata_edit: '作品情報の編集', job_created: '処理の受付', job_finished: '処理の終了', metadata_relocated: '編集情報の引き継ぎ' };
          const detail = event.kind === 'metadata_enrichment' ? `${event.detail.series}作品の不足情報を補完しました` : event.kind === 'metadata_edit' ? `${event.detail.source_action === 'linked' ? '書誌情報を対応付け · ' : event.detail.source_action === 'unlinked' ? '対応付けを解除 · ' : ''}${event.detail.changes.length}項目${event.detail.reset ? 'を元に戻しました' : 'を変更しました'}` : event.kind === 'metadata_relocated' ? (event.detail.restore ? '編集情報を元の作品へ戻しました' : '編集情報を整理先へ引き継ぎました') : event.kind === 'job_created' ? jobKinds[event.detail.kind] : jobStates[event.detail.status];
          row.append(node('td', '', labels[event.kind] || event.kind), node('td', '', detail), node('td', '', new Date(event.created).toLocaleString('ja-JP')));
          historyRows.append(row);
        });
        if (!history.events.length) { const row = node('tr'), cell = node('td', '', 'まだ操作の記録はありません。'); cell.colSpan = 3; row.append(cell); historyRows.append(row); }
        if (data.jobs.some((job) => ['queued', 'running'].includes(job.status))) timer = setTimeout(updateJobs, 1500);
      } catch (error) { $('#jobs-message').textContent = error.message; }
    };
    document.querySelectorAll('[data-job-kind]').forEach((button) => button.addEventListener('click', async () => {
      button.disabled = true;
      try { await post('/api/jobs', { kind: button.dataset.jobKind }); $('#jobs-message').textContent = '処理を受け付けました。'; await updateJobs(); }
      catch (error) { $('#jobs-message').textContent = error.message; }
      finally { button.disabled = false; }
    }));
    updateJobs();
  }
  const detail = $('#job-detail');
  if (detail && ['queued', 'running'].includes(detail.dataset.jobStatus)) {
    const poll = async () => {
      try {
        const job = await get(`/api/jobs/${encodeURIComponent(detail.dataset.jobId)}`);
        if (!['queued', 'running'].includes(job.status)) location.reload();
        else { $('#job-status').textContent = job.status === 'queued' ? jobState(job) : `${jobState(job)} · ${job.current}${detail.dataset.jobKind === 'enrichment' ? '作品' : '冊'}を確認`; setTimeout(poll, 1500); }
      } catch (error) { $('#job-status').textContent = error.message; }
    }; poll();
  }
  $('#match-komga')?.addEventListener('click', async (event) => {
    const button = event.currentTarget, message = $('#match-result'); button.disabled = true;
    message.textContent = 'Komgaの作品と蔵書を照合しています…';
    try {
      const result = await post('/api/komga/match');
      const list = $('#match-list'); list.replaceChildren();
      result.matches.forEach((match) => list.append(node('li', '', `${match.name || '名称未設定'}: ${{ matched: '対応する蔵書あり', missing_local_series: '蔵書に作品がありません', unmapped: 'パス対応を確認してください' }[match.state]}`)));
      message.textContent = `${result.matches.length}作品を確認しました（Komga全体: ${result.total_elements}作品）。`;
    } catch (error) { message.textContent = error.message; }
    finally { button.disabled = false; }
  });
})();
