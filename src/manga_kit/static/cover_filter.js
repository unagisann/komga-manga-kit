(() => {
  // Filter chips and a search box over a cover grid, as on the existing Tools pages.
  document.querySelectorAll('[data-cover-filter]').forEach((panel) => {
    const grid = document.querySelector(panel.dataset.coverFilter);
    if (!grid) return;
    const empty = grid.parentElement.querySelector('[data-filter-empty]');
    const search = panel.querySelector('[data-filter-search]');
    const apply = () => {
      const active = panel.querySelector('.filter-chip.active[data-filter]')?.dataset.filter || 'all';
      const query = (search?.value || '').trim().toLowerCase();
      let shown = 0;
      grid.querySelectorAll('.cover-card').forEach((card) => {
        const flags = (card.dataset.flags || '').split(' ');
        const visible = (active === 'all' || flags.includes(active)) && (!query || (card.dataset.search || '').toLowerCase().includes(query));
        card.hidden = !visible;
        if (visible) shown += 1;
      });
      if (empty) empty.hidden = shown > 0;
    };
    panel.addEventListener('click', (event) => {
      const chip = event.target.closest('.filter-chip[data-filter]');
      if (!chip) return;
      panel.querySelectorAll('.filter-chip[data-filter]').forEach((other) => {
        other.classList.toggle('active', other === chip);
        other.setAttribute('aria-pressed', String(other === chip));
      });
      apply();
    });
    search?.addEventListener('input', apply);
    apply();
  });
})();
