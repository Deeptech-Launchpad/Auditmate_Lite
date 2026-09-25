/* Searchable statement-line dropdowns.
 *
 * Every <select class="map-select"> - the account-to-line choice on the mapping
 * page, the trial balance grid and the statement page - lists a hundred or so
 * lines under their statements, and picking one meant scrolling the list. This
 * puts a search box in front of it: type "deposit" and only the lines with
 * "deposit" in their name (or their statement's) are listed.
 *
 * The <select> stays in the page, hidden, and is what everything else reads and
 * listens to: choosing a line sets its value and fires its own 'change' event,
 * so the code that saves the mapping is untouched.
 */
(function () {
  'use strict';

  const STYLE = `
    .ss-wrap { position: relative; display: block; min-width: 180px; }
    .ss-input { width: 100%; box-sizing: border-box; padding: 5px 24px 5px 7px;
      border: 1px solid var(--border, #d8d4c8); border-radius: 5px;
      font: inherit; font-size: 12.5px; background: #fff; color: inherit;
      text-overflow: ellipsis; cursor: pointer; }
    .ss-input:focus { outline: 2px solid rgba(37, 99, 168, .35); cursor: text; }
    .ss-input.is-empty { color: #8a8577; }
    .ss-caret { position: absolute; right: 8px; top: 50%; transform: translateY(-50%);
      pointer-events: none; font-size: 9px; color: #8a8577; }
    .ss-menu { position: absolute; z-index: 1000; left: 0; right: 0; top: 100%;
      min-width: 260px; max-height: 280px; overflow-y: auto; background: #fff;
      border: 1px solid var(--border-strong, #b9b3a2); border-radius: 6px;
      box-shadow: 0 6px 18px rgba(0, 0, 0, .16); margin-top: 2px; }
    .ss-group { padding: 6px 10px 3px; font-size: 10.5px; font-weight: 700;
      text-transform: uppercase; letter-spacing: .4px; color: #8a8577;
      background: #f6f4ee; position: sticky; top: 0; }
    .ss-item { padding: 5px 10px 5px 16px; font-size: 12.5px; cursor: pointer; }
    .ss-item.is-active, .ss-item:hover { background: #e8f0fb; }
    .ss-item.is-selected { font-weight: 700; }
    .ss-none { padding: 10px; font-size: 12px; color: #8a8577; }
  `;
  const style = document.createElement('style');
  style.textContent = STYLE;
  document.head.appendChild(style);

  const isLocked = select =>
    select.disabled || select.dataset.locked !== undefined ||
    select.getAttribute('aria-disabled') === 'true';

  function enhance(select) {
    if (select.dataset.searchable) return;
    select.dataset.searchable = '1';

    const wrap = document.createElement('div');
    wrap.className = 'ss-wrap';
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'ss-input';
    input.autocomplete = 'off';
    input.setAttribute('aria-label', 'Statement line');
    const caret = document.createElement('span');
    caret.className = 'ss-caret';
    caret.textContent = '▾';
    const menu = document.createElement('div');
    menu.className = 'ss-menu';
    menu.hidden = true;

    select.parentNode.insertBefore(wrap, select);
    wrap.appendChild(input);
    wrap.appendChild(caret);
    wrap.appendChild(menu);
    wrap.appendChild(select);
    select.style.display = 'none';

    let active = -1;
    let shown = [];

    const chosenText = () => {
      const option = select.options[select.selectedIndex];
      return option && option.value ? option.text.trim() : '';
    };
    const paintInput = () => {
      const text = chosenText();
      input.value = text;
      input.placeholder = 'Search lines…';
      input.classList.toggle('is-empty', !text);
      input.disabled = select.disabled;
    };

    function build(query) {
      const words = query.toLowerCase().split(/\s+/).filter(Boolean);
      menu.textContent = '';
      shown = [];
      let group = null;
      let groupNode = null;
      Array.from(select.options).forEach(option => {
        const parent = option.parentElement;
        const groupName = parent && parent.tagName === 'OPTGROUP' ? parent.label : '';
        const text = option.text.trim();
        if (!option.value) return;               // "not mapped" is the empty box
        const haystack = (text + ' ' + groupName).toLowerCase();
        if (!words.every(w => haystack.includes(w))) return;
        if (groupName !== group) {
          group = groupName;
          groupNode = document.createElement('div');
          groupNode.className = 'ss-group';
          groupNode.textContent = groupName;
          if (groupName) menu.appendChild(groupNode);
        }
        const item = document.createElement('div');
        item.className = 'ss-item' + (option.selected ? ' is-selected' : '');
        item.textContent = text;
        item.dataset.value = option.value;
        item.addEventListener('mousedown', event => {
          event.preventDefault();                 // keep focus until we choose
          choose(option.value);
        });
        menu.appendChild(item);
        shown.push(item);
      });
      if (!shown.length) {
        const none = document.createElement('div');
        none.className = 'ss-none';
        none.textContent = 'No line matches “' + query + '”';
        menu.appendChild(none);
      }
      active = shown.findIndex(i => i.classList.contains('is-selected'));
      mark();
    }

    function mark() {
      shown.forEach((item, i) => item.classList.toggle('is-active', i === active));
      if (active >= 0 && shown[active]) shown[active].scrollIntoView({ block: 'nearest' });
    }

    function open() {
      if (isLocked(select)) return;
      build('');
      menu.hidden = false;
      input.select();
    }
    function close() {
      menu.hidden = true;
      paintInput();
    }
    function choose(value) {
      close();
      if (select.value !== value) {
        select.value = value;
        select.dispatchEvent(new Event('change', { bubbles: true }));
      }
      paintInput();
    }

    input.addEventListener('focus', () => { paintInput(); open(); });
    input.addEventListener('mousedown', event => {
      if (select.dataset.locked !== undefined || select.getAttribute('aria-disabled') === 'true') {
        // the page's own "this trial balance is approved" message
        select.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
        event.preventDefault();
      }
    });
    input.addEventListener('input', () => { menu.hidden = false; build(input.value); });
    input.addEventListener('keydown', event => {
      if (event.key === 'Tab') { close(); return; }
      if (menu.hidden && (event.key === 'ArrowDown' || event.key === 'Enter')) {
        open(); event.preventDefault(); return;
      }
      if (event.key === 'ArrowDown') { active = Math.min(active + 1, shown.length - 1); mark(); event.preventDefault(); }
      else if (event.key === 'ArrowUp') { active = Math.max(active - 1, 0); mark(); event.preventDefault(); }
      else if (event.key === 'Enter') {
        if (active >= 0 && shown[active]) choose(shown[active].dataset.value);
        event.preventDefault();
      } else if (event.key === 'Escape') { close(); input.blur(); }
      else if (event.key === 'Backspace' && !input.value && !menu.hidden) { /* keep the box open */ }
    });
    input.addEventListener('blur', () => setTimeout(close, 120));
    select.addEventListener('change', paintInput);
    paintInput();
  }

  function enhanceAll(root) {
    (root || document).querySelectorAll('select.map-select').forEach(enhance);
  }

  // The grid is redrawn as rows are added or changed, and the page locks its
  // dropdowns after load; pick up new ones and keep the locked look in step.
  let pending = false;
  new MutationObserver(() => {
    if (pending) return;
    pending = true;
    setTimeout(() => { pending = false; enhanceAll(); }, 60);
  }).observe(document.documentElement, { childList: true, subtree: true });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => enhanceAll());
  } else {
    enhanceAll();
  }
})();
