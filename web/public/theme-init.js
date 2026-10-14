// Pre-paint theme (#211). Loaded synchronously from <head> so data-theme is set
// before first paint. A file, not inline, because the production CSP is
// script-src 'self'. Keep the storage key in step with src/theme.ts.
try {
  var t = localStorage.getItem('theme')
  if (t === 'light' || t === 'dark') document.documentElement.setAttribute('data-theme', t)
} catch (e) {}
