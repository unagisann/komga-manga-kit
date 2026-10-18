(() => {
  // Like the existing Tools: filters apply at once and "more" appends. Links and the form still work without this.
  const form = document.querySelector('#library-filters');
  const results = document.querySelector('#library-results');
  if (!form || !results) return;
  let timer, controller;
  const modified = (event) => event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0;
  const query = (page) => {
    const params = new URLSearchParams();
    for (const [name, value] of new FormData(form)) if (value) params.set(name, value);
    if (page > 1) params.set('page', page);
    return params.toString();
  };
  const load = async (page = 1) => {
    clearTimeout(timer);
    controller?.abort();
    controller = new AbortController();
    const params = query(page);
    const before = results.querySelectorAll('.cover-card').length;
    results.setAttribute('aria-busy', 'true');
    try {
      const response = await fetch(`${results.dataset.url}?${params}`, {headers: {Accept: 'text/html'}, signal: controller.signal});
      if (!response.ok || !response.headers.get('Content-Type')?.includes('text/html')) throw new Error();
      results.innerHTML = await response.text();
      history.replaceState(null, '', params ? `${form.action}?${params}` : form.action);
      // After "more", continue from the first new series.
      if (page > 1) results.querySelectorAll('.cover-card')[before]?.querySelector('.cap a')?.focus({preventScroll: true});
    } catch (error) {
      if (error.name !== 'AbortError') location.href = params ? `${form.action}?${params}` : form.action;
    } finally {
      results.setAttribute('aria-busy', 'false');
    }
  };
  form.addEventListener('click', (event) => {
    const chip = event.target.closest('.filter-chip[data-f]');
    if (!chip || modified(event)) return;
    event.preventDefault();
    form.elements[chip.dataset.f].value = chip.dataset.v;
    form.querySelectorAll(`.filter-chip[data-f="${chip.dataset.f}"]`).forEach((other) => {
      other.classList.toggle('active', other === chip);
      if (other === chip) other.setAttribute('aria-current', 'true'); else other.removeAttribute('aria-current');
    });
    load();
  });
  form.addEventListener('change', (event) => { if (event.target.matches('select')) load(); });
  form.addEventListener('input', (event) => {
    if (event.target.name !== 'q') return;
    clearTimeout(timer);
    timer = setTimeout(load, 250);
  });
  form.addEventListener('submit', (event) => { event.preventDefault(); load(); });
  results.addEventListener('click', (event) => {
    const more = event.target.closest('[data-more]');
    if (!more || modified(event)) return;
    event.preventDefault();
    load(Number(more.dataset.more));
  });
})();
