/* Drag-and-drop upload with a file preview list. */
(function () {
  const dropzone = document.getElementById('dropzone');
  const input = document.getElementById('file-input');
  const list = document.getElementById('file-list');
  const submit = document.getElementById('submit-btn');
  const form = document.getElementById('upload-form');

  if (!dropzone || !input) return;

  const MAX_SIZE = 25 * 1024 * 1024;
  const ALLOWED = ['pdf', 'xlsx', 'xls', 'csv', 'docx', 'png', 'jpg', 'jpeg'];

  // DataTransfer lets us keep an editable list the file input can read back.
  let staged = new DataTransfer();

  function humanSize(bytes) {
    const units = ['B', 'KB', 'MB', 'GB'];
    let n = bytes, i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + ' ' + units[i];
  }

  function extensionOf(name) {
    const parts = name.split('.');
    return parts.length > 1 ? parts.pop().toLowerCase() : '';
  }

  function render() {
    list.innerHTML = '';

    Array.from(staged.files).forEach((file, index) => {
      const ext = extensionOf(file.name);
      const tooBig = file.size > MAX_SIZE;
      const badType = !ALLOWED.includes(ext);

      const row = document.createElement('div');
      row.className = 'file-row';
      row.innerHTML =
        '<div class="ftype">' + (ext || '?').toUpperCase().slice(0, 4) + '</div>' +
        '<div class="fname">' + file.name + '</div>' +
        '<div class="fsize">' + humanSize(file.size) + '</div>';

      if (tooBig || badType) {
        const warn = document.createElement('span');
        warn.className = 'pill pill-red';
        warn.textContent = tooBig ? 'Too large' : 'Unsupported';
        row.appendChild(warn);
      }

      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'icon-btn no';
      remove.textContent = '✕';
      remove.title = 'Remove';
      remove.addEventListener('click', () => {
        const kept = new DataTransfer();
        Array.from(staged.files).forEach((f, i) => {
          if (i !== index) kept.items.add(f);
        });
        staged = kept;
        input.files = staged.files;
        render();
      });
      row.appendChild(remove);

      list.appendChild(row);
    });

    const valid = Array.from(staged.files).filter(
      f => f.size <= MAX_SIZE && ALLOWED.includes(extensionOf(f.name))
    );
    submit.disabled = valid.length === 0;
    submit.textContent = valid.length
      ? `Upload & extract ${valid.length} file${valid.length === 1 ? '' : 's'}`
      : 'Upload & extract';
  }

  function alreadyStaged(file) {
    return Array.from(staged.files).some(
      f => f.name === file.name
        && f.size === file.size
        && f.lastModified === file.lastModified
    );
  }

  function addFiles(fileList) {
    Array.from(fileList).forEach(f => {
      // The same file picked twice is the same document, not two of them.
      if (!alreadyStaged(f)) staged.items.add(f);
    });
    input.files = staged.files;
    render();
  }

  dropzone.addEventListener('click', () => input.click());

  // Adds to the list rather than replacing it. The picker hands back only
  // what was chosen in THAT dialog, so rebuilding the staged list from it
  // threw away everything chosen before - which made it impossible to send
  // up a batch a few files at a time, or to add one more after picking the
  // rest. Dropping files already added to the list this way too.
  input.addEventListener('change', () => addFiles(input.files));

  ['dragenter', 'dragover'].forEach(evt =>
    dropzone.addEventListener(evt, e => {
      e.preventDefault();
      dropzone.classList.add('dragging');
    })
  );

  ['dragleave', 'drop'].forEach(evt =>
    dropzone.addEventListener(evt, e => {
      e.preventDefault();
      dropzone.classList.remove('dragging');
    })
  );

  dropzone.addEventListener('drop', e => {
    if (e.dataTransfer && e.dataTransfer.files.length) {
      addFiles(e.dataTransfer.files);
    }
  });

  // Extraction runs during the POST, so give the user feedback rather than a
  // frozen-looking page.
  form.addEventListener('submit', () => {
    submit.disabled = true;
    submit.textContent = 'Uploading & extracting…';
  });
})();
