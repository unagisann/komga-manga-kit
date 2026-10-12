(() => {
  let theme;
  try { theme = localStorage.getItem('manga-kit-theme'); } catch (_) { /* Private browsing may deny storage. */ }
  if (!['light', 'dark'].includes(theme)) theme = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  document.documentElement.dataset.theme = theme;
})();
