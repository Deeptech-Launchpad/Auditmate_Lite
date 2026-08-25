/* Standard Trial Balance grid.
 *
 * Edits autosave. The footer totals and the unmapped count come back from the
 * server after every change, so the balance check is always the truth from
 * the database rather than something the browser worked out separately.
 */
(function () {
  const grid = document.getElementById('tb-grid');
  if (!grid) return;

  const readOnly = grid.dataset.readonly === 'true';
  const statusEl = document.getElementById('tb-status');

  function headers() {
    return { 'Content-Type': 'application/json', 'X-CSRFToken': window.CSRF_TOKEN };
  }

  function setStatus(text, kind) {
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.style.color = kind === 'error' ? 'var(--red)'
                         : kind === 'ok' ? 'var(--green)'
                         : 'var(--text-muted)';
  }

  function money(n) {
    if (n === 0) return '—';
    const abs = Math.abs(n).toLocaleString('en-SG',
      { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return n < 0 ? `(${abs})` : abs;
  }

  /* The Total column is Dr less Cr, recomputed in the browser so the row
     closes the moment an amount is edited rather than on the next reload. */
  function applyRowTotal(row) {
    const cell = row.querySelector('.tb-total');
    if (!cell) return;
    const dr = parseNumber(row.querySelector('[data-field="debit"]').value);
    const cr = parseNumber(row.querySelector('[data-field="credit"]').value);
    cell.textContent = money((dr || 0) - (cr || 0));
  }

  function applyTotals(t) {
    if (!t) return;
    document.getElementById('tb-debit').textContent = money(t.debit);
    document.getElementById('tb-credit').textContent = money(t.credit);

    const foot = {
      'foot-debit': money(t.debit),
      'foot-credit': money(t.credit),
      'foot-total': t.difference === 0 ? '0.00' : money(t.difference)
    };
    Object.entries(foot).forEach(([id, value]) => {
      const cell = document.getElementById(id);
      if (cell) cell.textContent = value;
    });

    const diff = document.getElementById('tb-diff');
    diff.textContent = money(t.difference);
    diff.style.color = t.balanced ? 'var(--green)' : 'var(--red)';

    const un = document.getElementById('tb-unmapped');
    un.textContent = t.unmapped;
    un.style.color = t.unmapped ? 'var(--amber)' : 'var(--green)';
  }

  /* Category and FS are consequences of the mapping, not separate choices,
     so they follow it immediately rather than waiting for a reload. */
  function applyClassification(row, data) {
    const category = row.querySelector('.tb-category');
    if (category) {
      category.innerHTML = data.category
        ? escapeHtml(data.category)
        : '<span class="muted">— unmapped —</span>';
    }
    const fs = row.querySelector('.tb-fs');
    if (fs) {
      fs.innerHTML = data.fs
        ? `<span class="pill ${data.fs === 'P&L' ? 'pill-blue' : 'pill-grey'}"
                 style="font-size:10.5px">${escapeHtml(data.fs)}</span>`
        : '<span class="muted">—</span>';
    }
  }

  function escapeHtml(text) {
    const node = document.createElement('span');
    node.textContent = text;
    return node.innerHTML;
  }

  async function patch(row, payload) {
    const id = row.dataset.accountId;
    setStatus('Saving…');
    try {
      const response = await fetch(`/trial-balance/api/account/${id}`, {
        method: 'PATCH', headers: headers(), body: JSON.stringify(payload)
      });
      const data = await response.json();

      if (!response.ok || !data.ok) {
        setStatus(data.error || 'Could not save', 'error');
        return null;
      }

      applyTotals(data.totals);
      applyRowTotal(row);
      applyClassification(row, data);
      setStatus('All changes saved', 'ok');
      return data;
    } catch (err) {
      setStatus('Could not reach the server', 'error');
      return null;
    }
  }

  function parseNumber(value) {
    if (!value || !value.trim()) return '';
    const cleaned = value.replace(/[, ]/g, '').replace(/^\((.*)\)$/, '-$1');
    const n = parseFloat(cleaned);
    return isNaN(n) ? '' : n;
  }

  /* ------------------------------------------------------- cell edits --- */

  grid.addEventListener('blur', async event => {
    const input = event.target;
    if (!input.classList || !input.classList.contains('cell')) return;
    if (readOnly) return;

    const field = input.dataset.field;
    let value = input.value.trim();

    if (field === 'debit' || field === 'credit') {
      const n = parseNumber(value);
      value = n === '' ? '' : String(n);
      input.value = n === '' ? '' : Number(n).toLocaleString('en-SG',
        { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }

    if (input.dataset.original === value) return;
    input.dataset.original = value;

    const payload = {};
    payload[field] = value;
    await patch(input.closest('tr'), payload);
  }, true);

  grid.addEventListener('keydown', event => {
    const input = event.target;
    if (!input.classList || !input.classList.contains('cell')) return;

    if (event.key === 'Enter') {
      event.preventDefault();
      const row = input.closest('tr');
      const index = Array.from(row.querySelectorAll('.cell')).indexOf(input);
      const next = row.nextElementSibling;
      if (next) {
        const target = next.querySelectorAll('.cell')[index];
        if (target) { target.focus(); target.select(); }
      }
    }
    if (event.key === 'Escape') {
      input.value = input.dataset.original || '';
      input.blur();
    }
  });

  /* ---------------------------------------------------------- mapping --- */

  grid.addEventListener('change', async event => {
    const select = event.target;
    if (!select.classList.contains('map-select') || readOnly) return;

    const row = select.closest('tr');
    const data = await patch(row, { standard_key: select.value });
    if (!data) return;

    // An account with a statement line is no longer a problem to fix.
    row.classList.toggle('row-flagged', !data.mapped);
  });

  /* ----------------------------------------------------------- delete --- */

  grid.addEventListener('click', async event => {
    const btn = event.target.closest('[data-action="delete"]');
    if (!btn || readOnly) return;

    const row = btn.closest('tr');
    const name = row.querySelector('[data-field="account_name"]').value;
    if (!confirm(`Remove "${name}" from the trial balance?`)) return;

    try {
      const response = await fetch(
        `/trial-balance/api/account/${row.dataset.accountId}`,
        { method: 'DELETE', headers: headers() });
      if (!response.ok) throw new Error();
      location.reload();
    } catch (err) {
      setStatus('Could not remove that account', 'error');
    }
  });

  if (readOnly) {
    /*
     * An approved trial balance is the source of the statements and the
     * audit report, so it cannot be edited in place - changing a mapping
     * here would silently change figures in a report that may already have
     * gone out. Reopening is the way back, and it is one click away.
     *
     * The controls are left enabled rather than set disabled: a disabled
     * select swallows the click, so it just looks broken. Kept live, it can
     * answer the question the click was asking.
     */
    grid.querySelectorAll('input.cell').forEach(i => { i.readOnly = true; });
    grid.classList.add('is-locked');

    const WHY = 'This trial balance is approved, so it is read only. ' +
                'Use "Reopen to edit" above to change it.';

    grid.querySelectorAll('.map-select').forEach(select => {
      select.dataset.locked = select.value;
      select.setAttribute('aria-disabled', 'true');
      select.title = WHY;
      select.addEventListener('mousedown', event => {
        event.preventDefault();
        select.blur();
        setStatus(WHY, 'error');
      });
      select.addEventListener('keydown', event => {
        if (event.key === 'Tab') return;
        event.preventDefault();
        setStatus(WHY, 'error');
      });
      /* Belt and braces: if a change ever gets through, put it back. */
      select.addEventListener('change', () => {
        select.value = select.dataset.locked;
        setStatus(WHY, 'error');
      });
    });

    grid.querySelectorAll('.icon-btn').forEach(button => {
      button.disabled = true;
      button.title = WHY;
    });

    grid.querySelectorAll('input.cell').forEach(input => {
      input.title = WHY;
      input.addEventListener('focus', () => setStatus(WHY, 'error'));
    });

    setStatus('Approved — read only. Reopen the trial balance to make changes.');
  }

  grid.querySelectorAll('input.cell').forEach(input => {
    input.dataset.original = input.value.trim();
  });
})();
