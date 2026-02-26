/**
 * app.js — SSE progress handler for the scanning page.
 *
 * Listens to /progress and drives:
 *   - The progress bar (#progress-bar)
 *   - The status message (#status-msg)
 *   - The percentage label (#pct-label)
 *   - The error banner (#error-banner / #error-text)
 *
 * On "done": redirects to /done.
 * On "error": shows the error banner inline.
 */

(function () {
  const bar     = document.getElementById('progress-bar');
  const msg     = document.getElementById('status-msg');
  const pctLbl  = document.getElementById('pct-label');
  const errBanner = document.getElementById('error-banner');
  const errText   = document.getElementById('error-text');

  function setProgress(pct, text) {
    if (pct >= 0) {
      bar.style.width = Math.min(100, pct) + '%';
      pctLbl.textContent = Math.min(100, pct) + '%';
    }
    if (text) msg.textContent = text;
  }

  const source = new EventSource('/progress');

  source.onmessage = function (event) {
    let data;
    try {
      data = JSON.parse(event.data);
    } catch (_) {
      return; // keepalive or malformed — ignore
    }

    const phase = data.phase;

    if (phase === 'walking') {
      // Indeterminate: wiggle between 5-15% during scan
      const current = parseFloat(bar.style.width) || 5;
      const next = current < 15 ? current + 1 : 5;
      setProgress(next, data.msg || 'Scanning your Google Drive…');

    } else if (phase === 'downloading') {
      setProgress(data.pct >= 0 ? data.pct : null, data.msg);

    } else if (phase === 'zipping') {
      setProgress(90, data.msg || 'Bundling your archive…');

    } else if (phase === 'done') {
      setProgress(100, data.msg || 'Done!');
      source.close();
      // Small delay so the user sees 100% before redirect
      setTimeout(function () {
        window.location.href = '/done';
      }, 600);

    } else if (phase === 'error') {
      source.close();
      setProgress(bar.style.width, 'Error — see below.');
      if (errBanner) errBanner.classList.remove('hidden');
      if (errText)   errText.textContent = data.msg || 'An unknown error occurred.';
    }
  };

  source.onerror = function () {
    // SSE connection dropped (server restart, network blip, etc.)
    msg.textContent = 'Connection lost. Check your internet and refresh the page.';
    source.close();
  };
})();
