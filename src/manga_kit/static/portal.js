(() => {
  const region = document.querySelector('#portal-library');
  if (!region) return;
  const fetchHtml = async (url) => {
    const response = await fetch(url, {headers: {'Accept': 'text/html'}});
    if (!response.ok || !response.headers.get('Content-Type')?.includes('text/html')) throw new Error();
    return response.text();
  };
  const load = async () => {
    try {
      region.innerHTML = await fetchHtml(region.dataset.url);
      // Hero stats and the seasonal pick arrive with the shelves; move them into the hero.
      region.querySelectorAll('template[data-slot]').forEach((template) => {
        document.getElementById(template.dataset.slot)?.replaceChildren(template.content);
        template.remove();
      });
    } catch {
      const message = document.createElement('p');
      message.className = 'notice warning';
      message.setAttribute('role', 'alert');
      message.textContent = '蔵書を読み込めませんでした。設定の保存先を確認して、画面を読み直してください。';
      region.replaceChildren(message);
    } finally {
      region.setAttribute('aria-busy', 'false');
    }
  };
  region.addEventListener('click', async (event) => {
    const link = event.target.closest('[data-portal-reroll]');
    const rail = document.getElementById('portal-completed-rail');
    if (!link || !rail || event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
    event.preventDefault();
    rail.setAttribute('aria-busy', 'true');
    try {
      const url = new URL(region.dataset.url, location.href);
      url.searchParams.set('section', 'completed');
      rail.innerHTML = await fetchHtml(url);
    } catch {
      location.href = link.href;
    } finally {
      rail.setAttribute('aria-busy', 'false');
    }
  });
  load();
})();
