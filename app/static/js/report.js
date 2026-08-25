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

  report.addEventListener('focusin', event => {
    const field = event.target.closest('[contenteditable="true"]');
    if (!field) return;
    original.set(field, field.dataset.field === 'title'
      ? field.textContent.trim() : serialise(field));
  });

  report.addEventListener('focusout', async event => {
    const field = event.target.closest('[contenteditable="true"]');
    if (!field) return;

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
