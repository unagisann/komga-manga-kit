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

  $('#theme-toggle')?.addEventListener('click', () => {
    const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem('manga-kit-theme', theme); } catch (_) { /* Still switch theme when storage is unavailable. */ }
    announce(`${theme === 'dark' ? 'ダーク' : 'ライト'}表示に切り替えました。`);
  });

  $('#refresh-library')?.addEventListener('click', async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try { await post('/api/library/refresh'); location.reload(); }
    catch (error) { announce(error.message); button.disabled = false; }
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
    announce('プリセットを選びました。「プレビューを更新」で配置を確認できます。');
  });

  $('#layout-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.currentTarget, button = form.querySelector('button[type=submit]');
    const errorBox = $('#plan-error');
    button.disabled = true;
    errorBox.hidden = true;
    try {
      const result = await post('/api/organize/preview', { layout: Object.fromEntries(new FormData(form)) });
      const counts = $('#plan-counts');
      counts.replaceChildren();
      [['ready', '変更予定'], ['unchanged', '変更なし'], ['blocked', '要確認']].forEach(([key, label]) => {
        const element = node('span'); element.append(node('strong', '', String(result.counts[key])), ` ${label}`); counts.append(element);
      });
      const list = $('#plan-list'); list.replaceChildren();
      result.items.slice(0, 200).forEach((item) => {
        const article = node('article', `plan-item ${item.state}`), heading = node('div', 'plan-item-heading');
        heading.append(node('strong', '', item.title), node('span', 'chip', { ready: '変更予定', unchanged: '変更なし', blocked: '要確認' }[item.state]));
        article.append(heading, node('code', 'source-path', item.source), node('span', 'path-arrow', '↓'), node('code', 'target-path', item.target || '巻数の確認が必要です'));
        item.issues.forEach((issue) => article.append(node('p', 'warning-text', issue)));
        list.append(article);
      });
      if (!result.items.length) list.append(node('div', 'empty-state', '整理対象の蔵書がありません。'));
      if (result.items.length > 200) list.append(node('p', 'form-hint', '先頭200件を表示しています。全件はCLIの plan で確認できます。'));
      if (result.truncated) { errorBox.textContent = 'ファイル上限に達しているため、一部の蔵書のプレビューです。'; errorBox.hidden = false; }
      announce('プレビューを更新しました。');
    } catch (error) { errorBox.textContent = error.message; errorBox.hidden = false; }
    finally { button.disabled = false; }
  });
})();
