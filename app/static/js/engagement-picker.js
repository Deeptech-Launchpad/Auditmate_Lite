/*
 * Engagement picker: type to filter, arrow keys to move, Enter to open.
 *
 * A plain <select> is fine for six engagements and unusable for a hundred -
 * you cannot scroll to a name you already know. Here the auditor types what
 * they know (the customer, or the year) and the list narrows to it.
 *
 * The whole list is already in the page, so filtering costs no round trip
 * and works with the keyboard from the first character.
 */
(function () {
  const picker = document.getElementById('engagement-picker');
  if (!picker) return;

  const input = document.getElementById('engagement-search');
  const list = document.getElementById('engagement-list');
  const items = Array.from(list.querySelectorAll('li[data-url]'));
  const noMatch = list.querySelector('.no-match');

  let active = -1;

  function open() {
    list.hidden = false;
    input.setAttribute('aria-expanded', 'true');
  }

  function close() {
    list.hidden = true;
    input.setAttribute('aria-expanded', 'false');
    setActive(-1);
    input.value = '';
    filter();
  }

  function visible() {
    return items.filter(item => !item.hidden);
  }

  function setActive(index) {
    const shown = visible();
    items.forEach(item => item.classList.remove('active'));
    active = index;
    if (index >= 0 && index < shown.length) {
      shown[index].classList.add('active');
      /* Keep the highlighted row inside the scrolling panel. */
      shown[index].scrollIntoView({ block: 'nearest' });
    }
  }

  function filter() {
    /* Match on every word typed, in any order, so "marina 2025" and
       "2025 marina" both find the same engagement. */
    const words = input.value.toLowerCase().split(/\s+/).filter(Boolean);

    items.forEach(item => {
      const haystack = item.dataset.search || '';
      item.hidden = !words.every(word => haystack.includes(word));
    });

    const count = visible().length;
    if (noMatch) noMatch.hidden = count > 0;
    setActive(count ? 0 : -1);
  }

  function go(item) {
    if (item && item.dataset.url) window.location.href = item.dataset.url;
  }

  input.addEventListener('focus', () => { open(); filter(); });
  input.addEventListener('input', () => { open(); filter(); });

  input.addEventListener('keydown', event => {
    const shown = visible();

    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (list.hidden) { open(); filter(); return; }
      const step = event.key === 'ArrowDown' ? 1 : -1;
      /* Wrap, so holding one arrow key still reaches everything. */
      setActive((active + step + shown.length) % (shown.length || 1));
      return;
    }

    if (event.key === 'Enter') {
      event.preventDefault();
      go(shown[active] || shown[0]);
      return;
    }

    if (event.key === 'Escape') {
      close();
      input.blur();
    }
  });

  list.addEventListener('mousedown', event => {
    /* mousedown, not click: the input's blur would hide the list first. */
    const item = event.target.closest('li[data-url]');
    if (item) { event.preventDefault(); go(item); }
  });

  document.addEventListener('click', event => {
    if (!picker.contains(event.target)) close();
  });
})();
