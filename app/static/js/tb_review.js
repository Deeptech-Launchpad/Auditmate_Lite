/* Auditor's accept / reject screen.
 *
 * Each decision posts immediately and the trial balance totals come back from
 * the server, so what you see is the database's answer rather than an
 * arithmetic guess made in the browser.
 */
(function () {
  const rows = document.querySelectorAll('tr.change-row');
  if (!rows.length) return;

  function headers() {
    return { 'Content-Type': 'application/json', 'X-CSRFToken': window.CSRF_TOKEN };
  }

  function money(n) {
    if (!n) return '—';
    const a = Math.abs(n).toLocaleString('en-SG',
      { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return n < 0 ? '(' + a + ')' : a;
  }

  function paint(row, status) {
    row.className = 'change-row status-' + status;
    const cell = row.querySelector('td:last-child');

    if (status === 'pending') {
      cell.innerHTML =
        '<button class="btn btn-sm btn-success" data-decide="accepted">✓ Accept</button> ' +
        '<button class="btn btn-sm btn-danger" data-decide="rejected">✕ Reject</button>';
    } else {
      const pill = status === 'accepted'
        ? '<span class="pill pill-green">✓ Accepted</span>'
        : '<span class="pill pill-red">✕ Rejected</span>';
      cell.innerHTML = pill +
        ' <button class="btn btn-sm" data-decide="pending">undo</button>';
    }
  }

  document.addEventListener('click', async event => {
    const btn = event.target.closest('[data-decide]');
    if (!btn) return;

    const row = btn.closest('tr.change-row');
    if (!row) return;

    const decision = btn.dataset.decide;
    btn.disabled = true;
    row.style.opacity = '.55';

    try {
      const response = await fetch(
        `/trial-balance/api/change/${row.dataset.changeId}`,
        { method: 'POST', headers: headers(),
          body: JSON.stringify({ decision: decision }) });

      const data = await response.json();
      row.style.opacity = '';

      if (!data.ok) {
        alert(data.error || 'Could not record that decision.');
        btn.disabled = false;
        return;
      }

      paint(row, data.status);

      const diff = document.getElementById('tb-live-diff');
      if (diff && data.totals) {
        diff.textContent = money(data.totals.difference);
        diff.style.color = data.totals.balanced ? 'var(--green)' : 'var(--red)';
      }
    } catch (err) {
      row.style.opacity = '';
      btn.disabled = false;
      alert('Could not reach the server.');
    }
  });
})();
