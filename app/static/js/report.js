/* Report builder: toggle sections, drag to reorder, edit section text. */
(function () {
  const list = document.getElementById('section-list');
  if (!list) return;

  const reportId = list.dataset.reportId;

  function csrfHeaders() {
    return {
      'Content-Type': 'application/json',
      'X-CSRFToken': window.CSRF_TOKEN
    };
  }

  async function patchSection(sectionId, payload) {
    const response = await fetch(`/reports/api/section/${sectionId}`, {
      method: 'PATCH',
      headers: csrfHeaders(),
      body: JSON.stringify(payload)
    });
    if (!response.ok) throw new Error('save failed');
    return response.json();
  }

  /* --------------------------------------------------------- toggles ---- */

  list.addEventListener('change', async event => {
    const box = event.target;
    if (!box.classList.contains('toggle')) return;

    const item = box.closest('.section-item');
    item.classList.toggle('disabled', !box.checked);

    try {
      await patchSection(box.dataset.sectionId, { is_enabled: box.checked });
      location.reload();          // refresh the live preview
    } catch (err) {
      box.checked = !box.checked;
      item.classList.toggle('disabled', !box.checked);
      alert('Could not update that section.');
    }
  });

  /* ------------------------------------------------------ reordering ---- */

  let dragged = null;

  list.addEventListener('dragstart', event => {
    const item = event.target.closest('.section-item');
    if (!item) return;
    dragged = item;
    item.classList.add('dragging');
    event.dataTransfer.effectAllowed = 'move';
  });

  list.addEventListener('dragend', () => {
    if (dragged) dragged.classList.remove('dragging');
    dragged = null;
    persistOrder();
  });

  list.addEventListener('dragover', event => {
    event.preventDefault();
    if (!dragged) return;

    const target = event.target.closest('.section-item');
    if (!target || target === dragged) return;

    const box = target.getBoundingClientRect();
    const after = (event.clientY - box.top) > box.height / 2;
    list.insertBefore(dragged, after ? target.nextSibling : target);
  });

  async function persistOrder() {
    const order = Array.from(list.querySelectorAll('.section-item'))
      .map(item => item.dataset.sectionId);

    try {
      await fetch(`/reports/api/report/${reportId}/reorder`, {
        method: 'POST',
        headers: csrfHeaders(),
        body: JSON.stringify({ order })
      });
    } catch (err) {
      /* order is cosmetic until reload — not worth interrupting the user */
    }
  }

  /* ------------------------------------------------- in-place editing ---- */
  /*
   * The report itself is the editor. Narrative sections are contenteditable
   * and save when focus leaves them; statements and note tables are not
   * editable at all, because they are derived from the approved trial
   * balance and typing over them would put a figure in the report that no
   * trial balance supports.
   *
   * Substituted fields ({{ customer.legal_name }} and friends) render as
   * uneditable ".ph" chips showing the real value. On save each chip is
   * turned back into its placeholder, so editing the words around a field
   * never bakes this year's value into the template.
   */

  const report = document.getElementById('live-report');
  const hint = document.getElementById('save-hint');
  if (!report) return;

  let hintTimer = null;

  function say(message, tone) {
    if (!hint) return;
    hint.textContent = message;
    hint.className = 'hint' + (tone ? ' ' + tone : '');
    clearTimeout(hintTimer);
    if (tone !== 'saving') {
      hintTimer = setTimeout(() => {
        hint.textContent = 'Enabled sections, in order';
        hint.className = 'hint';
      }, 2500);
    }
  }

  /* Chips back to placeholders, so the template survives editing. */
  function serialise(node) {
    const copy = node.cloneNode(true);
    copy.querySelectorAll('.ph').forEach(chip => {
      chip.replaceWith(document.createTextNode('{{ ' + chip.dataset.ph + ' }}'));
    });
    return copy.innerHTML.trim();
  }

  const original = new WeakMap();

  /* A statement figure or label is plain text, not rich content: it must
     never go through serialise(), which would wrap it in markup. */
  function isCell(field) {
    return field.classList.contains('ed-amount')
        || field.classList.contains('ed-label');
  }

  report.addEventListener('focusin', event => {
    const field = event.target.closest('[contenteditable="true"]');
    if (!field) return;
    original.set(field, (field.dataset.field === 'title' || isCell(field))
      ? field.textContent.trim() : serialise(field));
  });

  report.addEventListener('focusout', async event => {
    const field = event.target.closest('[contenteditable="true"]');
    if (!field) return;

    /* Figures and labels save through their own endpoints - see the second
       block below, which owns them and exposes this hook. */
    if (isCell(field)) {
      if (window.__auditmateSaveCell) await window.__auditmateSaveCell(field);
      return;
    }

    const key = field.dataset.field || 'content_html';
    const value = key === 'title' ? field.textContent.trim() : serialise(field);

    if (value === original.get(field)) return;      /* nothing changed */

    say('Saving…', 'saving');
    try {
      await patchSection(field.dataset.sectionId, { [key]: value });
      original.set(field, value);
      say('Saved', 'saved');
      if (key === 'title') syncTitle(field.dataset.sectionId, value);
    } catch (err) {
      say('Could not save — your text is still here', 'failed');
    }
  });

  /* Keep the section list in step with a title edited in the preview. */
  function syncTitle(sectionId, title) {
    const item = list.querySelector(`.section-item[data-section-id="${sectionId}"]`);
    if (!item) return;
    const label = item.querySelector('.stitle');
    if (label && label.childNodes[0]) label.childNodes[0].textContent = title + ' ';
  }

  /* Enter inside a title would insert a line break into a heading. */
  report.addEventListener('keydown', event => {
    const field = event.target.closest('[contenteditable="true"]');
    if (!field) return;
    if (event.key === 'Enter' && field.dataset.field === 'title') {
      event.preventDefault();
      field.blur();
    }
    if (event.key === 'Escape') {
      const key = field.dataset.field || 'content_html';
      if (original.has(field)) {
        if (key === 'title') field.textContent = original.get(field);
        else field.innerHTML = original.get(field);
      }
      field.blur();
    }
  });

  /* Paste as plain text - pasting from Word otherwise drags its styling in. */
  report.addEventListener('paste', event => {
    const field = event.target.closest('[contenteditable="true"]');
    if (!field) return;
    event.preventDefault();
    const text = (event.clipboardData || window.clipboardData).getData('text/plain');
    document.execCommand('insertText', false, text);
  });

  /* The pencil in the section list scrolls to the text and puts the cursor
     in it, rather than opening a second place to edit the same thing. */
  list.addEventListener('click', event => {
    const btn = event.target.closest('.edit-btn');
    if (!btn) return;
    const field = report.querySelector(
      `[contenteditable="true"][data-section-id="${btn.dataset.sectionId}"]` +
      `[data-field="content_html"]`);
    if (!field) return;
    field.scrollIntoView({ behavior: 'smooth', block: 'center' });
    field.focus();
  });

})();

/* ------------------------------------------------------------------------
   Figures and labels on statement and note pages.

   Wording is presentation, so it is simply stored. A figure is not: it goes
   to an endpoint that records it as an override, keeps the computed value
   underneath and recalculates every dependent total. The response carries
   the recalculated statement, which is why the whole table is repainted
   rather than just the cell that was typed in - a changed line moves its
   subtotal, its total, and often the balance sheet's footing.
   ------------------------------------------------------------------------ */
(function () {
  const report = document.getElementById('live-report');
  const hint = document.getElementById('save-hint');
  if (!report) return;

  function csrfHeaders() {
    return { 'Content-Type': 'application/json',
             'X-CSRFToken': window.CSRF_TOKEN };
  }

  function say(message, tone) {
    if (!hint) return;
    hint.textContent = message;
    hint.className = 'hint' + (tone ? ' ' + tone : '');
    if (tone !== 'saving') {
      setTimeout(() => {
        hint.textContent = 'Enabled sections, in order';
        hint.className = 'hint';
      }, 2500);
    }
  }

  /* Same presentation as the report's own `stmt` filter: whole dollars,
     thousands separated, negatives in brackets, nil as a double hyphen. */
  function fmt(value) {
    if (value === null || value === undefined || value === '') return '--';
    const amount = Math.round(Number(value));
    if (!isFinite(amount) || amount === 0) return '--';
    const body = Math.abs(amount).toLocaleString('en-US');
    return amount < 0 ? '(' + body + ')' : body;
  }

  /* Repaint a statement after the server recalculated it. */
  function applyLines(lines) {
    lines.forEach(line => {
      const cell = report.querySelector(
        '.ed-amount[data-line-id="' + line.id + '"]');
      if (cell) {
        cell.textContent = fmt(line.amount);
        cell.dataset.computed = line.amount;
        const td = cell.closest('td');
        if (td) {
          td.classList.toggle('is-overridden', !!line.overridden);
          if (line.overridden) td.classList.remove('from-tb', 'is-computed');
        }
      }
      const label = report.querySelector(
        '.ed-label[data-line-id="' + line.id + '"]');
      if (label) label.classList.toggle('is-edited', !!line.label_overridden);
    });
  }

  async function saveCell(field) {
    const key = field.dataset.field;                 /* 'label' | 'amount' */
    const value = field.textContent.trim();

    let url, body;
    if (field.dataset.lineId) {
      url = '/reports/api/line/' + field.dataset.lineId;
      body = {};
      body[key] = value;
    } else {
      url = '/reports/api/note-row';
      body = {
        section_id: field.dataset.sectionId,
        table_index: Number(field.dataset.tableIndex),
        row_index: Number(field.dataset.rowIndex),
        anchor_label: field.dataset.anchor
      };
      body[key] = value;
    }

    say('Saving...', 'saving');
    try {
      const response = await fetch(url, {
        method: 'PATCH', headers: csrfHeaders(), body: JSON.stringify(body)
      });
      const data = await response.json();
      if (!data.ok) {
        /* A typo must not silently become a nil figure. Put back what was
           there and say why. */
        say(data.error || 'Could not save', 'failed');
        if (key === 'amount') field.textContent = fmt(field.dataset.computed);
        return;
      }
      if (data.lines) applyLines(data.lines);
      say('Saved', 'saved');
    } catch (err) {
      say('Could not save - the figure on screen is not stored', 'failed');
    }
  }

  window.__auditmateSaveCell = saveCell;

  /* Enter commits a cell rather than inserting a line break into a table. */
  report.addEventListener('keydown', event => {
    const field = event.target.closest('.ed-amount, .ed-label');
    if (!field) return;
    if (event.key === 'Enter') { event.preventDefault(); field.blur(); }
    if (event.key === 'Escape') {
      event.preventDefault();
      if (field.dataset.field === 'amount') {
        field.textContent = fmt(field.dataset.computed);
      }
      field.blur();
    }
  });

  /* --------------------------------------------------------- sources --- */

  let panel = null;

  function closePanel() {
    if (panel) { panel.remove(); panel = null; }
  }

  function sourceRow(account) {
    const mapped = account.mapped_by === 'auditor'
      ? '<span class="by-auditor">mapped by auditor</span>'
      : '<span class="by-auto">mapped automatically</span>';
    const where = account.document
      ? account.document + (account.category ? ' · ' + account.category : '')
      : (account.source === 'xero' ? 'Xero' : 'entered by hand');
    const code = account.code ? account.code + '  ' : '';
    return '<li><div class="src-line">'
         + '<span class="src-name">' + code + account.name + '</span>'
         + '<span class="src-amt">' + fmt(account.amount) + '</span>'
         + '</div><div class="src-meta">' + where + ' · ' + mapped
         + '</div></li>';
  }

  function openPanel(anchor, data) {
    closePanel();
    panel = document.createElement('div');
    panel.className = 'src-panel';

    let body;
    if (data.kind === 'computed') {
      body = '<p class="src-none">Calculated from other lines'
           + (data.formula ? ' <code>' + data.formula + '</code>' : '')
           + '. It has no accounts of its own - change the lines it adds up.</p>';
    } else if (!data.accounts || !data.accounts.length) {
      body = '<p class="src-none">Nothing in the trial balance maps here, so'
           + ' this line prints nil. If that is wrong the account is either'
           + ' unmapped or mapped elsewhere - check Coverage.</p>';
    } else {
      body = '<ul class="src-list">'
           + data.accounts.map(sourceRow).join('') + '</ul>';
    }

    const overridden = data.overridden
      ? '<p class="src-over">Overridden by an auditor. The calculated figure'
        + ' was <strong>' + fmt(data.computed_amount) + '</strong>.'
        + ' Clear the cell to put it back.</p>'
      : '';

    panel.innerHTML = '<div class="src-head">'
      + (data.label || 'Where this came from') + '</div>'
      + body + overridden
      + '<button type="button" class="src-close">Close</button>';

    document.body.appendChild(panel);
    const box = anchor.getBoundingClientRect();
    panel.style.top = (window.scrollY + box.bottom + 6) + 'px';
    panel.style.left = Math.max(8, Math.min(
      window.scrollX + box.left - 240,
      window.scrollX + document.documentElement.clientWidth
        - panel.offsetWidth - 12)) + 'px';

    panel.querySelector('.src-close').addEventListener('click', closePanel);
  }

  report.addEventListener('click', async event => {
    const dot = event.target.closest('.src-dot');
    if (!dot) return;
    event.preventDefault();

    const url = dot.dataset.lineId
      ? '/reports/api/line/' + dot.dataset.lineId + '/sources'
      : '/reports/api/account/' + dot.dataset.accountId + '/sources';
    try {
      const response = await fetch(url);
      const data = await response.json();
      if (data.ok) openPanel(dot, data);
    } catch (err) {
      say('Could not load the sources', 'failed');
    }
  });

  document.addEventListener('click', event => {
    if (panel && !panel.contains(event.target)
        && !event.target.closest('.src-dot')) closePanel();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') closePanel();
  });

  /* ---------------------------------------------------- sources toggle -- */

  const toggle = document.getElementById('show-sources');
  if (toggle) {
    /* Remembered per browser so the working preference survives a reload,
       and never travels with the document. */
    let on = true;
    try { on = localStorage.getItem('auditmate.showSources') !== '0'; }
    catch (e) { /* private window: fall back to on */ }

    const paint = function () {
      report.classList.toggle('show-sources', on);
      toggle.setAttribute('aria-pressed', on ? 'true' : 'false');
      toggle.classList.toggle('on', on);
    };
    toggle.addEventListener('click', () => {
      on = !on;
      try { localStorage.setItem('auditmate.showSources', on ? '1' : '0'); }
      catch (e) { /* not fatal */ }
      paint();
    });
    paint();
  }
})();
