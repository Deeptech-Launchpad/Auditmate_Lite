/* Review & Correct grid.
 *
 * Every edit autosaves to the JSON API on blur, flips the row to "corrected",
 * and clears its flag. Totals recalculate live so the auditor can see a trial
 * balance come into balance as they fix rows.
 */
(function () {
  const grid = document.getElementById('review-grid');
  if (!grid) return;

  const documentId = grid.dataset.documentId;
  const readOnly = grid.dataset.readonly === 'true';
  const statusEl = document.getElementById('save-status');

  function csrfHeaders() {
    return {
      'Content-Type': 'application/json',
      'X-CSRFToken': window.CSRF_TOKEN
    };
  }

  function setStatus(text, kind) {
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.style.color = kind === 'error' ? 'var(--red)'
                         : kind === 'ok' ? 'var(--green)'
                         : 'var(--text-muted)';
  }

  function parseNumber(value) {
    if (!value || !value.trim()) return null;
    const cleaned = value.replace(/[, ]/g, '').replace(/^\((.*)\)$/, '-$1');
    const n = parseFloat(cleaned);
    return isNaN(n) ? null : n;
  }

  function formatMoney(n) {
    return n.toLocaleString('en-SG', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2
    });
  }

  /* ---------------------------------------------------------- totals ---- */

  function recalcTotals() {
    let debit = 0, credit = 0, hasDC = false;

    grid.querySelectorAll('tbody tr').forEach(row => {
      // Discarded rows are excluded by the auditor; the document's own
      // total is excluded because adding it would count the document twice.
      if (row.classList.contains('row-discarded')) return;
      if (row.dataset.total === '1') return;
      const d = parseNumber(row.querySelector('[data-field="debit"]').value);
      const c = parseNumber(row.querySelector('[data-field="credit"]').value);
      if (d !== null) { debit += d; hasDC = true; }
      if (c !== null) { credit += c; hasDC = true; }
    });

    const debitEl = document.getElementById('total-debit');
    const creditEl = document.getElementById('total-credit');
    const diffEl = document.getElementById('total-diff');

    if (!hasDC) {
      debitEl.textContent = creditEl.textContent = diffEl.textContent = '—';
      diffEl.style.color = '';
      return;
    }

    const difference = debit - credit;
    debitEl.textContent = formatMoney(debit);
    creditEl.textContent = formatMoney(credit);
    diffEl.textContent = formatMoney(difference);
    diffEl.style.color = Math.abs(difference) < 0.01 ? 'var(--green)' : 'var(--red)';

    updateBanner(debit, credit, difference);
  }

  /* The banner at the top of the page is rendered by the server. Left alone
     it keeps showing the figures from page load, so discarding a duplicate
     row appeared to change nothing and the auditor kept chasing a
     difference that had already moved. */
  function updateBanner(debit, credit, difference) {
    const banner = document.getElementById('tb-check');
    if (!banner) return;

    const balanced = Math.abs(difference) < 0.01;
    banner.className = 'notice ' + (balanced ? 'ok' : 'danger');
    banner.querySelector('.tb-check-icon').textContent = balanced ? '✓' : '✕';
    banner.querySelector('.tb-check-body').innerHTML = balanced
      ? '<strong>Trial balance check:</strong> debits and credits both '
        + formatMoney(debit) + '. This document balances.'
      : '<strong>Trial balance check:</strong> Debits ' + formatMoney(debit)
        + ' · Credits ' + formatMoney(credit)
        + ' · <strong>out by ' + formatMoney(Math.abs(difference))
        + '</strong> — something was misread or a row is missing.';
  }

  /* ----------------------------------------------------------- saving --- */

  async function saveRow(row, field, value) {
    const itemId = row.dataset.itemId;
    if (!itemId) return;

    row.classList.add('row-saving');
    setStatus('Saving…');

    const payload = {};
    payload[field] = value;

    try {
      const response = await fetch(`/documents/api/line-item/${itemId}`, {
        method: 'PATCH',
        headers: csrfHeaders(),
        body: JSON.stringify(payload)
      });

      if (!response.ok) throw new Error('save failed');

      // The row has now been checked by a human, so drop the amber flag.
      row.classList.remove('row-saving', 'row-flagged');
      row.classList.add('row-corrected');

      const confidence = row.querySelector('.conf');
      if (confidence) {
        confidence.className = 'conf conf-high';
        confidence.textContent = '100%';
      }

      const acceptBtn = row.querySelector('[data-action="accept"]');
      if (acceptBtn) acceptBtn.remove();

      setStatus('All changes saved', 'ok');
      updateVerifyButton();
    } catch (err) {
      row.classList.remove('row-saving');
      setStatus('Could not save — check your connection', 'error');
    }
  }

  async function rowAction(row, action) {
    const itemId = row.dataset.itemId;
    try {
      const response = await fetch(
        `/documents/api/line-item/${itemId}/${action}`,
        { method: 'POST', headers: csrfHeaders() }
      );
      if (!response.ok) throw new Error();

      if (action === 'discard') {
        row.classList.remove('row-flagged', 'row-corrected');
        row.classList.add('row-discarded');
      } else {
        row.classList.remove('row-flagged');
        row.classList.add('row-corrected');
      }

      const btn = row.querySelector('[data-action="accept"]');
      if (btn) btn.remove();

      setStatus('Saved', 'ok');
      recalcTotals();
      updateVerifyButton();
    } catch (err) {
      setStatus('Action failed', 'error');
    }
  }

  function updateVerifyButton() {
    const btn = document.getElementById('verify-btn');
    if (!btn) return;
    const remaining = grid.querySelectorAll('tr.row-flagged').length;
    btn.disabled = remaining > 0;
    btn.title = remaining > 0
      ? `${remaining} flagged row(s) still to resolve`
      : 'Release this data to the financial statements';
  }

  /* ------------------------------------------------------ interaction --- */

  grid.addEventListener('blur', event => {
    const input = event.target;
    if (!input.classList || !input.classList.contains('cell')) return;
    if (readOnly) return;

    const row = input.closest('tr');
    const field = input.dataset.field;
    let value = input.value.trim();

    if (field !== 'label') {
      const n = parseNumber(value);
      value = n === null ? '' : String(n);
      input.value = n === null ? '' : formatMoney(n);
    }

    if (input.dataset.original === value) return;
    input.dataset.original = value;

    saveRow(row, field, value);
    recalcTotals();
  }, true);

  // Enter moves down the column; Escape reverts the cell.
  grid.addEventListener('keydown', event => {
    const input = event.target;
    if (!input.classList || !input.classList.contains('cell')) return;

    if (event.key === 'Enter') {
      event.preventDefault();
      const row = input.closest('tr');
      const index = Array.from(row.querySelectorAll('.cell')).indexOf(input);
      const nextRow = row.nextElementSibling;
      if (nextRow) {
        const target = nextRow.querySelectorAll('.cell')[index];
        if (target) { target.focus(); target.select(); }
      }
    }

    if (event.key === 'Escape') {
      input.value = input.dataset.original || '';
      input.blur();
    }
  });

  grid.addEventListener('click', event => {
    const btn = event.target.closest('[data-action]');
    if (!btn || readOnly) return;
    rowAction(btn.closest('tr'), btn.dataset.action);
  });

  if (readOnly) {
    /*
     * A verified document is locked on purpose - its figures already feed
     * the trial balance. But a control that simply does nothing reads as a
     * broken app, so each one answers the click instead.
     */
    const WHY = 'This document is verified, so it is read only. ' +
                'Press "Reopen for editing" at the top to correct it.';

    grid.querySelectorAll('input.cell').forEach(input => {
      input.readOnly = true;
      input.title = WHY;
      input.addEventListener('focus', () => setStatus(WHY, 'error'));
    });

    grid.querySelectorAll('[data-action]').forEach(button => {
      button.title = WHY;
      button.addEventListener('click', () => setStatus(WHY, 'error'));
    });

    const add = document.getElementById('add-row-btn');
    if (add) {
      add.title = WHY;
      add.addEventListener('click', () => setStatus(WHY, 'error'));
    }

    setStatus('Verified — read only. Reopen for editing to make changes.');
  }

  /* -------------------------------------------------------- add a row --- */

  const addBtn = document.getElementById('add-row-btn');
  if (addBtn) {
    addBtn.addEventListener('click', async () => {
      // The read-only handler above only explains; this is what stops it.
      if (readOnly) return;
      const label = prompt('Account / description for the new line:');
      if (label === null || !label.trim()) return;

      try {
        const response = await fetch(`/documents/api/document/${documentId}/row`, {
          method: 'POST',
          headers: csrfHeaders(),
          body: JSON.stringify({ label: label.trim() })
        });
        if (!response.ok) throw new Error();
        location.reload();
      } catch (err) {
        setStatus('Could not add row', 'error');
      }
    });
  }

  // Seed each cell's baseline so we only save genuine changes.
  grid.querySelectorAll('input.cell').forEach(input => {
    input.dataset.original = input.value.trim();
  });

  recalcTotals();
  updateVerifyButton();
})();
