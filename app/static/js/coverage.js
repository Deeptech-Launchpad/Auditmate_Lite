/*
 * Coverage: what is in the trial balance but never reaches the report.
 *
 * There are two ways an account goes missing, and they need different
 * answers, so they are shown separately:
 *
 *   unmapped  - no standard key at all. The trial balance already flags
 *               these, and the fix is to map the account there.
 *   orphaned  - mapped to a key that no statement template consumes. This
 *               is the dangerous one: it looks resolved on the trial
 *               balance and silently never appears in a statement.
 *
 * The panel leads with the money, not the count. Two stranded accounts
 * worth twelve dollars and two worth 340,000 are not the same problem, and
 * an auditor deciding what to look at first needs to see which they have.
 */
(function () {
  const card = document.getElementById('coverage-card');
  if (!card) return;

  const fyId = card.dataset.fyId;
  const body = document.getElementById('coverage-body');
  const hint = document.getElementById('coverage-hint');

  function money(value) {
    const amount = Math.round(Number(value) || 0);
    return Math.abs(amount).toLocaleString('en-US');
  }

  function csrfHeaders() {
    return { 'Content-Type': 'application/json',
             'X-CSRFToken': window.CSRF_TOKEN };
  }

  function accountRow(entry, extra) {
    const code = entry.code ? '<span class="cv-code">' + entry.code + '</span> ' : '';
    return '<tr>'
         + '<td>' + code + entry.name + '</td>'
         + '<td class="cv-num">' + money(entry.net) + '</td>'
         + '<td class="cv-key">' + (entry.standard_key || '&mdash;') + '</td>'
         + '<td class="cv-fix">' + (extra || '') + '</td>'
         + '</tr>';
  }

  function table(title, why, rows, extraFor) {
    if (!rows.length) return '';
    return '<div class="cv-group">'
      + '<h3>' + title + ' <span class="cv-count">' + rows.length + '</span></h3>'
      + '<p class="muted small">' + why + '</p>'
      + '<table class="cv-table"><thead><tr>'
      + '<th>Account</th><th class="cv-num">Net</th>'
      + '<th>Mapped to</th><th></th>'
      + '</tr></thead><tbody>'
      + rows.map(r => accountRow(r, extraFor ? extraFor(r) : '')).join('')
      + '</tbody></table></div>';
  }

  let suggestions = {};

  function render(data) {
    if (!data.ok) {
      body.innerHTML = '<p class="muted">Coverage could not be checked.</p>';
      return;
    }

    if (data.clean) {
      hint.textContent = data.accounts_total + ' accounts, all presented';
      hint.className = 'hint ok';
      body.innerHTML = '<div class="cv-clean">'
        + '<strong>Every account reaches the report.</strong> '
        + 'All ' + data.accounts_total + ' accounts in the approved trial '
        + 'balance appear in a statement. Nothing was dropped.</div>';
      return;
    }

    hint.textContent = data.missing_count + ' account(s) missing · S$'
                     + money(data.missing_value);
    hint.className = 'hint warn';

    const fix = function (entry) {
      const found = suggestions[entry.name];
      if (!found) return '';
      const by = found.by === 'ai' ? 'AI' : 'rule';
      return '<span class="cv-sugg" title="' + (found.why || '') + '">'
           + 'suggest <code>' + found.suggested_key + '</code>'
           + ' <em>(' + by + ')</em></span>';
    };

    body.innerHTML =
        '<div class="cv-summary">'
      + '<strong>S$' + money(data.missing_value) + '</strong> across '
      + data.missing_count + ' account(s) is in the trial balance but not in '
      + 'the report. The statements will not agree with the books until this '
      + 'is resolved.</div>'
      + table('Never reaches a statement',
              'Mapped to a key that no statement template uses. It looks '
              + 'resolved on the trial balance and silently never appears.',
              data.orphaned, fix)
      + table('Not mapped at all',
              'No standard key, so nothing knows where to put it. Fix these '
              + 'on the Trial Balance page.',
              data.unmapped, fix)
      + '<div class="cv-actions">'
      + '<button type="button" class="btn" id="cv-suggest">Suggest where these belong</button>'
      + '<a class="btn" href="/trial-balance/fy/' + fyId + '">Open Trial Balance</a>'
      + '</div>'
      + '<p class="muted small" id="cv-note"></p>';

    const button = document.getElementById('cv-suggest');
    if (button) button.addEventListener('click', () => suggest(data));
  }

  async function suggest(coverageData) {
    const button = document.getElementById('cv-suggest');
    const note = document.getElementById('cv-note');
    button.disabled = true;
    button.textContent = 'Thinking…';

    try {
      const response = await fetch('/reports/api/fy/' + fyId + '/suggest', {
        method: 'POST', headers: csrfHeaders(),
        body: JSON.stringify({ use_ai: true })
      });
      const data = await response.json();

      suggestions = {};
      (data.suggestions || []).forEach(s => { suggestions[s.name] = s; });
      render(coverageData);

      const placed = (data.suggestions || []).length;
      const stuck = (data.unresolved || []).length;
      const after = document.getElementById('cv-note');
      if (after) {
        let message = placed + ' suggestion(s); ' + stuck + ' still unplaced.';
        if (data.ai_error) message += ' AI unavailable: ' + data.ai_error;
        message += ' Suggestions are proposals - apply them on the Trial '
                 + 'Balance page, where the change is recorded.';
        after.textContent = message;
      }
    } catch (err) {
      if (note) note.textContent = 'Could not get suggestions.';
      button.disabled = false;
      button.textContent = 'Suggest where these belong';
    }
  }

  fetch('/reports/api/fy/' + fyId + '/coverage')
    .then(response => response.json())
    .then(render)
    .catch(() => {
      body.innerHTML = '<p class="muted">Coverage could not be checked.</p>';
      hint.textContent = '';
    });
})();
