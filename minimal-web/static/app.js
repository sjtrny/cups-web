'use strict';
const $ = id => document.getElementById(id);
let printer = null, upload = null, previewPage = 1, previewPages = 1, timer, previewRequest, revision = 0, uploadRevision = 0;
let uploadRequest, printerRevision = 0, activeRequests = 0, previewPending = false;
const primary = new Set(['PageSize', 'media', 'copies', 'BrPriority', 'print-quality']);
const jobStates = {3: 'Queued', 4: 'Held', 5: 'Printing', 6: 'Stopped', 7: 'Cancelled', 8: 'Aborted', 9: 'Completed'};

function message(text, error = false) {
  $('status').textContent = text;
  $('status').className = error ? 'text-danger' : '';
}


function activity() {
  $('loading-indicator').hidden = activeRequests === 0 && !previewPending;
  $('preview-loading').hidden = !previewPending;
  $('preview-area').setAttribute('aria-busy', String(previewPending));
  $('preview').classList.toggle('opacity-50', previewPending);
  $('previous').disabled = previewPending || previewPage <= 1;
  $('next').disabled = previewPending || previewPage >= previewPages;
}

function previewBusy(active, label = 'Updating preview…') {
  previewPending = active;
  if (active) {
    $('preview-loading-text').textContent = label;
    $('print').disabled = true;
  }
  activity();
}

function selectTab(name, focus = false) {
  for (const id of ['print', 'queue']) {
    const selected = id === name;
    const tab = $(id + '-tab');
    tab.classList.toggle('active', selected);
    tab.setAttribute('aria-selected', String(selected));
    tab.tabIndex = selected ? 0 : -1;
    $(id + '-panel').hidden = !selected;
  }
  if (focus) $(name + '-tab').focus();
  if (name === 'queue') loadJobs().catch(showError);
}

function adminHeader() {
  if (!$('admin-password').value) {
    selectTab('print');
    $('admin-controls').open = true;
    $('admin-password').focus();
    throw new Error('Enter the administrator password first.');
  }
  return 'Basic ' + btoa('admin:' + $('admin-password').value);
}

async function api(path, body, administrator = false, signal, foreground = true) {
  const headers = {'X-Print-UI': '1'};
  if (administrator) headers.Authorization = adminHeader();
  const params = {headers, signal};
  if (body !== undefined) {
    params.method = 'POST';
    if (body instanceof FormData) params.body = body;
    else {
      headers['Content-Type'] = 'application/json';
      params.body = JSON.stringify(body);
    }
  }
  if (foreground) activeRequests++;
  activity();
  try {
    const response = await fetch(path, params);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `Request failed (${response.status}).`);
    return data;
  } finally {
    if (foreground) activeRequests--;
    activity();
  }
}

function optionControl(option) {
  const wrapper = document.createElement('div');
  wrapper.className = 'col-6';
  const label = document.createElement('label');
  label.className = 'form-label';
  label.htmlFor = 'option-' + option.key;
  label.textContent = option.label;
  const field = document.createElement(['number', 'text-list'].includes(option.kind) ? 'input' : 'select');
  field.id = label.htmlFor;
  field.dataset.option = option.key;
  if (['number', 'text-list'].includes(option.kind)) {
    field.type = option.kind === 'number' ? 'number' : 'text';
    field.className = 'form-control';
    field.min = option.min;
    field.max = option.max;
    field.step = option.step || 1;
    field.value = option.default;
    field.required = true;
    if (option.kind === 'text-list') field.title = 'Comma-separated choices: ' + option.choices.map(c => c.value).join(', ');
  } else {
    field.className = 'form-select';
    field.multiple = option.kind === 'multiple';
    const selected = new Set(String(option.default).split(','));
    for (const choice of option.choices) {
      const element = new Option(choice.label, choice.value);
      element.selected = selected.has(choice.value) || (choice.value === 'Custom' && String(option.default).startsWith('Custom.'));
      field.append(element);
    }
  }
  wrapper.append(label, field);
  return wrapper;
}

function printerState() {
  if (!printer) return;
  const state = {3: 'Idle', 4: 'Printing', 5: 'Paused'}[printer.state] || 'Unknown state';
  $('printer-state').textContent = state + (printer.accepting ? '' : ' · Not accepting jobs');
}

async function loadPrinter() {
  const version = ++printerRevision;
  revision++;
  clearTimeout(timer);
  previewRequest?.abort();
  previewBusy(Boolean(upload), 'Loading printer settings…');
  $('print').disabled = true;
  $('printer-info').disabled = true;
  $('capabilities').textContent = 'Loading printer capabilities…';
  if (!$('printer').value) {
    printer = null;
    $('capabilities').textContent = 'No printer selected.';
    previewBusy(false);
    message('No printers configured in CUPS.', true);
    return;
  }
  const name = $('printer').value;
  message('Loading printer settings…');
  let result;
  try {
    result = await api('/api/printers/' + encodeURIComponent(name));
  } catch (error) {
    if (version !== printerRevision) return;
    previewBusy(false);
    throw error;
  }
  if (version !== printerRevision || $('printer').value !== name) return;
  printer = result;
  for (const id of ['main-controls', 'driver-controls', 'other-controls']) $(id).replaceChildren();
  for (const option of printer.options) {
    const group = primary.has(option.key) ? 'main-controls' : option.source === 'driver' ? 'driver-controls' : 'other-controls';
    $(group).append(optionControl(option));
  }
  const layout = printer.layout;
  $('scale').value = layout.scale;
  $('percent').value = layout.percent;
  $('offset-x').value = layout.offset_x ?? 0;
  $('offset-y').value = layout.offset_y ?? 0;
  $('rotation').value = layout.rotation;
  $('crop').checked = layout.crop;
  $('pages').value = '';
  $('extra').value = '';
  $('capabilities').textContent = JSON.stringify(printer.attributes, null, 2);
  $('printer-info').disabled = false;
  if (printer.custom) {
    for (const axis of ['width', 'height']) {
      const field = $('paper-' + axis), limits = printer.custom[axis];
      field.min = limits.min;
      field.max = limits.max;
      field.value = Math.max(limits.min, Math.min(limits.max, axis === 'width' ? 102 : 152));
    }
    const value = printer.options.find(o => o.key === 'PageSize')?.default;
    const match = /^Custom\.([\d.]+)x([\d.]+)(mm|in|pt)?$/.exec(value || '');
    if (match) {
      const factor = match[3] === 'mm' ? 1 : match[3] === 'in' ? 25.4 : 25.4 / 72;
      $('paper-width').value = Number(match[1]) * factor;
      $('paper-height').value = Number(match[2]) * factor;
    }
  }
  printerState();
  schedulePreview();
  if (!upload && !$('file').files.length) message('Select a document to preview and print.');
}

async function loadPrinters() {
  const selected = $('printer').value;
  const result = await api('/api/printers');
  $('printer').replaceChildren(...result.printers.map(p => new Option(p.info || p.name, p.name)));
  $('printer').value = result.printers.some(p => p.name === selected) ? selected : result.default || result.printers[0]?.name || '';
  await loadPrinter();
}

function requestDocument() {
  if (!printer) throw new Error('Select a printer.');
  if (!$('print-form').reportValidity()) throw new Error('Check the highlighted print setting.');
  const options = {};
  for (const field of document.querySelectorAll('[data-option]')) {
    options[field.dataset.option] = field.multiple ? [...field.selectedOptions].map(o => o.value).join(',') : field.value;
  }
  if ($('pages').value.trim()) options['page-ranges'] = $('pages').value.trim();
  if (options.PageSize === 'Custom' && printer.custom) options.PageSize = `Custom.${$('paper-width').value}x${$('paper-height').value}mm`;
  for (const line of $('extra').value.split('\n')) {
    if (!line.trim()) continue;
    const position = line.indexOf('=');
    if (position < 1) throw new Error('Additional options must look like option=value.');
    options[line.slice(0, position).trim()] = line.slice(position + 1).trim();
  }
  return {upload: upload?.id, printer: printer.name, options,
    layout: {scale: $('scale').value, percent: $('scale').value === 'custom' ? Number($('percent').value) : 100,
      offset_x: Number($('offset-x').value), offset_y: Number($('offset-y').value),
      rotation: Number($('rotation').value), crop: $('crop').checked}};
}

function schedulePreview() {
  $('percent-field').hidden = $('scale').value !== 'custom';
  $('percent').disabled = $('scale').value !== 'custom';
  const custom = Boolean(printer?.custom && $('option-PageSize')?.value === 'Custom');
  $('custom-paper').hidden = !custom;
  $('paper-width').disabled = $('paper-height').disabled = !custom;
  $('print').disabled = true;
  clearTimeout(timer);
  revision++;
  previewRequest?.abort();
  if (!upload || !printer) {
    previewBusy(false);
    return;
  }
  previewBusy(true);
  message('Updating preview…');
  timer = setTimeout(() => refreshPreview().catch(showError), 120);
}

async function refreshPreview() {
  if (!upload || !printer) return;
  const version = revision;
  previewRequest?.abort();
  const controller = new AbortController();
  previewRequest = controller;
  previewBusy(true);
  message('Updating preview…');
  try {
    const payload = requestDocument();
    payload.page = previewPage;
    const result = await api('/api/preview', payload, false, controller.signal);
    if (version !== revision || controller.signal.aborted) return;
    const image = new Image();
    image.src = result.image;
    await image.decode();
    if (version !== revision || controller.signal.aborted) return;
    previewPages = result.pages;
    $('preview').src = result.image;
    $('preview').hidden = false;
    $('preview-controls').hidden = false;
    $('preview-page').textContent = `${previewPage} / ${previewPages}`;
    $('download').href = result.document;
    $('print').disabled = false;
    message('Ready to print.');
  } catch (error) {
    if (version !== revision || controller.signal.aborted) return;
    // An N-up/range change can reduce the number of preview pages.
    if (previewPage > 1 && error.message.startsWith('Preview page must')) {
      previewPage = 1;
      revision++;
      return refreshPreview();
    }
    throw error;
  } finally {
    if (version === revision && previewRequest === controller) previewBusy(false);
  }
}

function savedJobs() {
  try { return JSON.parse(localStorage.getItem('print-jobs') || '{}'); }
  catch { return {}; }
}

async function loadJobs(background = false) {
  const result = await api('/api/jobs', undefined, false, undefined, !background);
  const tokens = savedJobs();
  $('jobs').replaceChildren();
  for (const job of result.jobs) {
    const row = document.createElement('tr');
    for (const key of ['id', 'title', 'printer', 'state']) {
      const cell = document.createElement('td');
      if (key === 'title') cell.className = 'text-break';
      if (key === 'printer') cell.className = 'text-break w-25';
      cell.textContent = key === 'state' ? jobStates[job.state] || job.state : job[key];
      row.append(cell);
    }
    const actions = document.createElement('td');
    if (job.state <= 6) {
      for (const [action, label] of [['cancel', 'Cancel'], ...(job.state === 4 ? [['release', 'Release']] : job.state === 3 ? [['hold', 'Hold']] : [])]) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn btn-sm btn-outline-secondary me-1';
        button.textContent = label;
        button.addEventListener('click', async () => {
          button.disabled = true;
          try {
            await api(`/api/jobs/${job.id}/${action}`, {upload: tokens[job.id]}, !tokens[job.id]);
            message(`Job ${job.id}: ${label.toLowerCase()} requested.`);
            await loadJobs();
          } catch (error) { showError(error); button.disabled = false; }
        });
        actions.append(button);
      }
    }
    row.append(actions);
    $('jobs').append(row);
  }
}

function showError(error) {
  if (error.name !== 'AbortError') message(error.message || String(error), true);
}

function handle(id, operation) {
  $(id).addEventListener('click', () => operation().catch(showError));
}

for (const name of ['print', 'queue']) {
  const tab = $(name + '-tab');
  tab.addEventListener('click', () => selectTab(name));
  tab.addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === 'Home' ? 'print' : event.key === 'End' ? 'queue' : name === 'print' ? 'queue' : 'print';
    selectTab(next, true);
  });
}
$('printer-info').addEventListener('click', () => $('capabilities-modal').showModal());
$('close-capabilities').addEventListener('click', () => $('capabilities-modal').close());
$('capabilities-modal').addEventListener('click', event => {
  if (event.target !== $('capabilities-modal')) return;
  const box = event.target.getBoundingClientRect();
  if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) {
    event.target.close();
  }
});

$('printer').addEventListener('change', () => loadPrinter().catch(showError));
$('file').addEventListener('change', async () => {
  const version = ++uploadRevision;
  $('print').disabled = true;
  upload = null;
  revision++;
  clearTimeout(timer);
  previewRequest?.abort();
  uploadRequest?.abort();
  previewBusy(false);
  $('preview').hidden = $('preview-controls').hidden = true;
  const file = $('file').files[0];
  if (!file) return;
  if (file.size > 50 * 1024 * 1024) return message('File exceeds 50 MB.', true);
  const controller = new AbortController();
  uploadRequest = controller;
  previewBusy(true, 'Uploading document…');
  try {
    message('Uploading…');
    const body = new FormData();
    body.append('file', file);
    const result = await api('/api/uploads', body, false, controller.signal);
    if (version !== uploadRevision) return;
    upload = result;
    previewPage = 1;
    $('file-info').textContent = `${upload.name} · ${upload.pages} page${upload.pages === 1 ? '' : 's'}`;
    if (upload.kind === 'image') $('scale').value = 'fit';
    schedulePreview();
  } catch (error) {
    if (version === uploadRevision) showError(error);
  } finally {
    if (version === uploadRevision && !upload) previewBusy(false);
  }
});
$('print-form').addEventListener('input', event => {
  if (event.target !== $('file')) schedulePreview();
});
$('print-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (!upload) return message('Upload a document first.', true);
  $('print').disabled = true;
  try {
    const payload = requestDocument();
    payload.submission = [...crypto.getRandomValues(new Uint8Array(16))].map(v => v.toString(16).padStart(2, '0')).join('');
    message('Submitting print job…');
    const result = await api('/api/print', payload);
    const tokens = savedJobs();
    tokens[result.id] = upload.id;
    localStorage.setItem('print-jobs', JSON.stringify(tokens));
    message(`Job ${result.id} sent to ${result.printer}.`);
    await loadJobs();
  } catch (error) { showError(error); }
  finally { $('print').disabled = !upload || previewPending; }
});
handle('refresh', loadPrinters);
handle('reset', loadPrinter);
handle('refresh-jobs', loadJobs);
handle('previous', async () => { previewPage--; revision++; await refreshPreview(); });
handle('next', async () => { previewPage++; revision++; await refreshPreview(); });
handle('save-defaults', async () => {
  await api('/api/printers/' + encodeURIComponent(printer.name) + '/defaults', requestDocument(), true);
  await loadPrinter();
  message('Printer defaults saved.');
});
for (const action of ['pause', 'resume', 'reject', 'accept', 'make-default']) {
  handle(action, async () => {
    if (!printer) throw new Error('Select a printer.');
    await api('/api/printers/' + encodeURIComponent(printer.name) + '/' + action, {}, true);
    const status = await api('/api/printers/' + encodeURIComponent(printer.name));
    printer.state = status.state;
    printer.accepting = status.accepting;
    printerState();
    message('Printer queue updated.');
  });
}
Promise.all([loadPrinters(), loadJobs()]).catch(showError);
setInterval(() => loadJobs(true).catch(() => {}), 5000);
