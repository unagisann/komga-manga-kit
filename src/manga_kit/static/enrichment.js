(() => {
  const root = document.querySelector('#enrichment');
  if (!root) return;
  const $ = (selector) => root.querySelector(selector);
  const current = JSON.parse(root.dataset.current);
  const base = `/api/metadata/${encodeURIComponent(root.dataset.seriesId)}`;
  const provider = root.dataset.provider;
  const labels = { title: '作品名', reading: '読み', author: '著者', publisher: '出版社', summary: 'あらすじ', genres: 'ジャンル', tags: 'タグ' };
  const display = (value) => Array.isArray(value) ? value.join(' / ') || '未設定' : value || '未設定';
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const assessment = (match) => {
    const box = node('div', undefined, match?.warnings?.length ? 'notice warning source-match' : 'form-hint source-match');
    if (match?.evidence?.length) box.append(node('p', match.evidence.join(' · ')));
    if (match?.warnings?.length) {
      box.append(node('p', '要確認'));
      const list = node('ul'); match.warnings.forEach((text) => list.append(node('li', text))); box.append(list);
    }
    return box;
  };
  const productDetails = (candidate) => [
    candidate.label ? `レーベル: ${candidate.label}` : '',
    candidate.editions?.length ? `版: ${candidate.editions.join(' / ')}` : '',
    candidate.size ? `判型: ${candidate.size}` : '',
    candidate.volume ? `${candidate.volume}巻` : '',
    candidate.sales_date ? `発売: ${candidate.sales_date}` : '',
    candidate.fields.genres?.length ? `読者層: ${display(candidate.fields.genres)}${candidate.readership_source === 'label' ? '（レーベル名から）' : ''}` : '',
    candidate.fields.summary ? 'あらすじあり' : ''
  ].filter(Boolean).join(' · ');
  const error = (message) => { $('#source-error').textContent = message; $('#source-error').hidden = false; };
  const post = async (url, body) => {
    const response = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...body, provider }) });
    let value;
    try { value = await response.json(); } catch (_) { throw new Error('応答を読み取れませんでした。認証と接続を確認してください。'); }
    if (!response.ok) throw new Error(value.error || '処理に失敗しました。');
    return value;
  };
  let results, selected, query, pending, generation = 0, searching = false, saving = false;
  const invalidate = () => {
    generation += 1; pending = null; $('#source-save').disabled = true;
    $('#source-review-message').textContent = '内容が変わりました。採用内容を再確認してください。';
  };
  const review = async (body) => {
    if (saving) return;
    invalidate(); const expected = generation; $('#source-error').hidden = true;
    try {
      const result = await post(`${base}/source/preview`, body);
      if (generation !== expected) return;
      const rows = $('#source-diff'); rows.replaceChildren();
      result.changes.forEach((change) => {
        const row = node('tr');
        row.append(node('td', labels[change.field] || change.field), node('td', display(change.before), 'metadata-value'), node('td', display(change.after), 'metadata-value'));
        rows.append(row);
      });
      $('#source-review').hidden = false;
      $('#source-review-message').textContent = result.unlink ? '対応付けを解除します。保存した作品情報は残ります。' : `${result.changes.length}項目を変更し、選んだ候補に対応付けます。${result.provenance.adjusted_fields.length ? '候補から修正した項目: ' + result.provenance.adjusted_fields.map((key) => labels[key]).join('・') : ''}`;
      $('#source-review-match').replaceChildren(assessment(result.match));
      $('#source-save').textContent = result.unlink ? '対応付けの解除を保存' : '対応付けと選んだ情報を保存';
      pending = { ...body, revision: result.revision, base_fingerprint: result.base_fingerprint };
      $('#source-save').disabled = false;
      $('#source-review').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    } catch (reason) { error(reason.message); }
  };
  const choose = (candidate) => {
    if (saving) return;
    invalidate(); selected = candidate; $('#source-review').hidden = true;
    $('#source-selection').hidden = false;
    $('#source-selected-title').textContent = candidate.title;
    $('#source-selected-provider').textContent = results.label;
    $('#source-selection-match').replaceChildren(assessment(candidate.match));
    const reference = $('#source-reference'); reference.replaceChildren();
    reference.append(node('span', `${candidate.isbn ? 'ISBN ' + candidate.isbn + ' · ' : ''}取得: ${new Date(results.retrieved_at).toLocaleString('ja-JP')} `));
    if (candidate.url) {
      const link = node('a', '情報源を見る'); link.href = candidate.url; link.target = '_blank'; link.rel = 'noopener noreferrer'; reference.append(link);
    }
    if (provider !== 'anilist' && productDetails(candidate)) reference.append(node('p', productDetails(candidate), 'form-hint'));
    const list = $('#source-field-list'); list.replaceChildren();
    Object.entries(labels).forEach(([key, label]) => {
      if (!(key in candidate.fields)) return;
      const row = node('div', undefined, 'source-field');
      const checkLabel = node('label', undefined, 'source-check');
      const isList = ['genres', 'tags'].includes(key), isText = key === 'summary';
      const check = node('input'); check.type = 'checkbox'; check.dataset.field = key;
      check.checked = candidate.match?.status !== 'review' && (isList ? !current[key]?.length : !current[key]);
      checkLabel.append(check, node('span', label + 'を採用'));
      const editLabel = node('label', undefined, 'source-value'); editLabel.append(node('span', label + 'の候補'));
      const input = node(isList || isText ? 'textarea' : 'input'); input.name = key;
      input.value = isList ? candidate.fields[key].join('\n') : candidate.fields[key]; input.maxLength = isList ? 10100 : isText ? 8000 : 300;
      if (isList || isText) input.rows = isText ? 6 : 4;
      editLabel.append(input, node('span', `現在: ${display(current[key])}`, 'form-hint'));
      row.append(checkLabel, editLabel); list.append(row);
    });
    $('#source-selection').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  };
  const search = async (page) => {
    if (searching || saving) return;
    invalidate(); selected = null; results = null; searching = true;
    $('#source-selection').hidden = true; $('#source-review').hidden = true; $('#source-pages').hidden = true;
    $('#source-results').replaceChildren(); $('#source-error').hidden = true;
    $('#source-search-message').textContent = '候補を検索しています…';
    const expected = generation;
    $('#source-search button').disabled = true;
    try {
      const value = await post(`${base}/search`, { ...query, page });
      if (generation !== expected) { $('#source-search-message').textContent = '条件が変わりました。もう一度検索してください。'; return; }
      results = value;
      $('#source-search-message').textContent = `${results.total === null ? 'このページ ' + results.candidates.length : results.total}件 · ${results.cached ? '保存済みの検索結果' : '検索結果'} · 取得: ${new Date(results.retrieved_at).toLocaleString('ja-JP')}${results.candidates.length ? '' : ' · 条件を変えて検索してください。'}`;
      results.candidates.forEach((candidate) => {
        const card = node('article', undefined, 'source-card');
        const description = node('div'); description.append(node('h3', candidate.title));
        if (provider === 'anilist') {
          description.append(node('p', `${candidate.alternate_titles?.join(' / ') || '別表記なし'} · ${candidate.format === 'ONE_SHOT' ? '読切' : '漫画'} · 初出: ${candidate.year || '不明'} · ${candidate.country || '国未設定'}`, 'muted'));
          description.append(node('p', `ジャンル: ${display(candidate.fields.genres)} · タグ: ${display(candidate.fields.tags)}`, 'form-hint'));
        } else {
          description.append(node('p', `${candidate.fields.author || '著者未設定'} · ${candidate.fields.publisher || '出版社未設定'}`, 'muted'));
          if (productDetails(candidate)) description.append(node('p', productDetails(candidate), 'form-hint'));
        }
        description.append(assessment(candidate.match));
        if (candidate.isbn) description.append(node('p', `ISBN ${candidate.isbn}`, 'form-hint'));
        const button = node('button', 'この候補を確認', 'button'); button.type = 'button'; button.addEventListener('click', () => choose(candidate));
        card.append(description, button); $('#source-results').append(card);
      });
      const hasNext = results.has_next ?? results.page < results.page_count;
      $('#source-pages').hidden = results.page <= 1 && !hasNext;
      $('#source-prev').disabled = results.page <= 1; $('#source-next').disabled = !hasNext;
      $('#source-page-label').textContent = results.page_count === null ? `${results.page}ページ` : `${results.page} / ${results.page_count}ページ`;
    } catch (reason) { error(reason.message); $('#source-search-message').textContent = '検索を完了できませんでした。'; }
    finally { searching = false; $('#source-search button').disabled = false; }
  };
  $('#source-search').addEventListener('submit', (event) => { event.preventDefault(); query = Object.fromEntries(new FormData(event.currentTarget)); search(1); });
  $('#source-search').addEventListener('input', () => { invalidate(); results = null; selected = null; $('#source-results').replaceChildren(); $('#source-pages').hidden = true; $('#source-selection').hidden = true; $('#source-review').hidden = true; });
  root.querySelectorAll('.source-alias').forEach((button) => button.addEventListener('click', () => {
    if (saving || searching) return;
    const input = $('#source-search').elements.title; input.value = button.dataset.title;
    if ($('#source-search').elements.isbn) $('#source-search').elements.isbn.value = '';
    input.dispatchEvent(new Event('input', { bubbles: true })); input.focus();
  }));
  $('#source-prev').addEventListener('click', () => { if (results) search(results.page - 1); });
  $('#source-next').addEventListener('click', () => { if (results) search(results.page + 1); });
  $('#source-fields').addEventListener('input', invalidate);
  $('#source-fields').addEventListener('submit', (event) => {
    event.preventDefault(); if (!selected || !results) return;
    const fields = {};
    $('#source-field-list').querySelectorAll('input[type="checkbox"]:checked').forEach((input) => {
      const key = input.dataset.field, value = $('#source-fields').elements[key].value;
      fields[key] = ['genres', 'tags'].includes(key) ? value.split('\n').map((item) => item.trim()).filter(Boolean) : value;
    });
    review({ search_id: results.search_id, candidate_id: selected.id, fields });
  });
  $('#source-unlink')?.addEventListener('click', () => { selected = null; $('#source-selection').hidden = true; review({ unlink: true }); });
  $('#source-save').addEventListener('click', async () => {
    if (!pending || saving) return;
    saving = true; $('#source-save').disabled = true; $('#source-error').hidden = true;
    const controls = Array.from(root.querySelectorAll('input, textarea, button'), (element) => [element, element.disabled]);
    controls.forEach(([element]) => { element.disabled = true; });
    try { await post(`${base}/source`, pending); location.assign(root.dataset.returnUrl); }
    catch (reason) { error(reason.message); pending = null; controls.forEach(([element, disabled]) => { element.disabled = disabled; }); $('#source-save').disabled = true; }
    finally { saving = false; }
  });
})();
