/* Statement editing: override a figure, or map an unmapped account. */
(function () {
  const table = document.getElementById('statement-table');
  if (!table) return;

  const statementId = table.dataset.statementId;

  function csrfHeaders() {
    return {
      'Content-Type': 'application/json',
      'X-CSRFToken': window.CSRF_TOKEN
    };
  }

  function formatMoney(n) {
    if (n === 0 || n === null) return '';
    return Number(n).toLocaleString('en-SG', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2
    });
  }

  /* ------------------------------------------------------- overrides ---- */

  table.addEventListener('blur', async event => {
    const input = event.target;
    if (!input.classList || !input.classList.contains('amount')) return;

    const value = input.value.trim();
    if (input.dataset.original === value) return;
    input.dataset.original = value;

    const lineId = input.dataset.lineId;
    const row = input.closest('tr');
    row.style.opacity = '.6';

    try {
      const response = await fetch(`/statements/api/line/${lineId}`, {
        method: 'PATCH',
        headers: csrfHeaders(),
        body: JSON.stringify({ amount: value })
      });

      const data = await response.json();
      row.style.opacity = '';

      if (!data.ok) {
        alert(data.error || 'Could not save that value.');
        return;
      }

      row.classList.toggle('overridden', data.overridden);

      // Totals and subtotals depend on this line, so refresh every figure
      // the server recalculated.
      (data.lines || []).forEach(line => {
        const field = table.querySelector(`input.amount[data-line-id="${line.id}"]`);
        if (field && document.activeElement !== field) {
          field.value = formatMoney(line.amount);
          field.dataset.original = field.value;
          field.closest('tr').classList.toggle('overridden', line.overridden);
        }
      });
    } catch (err) {
      row.style.opacity = '';
      alert('Could not reach the server.');
    }
  }, true);

  table.addEventListener('keydown', event => {
    if (event.target.classList.contains('amount') && event.key === 'Enter') {
      event.target.blur();
    }
  });

  table.querySelectorAll('input.amount').forEach(input => {
    input.dataset.original = input.value.trim();
  });

  /* ------------------------------------------------- account mapping ---- */

  document.querySelectorAll('.map-select').forEach(select => {
    select.addEventListener('change', async () => {
      const lineKey = select.value;
      if (!lineKey) return;

      const label = select.dataset.label;
      select.disabled = true;

      try {
        const response = await fetch('/statements/api/map', {
          method: 'POST',
          headers: csrfHeaders(),
          body: JSON.stringify({
            label: label,
            line_key: lineKey,
            statement_id: parseInt(statementId, 10)
          })
        });
        if (!response.ok) throw new Error();
        // Rebuild happened server-side; reload to show the updated figures.
        location.reload();
      } catch (err) {
        select.disabled = false;
        alert('Could not save that mapping.');
      }
    });
  });
})();
