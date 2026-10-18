(() => {
  const buttons = document.querySelectorAll('[data-discovery-filter]');
  const search = document.querySelector('#discovery-search');
  const status = document.querySelector('#discovery-filter-status');
  const normalize = value => value.normalize('NFKC').toLocaleLowerCase('ja').trim();
  const applyFilters = () => {
    const filter = document.querySelector('[data-discovery-filter].active')?.dataset.discoveryFilter || 'all';
    const query = normalize(search?.value || '');
    let total = 0;
    document.querySelectorAll('.discovery-article').forEach(article => {
      let visible = 0;
      article.querySelectorAll('.discovery-card').forEach(card => {
        const owned = card.dataset.owned === '1';
        const match = (filter === 'all' || (filter === 'owned' ? owned : !owned)) &&
          (!query || normalize(card.dataset.search || '').includes(query));
        card.hidden = !match;
        if (match) visible++;
      });
      const empty = article.querySelector('.discovery-no-results');
      if (empty) empty.hidden = visible !== 0;
      if (!article.hidden) total += visible;
    });
    if (status) {
      status.hidden = !query && filter === 'all';
      status.textContent = `選択中の特集で${total}作品を表示しています。`;
    }
  };
  buttons.forEach(button => button.addEventListener('click', () => {
    buttons.forEach(other => {
      const selected = other === button;
      other.classList.toggle('active', selected);
      other.setAttribute('aria-pressed', String(selected));
    });
    applyFilters();
  }));
  search?.addEventListener('input', applyFilters);
  document.querySelectorAll('[data-discovery-shelf]').forEach(select => {
    select.addEventListener('change', () => {
      const shelf = document.getElementById(select.dataset.discoveryShelf);
      shelf?.querySelectorAll('.discovery-article').forEach(article => {
        article.hidden = article.dataset.article !== select.value;
      });
      applyFilters();
    });
  });
  document.querySelectorAll('img[data-cover-fallback]').forEach(img => {
    const fallback = () => {
      const url = img.dataset.coverFallback;
      if (url && img.src !== url) { img.dataset.coverFallback = ''; img.src = url; }
      else img.hidden = true;
    };
    img.addEventListener('error', fallback);
    if (img.complete && img.naturalWidth === 0) fallback();
  });
})();
