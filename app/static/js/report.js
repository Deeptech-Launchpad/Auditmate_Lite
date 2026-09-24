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

  /* ----------------------------------------------------------- search --- */

  const search = document.getElementById('section-search');
  const searchEmpty = document.getElementById('section-search-empty');

  if (search) {
    search.addEventListener('input', () => {
      const query = search.value.trim().toLowerCase();
      let visible = 0;

      list.querySelectorAll('.section-item').forEach(item => {
        const title = item.querySelector('.stitle').textContent.toLowerCase();
        const matches = !query || title.includes(query);
        item.classList.toggle('search-hidden', !matches);
        if (matches) visible += 1;
      });

      if (searchEmpty) searchEmpty.hidden = visible > 0;
    });
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

  /* ------------------------------------------ add and delete a note ---- */
  /*
   * A note the library never anticipated - a subsequent event, a related
   * party transaction, a contingent liability. The page reloads afterwards
   * rather than building the row in JavaScript: the new section has to
   * appear in the preview as well as the list, and one rendering path that
   * is certainly right beats two that agree most of the time.
   */

  const openBtn = document.getElementById('open-add-note');
  const addForm = document.getElementById('add-note-form');
  if (openBtn && addForm) {
    const parentSelect = document.getElementById('anf-parent');
    const submitBtn = document.getElementById('anf-submit');
    const cancelBtn = document.getElementById('anf-cancel');
    const titleField = document.getElementById('anf-title');

    openBtn.addEventListener('click', () => {
      openBtn.hidden = true;
      addForm.hidden = false;
      titleField.focus();
    });
    cancelBtn.addEventListener('click', () => {
      addForm.hidden = true;
      openBtn.hidden = false;
      titleField.value = '';
      addForm.querySelectorAll('input[type="checkbox"]').forEach(el => { el.checked = false; });
      addForm.querySelector('input[name="anf-scope"][value="engagement"]').checked = true;
      parentSelect.value = '';
    });

    submitBtn.addEventListener('click', async () => {
      const title = (titleField.value || '').trim();
      if (!title) { titleField.focus(); return; }

      const parentId = parentSelect.value || null;

      const accountKeys = Array.from(
        addForm.querySelectorAll('input[name="anf-account"]:checked'))
        .map(el => el.value);
      const scope = addForm.querySelector('input[name="anf-scope"]:checked').value;

      submitBtn.disabled = true;
      try {
        const response = await fetch(
          `/reports/api/report/${addForm.dataset.reportId}/section`,
          { method: 'POST', headers: csrfHeaders(),
            body: JSON.stringify({
              title, parent_section_id: parentId,
              account_keys: accountKeys, save_scope: scope,
            }) });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data.ok) {
          window.alert(data.error || 'Could not add the note.');
          submitBtn.disabled = false;
          return;
        }
        window.location.reload();
      } catch (err) {
        window.alert('Could not add the note.');
        submitBtn.disabled = false;
      }
    });
  }

  list.addEventListener('click', async event => {
    const button = event.target.closest('.delete-section');
    if (!button) return;
    /* Read from a data attribute, never interpolated into the markup.
       Autoescaping turns an apostrophe in a title into &#39;, which the HTML
       parser decodes before the JavaScript is compiled - so a note called
       "Director's loan" would have broken the script, or worse. */
    if (!window.confirm(button.dataset.confirm || 'Delete this note?')) return;

    const response = await fetch(
      `/reports/api/section/${button.dataset.sectionId}`,
      { method: 'DELETE', headers: csrfHeaders() });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) {
      window.alert(data.error || 'Could not delete the note.');
      return;
    }
    window.location.reload();
  });

  /* ------------------------------------------------- in-place editing ---- */
  /*
   * The report itself is the editor. Narrative sections are contenteditable
   * and save when focus leaves them. Statement labels and figures are
   * editable too, but they route to their own endpoints - see the second
   * block at the foot of this file - so a changed figure is stored as a
   * recorded override with the calculation kept underneath, rather than
   * being typed over the page with nothing behind it.
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

  /* A company fact on the cover (directors, registered office...). Its text is
     read with line breaks, and it is saved to the customer record. */
  const MULTILINE_COVER = ['directors', 'office'];

  report.addEventListener('focusin', event => {
    const field = event.target.closest('[contenteditable="true"]');
    if (!field) return;
    if (field.dataset.coverField) {
      original.set(field, field.innerText.trim());
      return;
    }
    const plain = field.dataset.field === 'title'
               || field.dataset.labelKey || isCell(field);
    original.set(field, plain ? field.textContent.trim() : serialise(field));
  });

  report.addEventListener('focusout', async event => {
    const field = event.target.closest('[contenteditable="true"]');
    if (!field) return;

    if (field.dataset.coverField) {
      const text = field.innerText.trim();
      if (text === original.get(field)) return;
      say('Saving…', 'saving');
      try {
        const response = await fetch(
          '/reports/api/section/' + field.dataset.sectionId + '/cover-field',
          { method: 'PATCH', headers: csrfHeaders(),
            body: JSON.stringify({ field: field.dataset.coverField,
                                   value: text }) });
        if (!response.ok) throw new Error('save failed');
        original.set(field, text);
        say('Saved to the customer record', 'saved');
      } catch (err) {
        say('Could not save — your text is still here', 'failed');
      }
      return;
    }

    /* A fixed caption on the cover page: stored on the section, not on any
       statement line, so it is handled here rather than by the cell hook. */
    if (field.dataset.labelKey) {
      const text = field.textContent.trim();
      if (text === original.get(field)) return;
      say('Saving…', 'saving');
      try {
        const labels = {};
        labels[field.dataset.labelKey] = text;
        await patchSection(field.dataset.sectionId, { labels: labels });
        original.set(field, text);
        say('Saved', 'saved');
      } catch (err) {
        say('Could not save — your text is still here', 'failed');
      }
      return;
    }

    /* Figures and labels save through their own endpoints - see the second
       block below, which owns them and exposes this hook. */
    if (isCell(field)) {
      if (window.__auditmateSaveCell) await window.__auditmateSaveCell(field);
      return;
    }

    const key = field.dataset.field || 'content_html';
    const value = key === 'title' ? field.textContent.trim() : serialise(field);

    if (value === original.get(field)) return;      /* nothing changed */

    /* Wording is overridable too (OV-02), and on the same terms as a
       figure: the library's sentence is kept, the new one prints, and a
       reason is recorded. Saved before the body, so the id can be written
       into the paragraph - that is what keeps the record with its
       sentence when the note is later reordered or rebuilt. */
    if (key === 'content_html') {
      const kept = await recordWording(field, original.get(field) || '');
      if (!kept) {
        field.innerHTML = original.get(field) || '';
        say('Change not saved - no reason given', 'failed');
        return;
      }
    }

    say('Saving…', 'saving');
    try {
      await patchSection(field.dataset.sectionId,
                         { [key]: key === 'content_html'
                                  ? serialise(field) : value });
      original.set(field, value);
      say('Saved', 'saved');
      if (key === 'title') syncTitle(field.dataset.sectionId, value);
    } catch (err) {
      say('Could not save — your text is still here', 'failed');
    }
  });

  /* Which elements of a note body count as a paragraph a person can type
     over. Headings and list items are wording the accounts print, so they
     are in; a table placed by the note is not - its rows have their own
     override path with their own source figures. */
  const PARAGRAPHS = 'p, h4, li, blockquote';

  /* Record every paragraph that changed, one reason covering the edit.
     Returns false only if the person declined to give one. */
  async function recordWording(field, before) {
    const was = document.createElement('div');
    was.innerHTML = before;
    const old = Array.from(was.querySelectorAll(PARAGRAPHS));
    const now = Array.from(field.querySelectorAll(PARAGRAPHS));

    /* Same number of paragraphs: match them up and record the ones that
       differ. A different number means the person added or removed one,
       which no position can describe honestly - so the whole body is
       recorded as the change, with the previous text kept beside it. */
    const changes = [];
    if (old.length === now.length) {
      now.forEach((node, index) => {
        const previous = old[index];
        if (node.textContent.trim() === previous.textContent.trim()) return;
        changes.push({
          node: node,
          para_id: node.dataset.para || previous.dataset.para || '',
          override_id: node.dataset.override || previous.dataset.override || '',
          source_text: previous.innerHTML.trim(),
          wording: node.innerHTML.trim(),
          what: 'The library says: "' + previous.textContent.trim().slice(0, 220)
                + '"'
        });
      });
    } else {
      changes.push({
        node: null, para_id: '', override_id: '',
        source_text: before, wording: serialise(field),
        what: 'This note had ' + old.length + ' paragraph(s) and now has '
              + now.length + '.'
      });
    }
    if (!changes.length) return true;

    const reason = await window.__auditmateAskReason(changes[0].what);
    if (!reason) return false;

    for (const change of changes) {
      try {
        const response = await fetch('/reports/api/note-paragraph', {
          method: 'PATCH', headers: csrfHeaders(),
          body: JSON.stringify({
            section_id: field.dataset.sectionId,
            para_id: change.para_id,
            override_id: change.override_id || null,
            source_text: change.source_text,
            wording: change.wording,
            reason: reason
          })
        });
        const data = await response.json();
        if (data.ok && data.override_id && change.node) {
          change.node.setAttribute('data-override', data.override_id);
        }
      } catch (err) {
        /* The wording still saves below; the record is what is missing,
           and saying so is better than losing the person's sentence. */
        say('Saved, but the reason was not recorded', 'failed');
      }
    }
    return true;
  }

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
    if (event.key === 'Enter'
        && (field.dataset.field === 'title' || field.dataset.labelKey
            || (field.dataset.coverField
                && !MULTILINE_COVER.includes(field.dataset.coverField)))) {
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

  /* A held paragraph is answered in its note: it applies (the wording is added
     to the note) or it does not (left out). Saved for the engagement, then the
     page is reloaded on the same note so the wording shows where it sits. */
  document.addEventListener('click', async event => {
    const button = event.target.closest('.confirm-box button[data-decision]');
    if (!button) return;
    const box = button.closest('.confirm-box');
    button.disabled = true;
    try {
      const response = await fetch('/reports/api/confirm-paragraph', {
        method: 'PATCH', headers: csrfHeaders(),
        body: JSON.stringify({ section_id: box.dataset.sectionId,
                               para_id: box.dataset.paraId,
                               decision: button.dataset.decision })
      });
      const data = await response.json();
      if (!data.ok) throw new Error(data.error || 'failed');
      try { sessionStorage.setItem('am-scroll-section', box.dataset.sectionId); }
      catch (err) { /* private window: the page still reloads */ }
      window.location.reload();
    } catch (err) {
      button.disabled = false;
      say('Could not save the answer', 'failed');
    }
  });

  /* After answering, come back to the same note. */
  try {
    const back = sessionStorage.getItem('am-scroll-section');
    if (back) {
      sessionStorage.removeItem('am-scroll-section');
      const note = report.querySelector(
        `[data-section-id="${back}"][data-field="content_html"]`);
      if (note) note.scrollIntoView({ block: 'center' });
    }
  } catch (err) { /* nothing to restore */ }

  /* "Go to the note" for a held paragraph lands on its own confirm box. */
  document.addEventListener('click', event => {
    const link = event.target.closest('a.gap-fix[href^="#confirm-"]');
    if (!link) return;
    const box = report.querySelector('#' + link.getAttribute('href').slice(1));
    if (!box) return;                         // let the plain anchor do its job
    event.preventDefault();
    box.scrollIntoView({ behavior: 'smooth', block: 'center' });
    box.classList.add('section-found');
    setTimeout(() => box.classList.remove('section-found'), 2200);
  });

  /* "Go to the note" in the gaps panel lands on the note itself.
     The href is an anchor on the section's row in the list, which is the
     right fallback with no JavaScript, but scrolling to a row in a list
     leaves the preparer to find the note they were sent to. Taken over
     here so the jump ends on the wording, with the note held for a moment
     so it is obvious which of fifty-three was meant. */
  document.addEventListener('click', event => {
    const link = event.target.closest('a.gap-fix[href^="#sec-"]');
    if (!link) return;
    const id = link.getAttribute('href').slice(5);
    const field = report && report.querySelector(
      `[data-section-id="${id}"][data-field="content_html"]`);
    const row = list && list.querySelector(`.section-item[data-section-id="${id}"]`);
    if (!field && !row) return;             // let the plain anchor do its job
    event.preventDefault();
    if (row) {
      row.classList.add('section-found');
      setTimeout(() => row.classList.remove('section-found'), 2200);
    }
    const target = field || row;
    target.scrollIntoView({ behavior: 'smooth', block: 'center' });
    if (field) {
      field.classList.add('section-found');
      setTimeout(() => field.classList.remove('section-found'), 2200);
      if (field.isContentEditable) field.focus();
    }
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

  /* Cleared, or typed back to what the source gave: a revert, not a new
     override. The source figure comes back and no reason is asked for -
     the reason for the change that is being withdrawn is already on the
     record, and stays there (OV-07). */
  function isRevert(field, value) {
    if (field.dataset.field !== 'amount') return false;
    if (value === '') return true;
    const was = field.dataset.computed;
    if (was === undefined || was === '' || was === 'None') return false;
    const typed = value.replace(/,/g, '').replace(/^\((.*)\)$/, '-$1');
    return Math.round(Number(typed)) === Math.round(Number(was));
  }

  async function saveCell(field) {
    const key = field.dataset.field;                 /* 'label' | 'amount' */
    const value = field.textContent.trim();

    /* A note figure or caption is an override of what the engine
       assembled, so it needs a reason before it is stored. A statement
       line goes through the statements' own override path, which has
       carried its own record since long before the library asked for one. */
    let reason = null;
    const isFigure = field.dataset.field === 'amount';
    /* A caption is presentation - one client says Revenue, another
       Turnover - and is simply stored. A figure is a change to what the
       accounts state, wherever it sits, so it needs a reason: a line on
       the face of the balance sheet is no different from a row in a note,
       and the reviewer reads the two side by side. */
    if ((isFigure || !field.dataset.lineId) && !isRevert(field, value)) {
      const was = field.dataset.field === 'amount'
        ? fmt(field.dataset.computed)
        : (field.dataset.sourceLabel || '');
      reason = await window.__auditmateAskReason(
        (field.dataset.sourceLabel || 'This row') + ': the source gives '
        + was + '; the accounts will print ' + (value || '--') + '.');
      if (!reason) {
        /* Nobody will explain it, so it does not happen. */
        field.textContent = field.dataset.field === 'amount'
          ? fmt(field.dataset.computed) : (field.dataset.sourceLabel || '');
        say('Change not saved - no reason given', 'failed');
        return;
      }
    }

    let url, body;
    if (field.dataset.lineId) {
      url = '/reports/api/line/' + field.dataset.lineId;
      body = { reason: reason };
      body[key] = value;
    } else {
      url = '/reports/api/note-row';
      body = {
        section_id: field.dataset.sectionId,
        table_index: Number(field.dataset.tableIndex),
        row_index: Number(field.dataset.rowIndex),
        anchor_label: field.dataset.anchor,
        reason: reason
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
      if (data.cleared && data.amount !== undefined && data.amount !== null) {
        field.textContent = fmt(data.amount);
        const cell = field.closest('td');
        if (cell) cell.classList.remove('is-overridden');
        const mark = cell && cell.querySelector('.ov-mark');
        if (mark) mark.remove();
        say('Put back to ' + fmt(data.amount), 'saved');
        return;
      }
      say(reason ? 'Saved, with the reason' : 'Saved', 'saved');
    } catch (err) {
      say('Could not save - the figure on screen is not stored', 'failed');
    }
  }

  window.__auditmateSaveCell = saveCell;

  /* Answering an Incomplete cell directly in the note. Not an override -
     there is no source figure to contradict, only a blank the Figures
     page would ask the same question about - so it saves straight
     through document_fields.save() and asks for no reason, the same act
     as typing it on that page.

     Reloads on success rather than patching the one cell in place:
     filling this figure can settle whether OTHER cells in the same note
     are still held (a total that was Incomplete because one of its parts
     was), and reloading is the only way every one of them is sure to
     catch up, not just the one just typed into. */
  report.addEventListener('focusin', event => {
    const field = event.target.closest('.held-answer');
    if (!field) return;
    field.dataset.original = field.textContent.trim();
  });

  report.addEventListener('focusout', async event => {
    const field = event.target.closest('.held-answer');
    if (!field) return;
    const value = field.textContent.trim();
    if (value === (field.dataset.original || '')) return;

    say('Saving…', 'saving');
    try {
      const response = await fetch('/reports/api/document-figure', {
        method: 'PATCH', headers: csrfHeaders(),
        body: JSON.stringify({
          financial_year_id: Number(field.dataset.fyId),
          token: field.dataset.token,
          field: field.dataset.docField,
          scope: field.dataset.scope,
          member: field.dataset.member,
          amount: value
        })
      });
      const data = await response.json();
      if (!data.ok) {
        field.textContent = field.dataset.original || '';
        say(data.error || 'Could not save', 'failed');
        return;
      }
      say(value === '' ? 'Cleared - reloading' : 'Saved - reloading', 'saved');
      location.reload();
    } catch (err) {
      field.textContent = field.dataset.original || '';
      say('Could not save - the figure was not stored', 'failed');
    }
  });

  report.addEventListener('keydown', event => {
    const field = event.target.closest('.held-answer');
    if (!field) return;
    if (event.key === 'Enter') { event.preventDefault(); field.blur(); }
    if (event.key === 'Escape') {
      event.preventDefault();
      field.textContent = field.dataset.original || '';
      field.blur();
    }
  });


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
    if (data.kind === 'computed' && data.depends_on && data.depends_on.length) {
      body = data.depends_on.map(group =>
        '<div class="src-group"><div class="src-group-label">' + group.via + '</div>'
        + '<ul class="src-list">' + group.accounts.map(sourceRow).join('') + '</ul></div>'
      ).join('')
      + (data.formula ? '<p class="src-none">Totalled by <code>' + data.formula
                        + '</code>.</p>' : '');
    } else if (data.kind === 'computed') {
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

    // Below the figure by default - but a figure near the bottom of a long
    // report page has no room there, and the panel opening straight off the
    // bottom of the screen cut off its own Close button. Flip it above the
    // figure instead whenever below would not fit and above does.
    const viewportBottom = window.scrollY + window.innerHeight;
    const belowTop = window.scrollY + box.bottom + 6;
    const aboveTop = window.scrollY + box.top - panel.offsetHeight - 6;
    const fitsBelow = belowTop + panel.offsetHeight <= viewportBottom - 8;
    panel.style.top = ((fitsBelow || aboveTop < window.scrollY)
      ? belowTop : aboveTop) + 'px';

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

    const legend = document.getElementById('src-legend');
    const paint = function () {
      report.classList.toggle('show-sources', on);
      toggle.setAttribute('aria-pressed', on ? 'true' : 'false');
      toggle.classList.toggle('on', on);
      if (legend) legend.classList.toggle('on', on);
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

/* ------------------------------------------------------------------------
   Why was this changed?

   Library 3.5 puts manual entry on every figure and every paragraph, and
   asks one thing back: a reason, entered at the time (Overrides sheet,
   OV-04). Short is fine; blank is not. The reason is what makes the
   override reviewable a year later, when the person who made it has
   forgotten, so it is asked for at the moment of the edit and never
   afterwards.

   Cancelling puts back what was on the page. An edit nobody will explain
   is an edit that does not happen - which is the point.
   ------------------------------------------------------------------------ */
window.__auditmateAskReason = (function () {
  let box = null;

  function build() {
    box = document.createElement('div');
    box.className = 'reason-veil';
    box.hidden = true;
    box.innerHTML =
      '<div class="reason-box" role="dialog" aria-modal="true"'
      + ' aria-labelledby="reason-title">'
      + '<h3 id="reason-title">Why is this being changed?</h3>'
      + '<p class="reason-what"></p>'
      + '<p class="reason-note">Kept with the change and shown to whoever'
      + ' reviews the draft. It does not alter the trial balance or any'
      + ' document behind it — only what the accounts print.</p>'
      + '<textarea class="reason-text" rows="3" maxlength="600"'
      + ' placeholder="e.g. agreed to the signed 2022 accounts"></textarea>'
      + '<p class="reason-error" hidden>A reason is required.</p>'
      + '<div class="reason-buttons">'
      + '<button type="button" class="btn btn-sm reason-cancel">Cancel</button>'
      + '<button type="button" class="btn btn-sm btn-primary reason-ok">'
      + 'Save the change</button>'
      + '</div></div>';
    document.body.appendChild(box);
  }

  return function ask(what) {
    if (!box) build();
    const text = box.querySelector('.reason-text');
    const error = box.querySelector('.reason-error');
    box.querySelector('.reason-what').textContent = what || '';
    text.value = '';
    error.hidden = true;
    box.hidden = false;
    setTimeout(() => text.focus(), 0);

    return new Promise(resolve => {
      function done(value) {
        box.hidden = true;
        box.removeEventListener('click', onClick);
        text.removeEventListener('keydown', onKey);
        resolve(value);
      }
      function accept() {
        const reason = text.value.trim();
        if (!reason) { error.hidden = false; text.focus(); return; }
        done(reason);
      }
      function onClick(event) {
        if (event.target.closest('.reason-ok')) accept();
        else if (event.target.closest('.reason-cancel') || event.target === box) {
          done(null);
        }
      }
      function onKey(event) {
        if (event.key === 'Escape') done(null);
        if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) accept();
      }
      box.addEventListener('click', onClick);
      text.addEventListener('keydown', onKey);
    });
  };
})();
