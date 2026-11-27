'use strict';

const $ = (id) => document.getElementById(id);
const TOKEN_KEY = 'nerveForgeToken';
const THEME_KEY = 'nerveForgeTheme';
const PHRASE = 'yesiknow';

let tokenValue = localStorage.getItem(TOKEN_KEY) || '';
let platform = null;
let capabilities = {};
let fullArmed = false;
let jobRunning = false;
let currentFolder = '.';
let currentEntries = [];
let openedFile = '';
let editorDirty = false;
let tokenRevealed = false;

/* ------------------------------------------------------------------ utils */
function notify(message, type = 'ok') {
  const item = document.createElement('div');
  item.className = 'toast' + (type === 'error' ? ' error' : '');
  item.textContent = message;
  $('toastRegion').appendChild(item);
  setTimeout(() => item.remove(), 4500);
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch]));
}

async function request(path, method = 'GET', body = null) {
  const headers = { 'Authorization': 'Bearer ' + tokenValue };
  if (body !== null) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, {
    method, headers, body: body === null ? undefined : JSON.stringify(body), cache: 'no-store'
  });
  let data = {};
  try { data = await response.json(); } catch (_) {}
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

async function localAdmin(path, method = 'GET', body = null) {
  const headers = { 'Authorization': 'Bearer ' + tokenValue };
  if (body !== null) headers['Content-Type'] = 'application/json';
  const response = await fetch('http://127.0.0.1:8767' + path, {
    method, headers, body: body === null ? undefined : JSON.stringify(body), cache: 'no-store'
  });
  let data = {};
  try { data = await response.json(); } catch (_) {}
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

async function copyText(value, label) {
  try {
    await navigator.clipboard.writeText(value);
    notify(`${label} скопировано`);
  } catch (_) {
    notify('Не удалось скопировать (разрешите доступ к буферу)', 'error');
  }
}

/* ------------------------------------------------------------------ theme */
function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem(THEME_KEY, theme);
}
applyTheme(localStorage.getItem(THEME_KEY) || 'dark');
$('themeToggle').addEventListener('click', () => {
  const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  applyTheme(next);
});

/* ----------------------------------------------------------------- tabs */
document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((item) => item.classList.toggle('active', item === tab));
    document.querySelectorAll('.panel').forEach((panel) => {
      panel.classList.toggle('current', panel.dataset.panel === tab.dataset.tab);
    });
  });
});

/* --------------------------------------------------------------- token */
function acceptBootstrap(data) {
  if (!data || typeof data.token !== 'string' || data.token.length < 32) return false;
  tokenValue = data.token;
  localStorage.setItem(TOKEN_KEY, tokenValue);
  return true;
}

async function bootstrapLocalSession() {
  // The only safe token delivery: loopback-only listener on the target PC.
  try {
    const response = await fetch('http://127.0.0.1:8767/bootstrap', { cache: 'no-store' });
    if (response.ok && acceptBootstrap(await response.json())) return true;
  } catch (_) {}

  // Legacy route is closed by default; kept only for the explicit preview flag.
  try {
    const response = await fetch('/api/local-bootstrap', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}', cache: 'no-store'
    });
    if (response.ok && acceptBootstrap(await response.json())) return true;
  } catch (_) {}
  return false;
}

function paintToken() {
  $('tokenInput').value = tokenValue;
  $('tokenMasked').textContent = tokenRevealed && tokenValue
    ? tokenValue
    : '•'.repeat(Math.min(12, tokenValue.length || 8));
  $('revealToken').textContent = tokenRevealed ? 'скрыть' : 'показать';
}

$('revealToken').addEventListener('click', () => { tokenRevealed = !tokenRevealed; paintToken(); });
$('copyToken').addEventListener('click', () => copyText(tokenValue, 'Токен'));

$('switchTunnelBtn').addEventListener('click', async () => {
  const mode = $('tunnelSelect').value;
  const label = mode === 'tailscale' ? 'Tailscale Funnel' : mode === 'cloudflared' ? 'Cloudflare Quick Tunnel' : 'Локальный режим';
  notify(`Переключение туннеля на ${label}…`);
  try {
    const res = await request('/api/tunnel/config', 'POST', { mode });
    if (res.url) {
      $('tunnelUrl').textContent = res.url;
      notify(`Туннель ${res.mode} активен: ${res.url}`);
    } else {
      $('tunnelUrl').textContent = 'нет';
      notify('Туннель отключён (локальный режим)');
    }
    await refreshAll();
  } catch (error) {
    notify(`Ошибка переключения туннеля: ${error.message}`, 'error');
  }
});

$('copyUrl').addEventListener('click', () => copyText($('tunnelUrl').textContent === 'нет' ? '' : $('tunnelUrl').textContent, 'Адрес'));

$('connectBtn').addEventListener('click', async () => {
  const value = $('tokenInput').value.trim();
  if (!value) { notify('Введите токен', 'error'); return; }
  tokenValue = value;
  localStorage.setItem(TOKEN_KEY, value);
  await refreshAll();
  notify('Подключение обновлено');
});

$('forgetBtn').addEventListener('click', async () => {
  tokenRevealed = false;
  tokenValue = '';
  localStorage.removeItem(TOKEN_KEY);
  paintToken();
  try { await request('/api/full-access/stop', 'POST', {}); } catch (_) {}
  try { await localAdmin('/yolo/config', 'POST', { enabled: false }); } catch (_) {}
  notify('Сеанс отключён');
  await refreshAll();
});

/* ----------------------------------------------------------- status/state */
function applyPlatform(info) {
  if (!info) return;
  platform = info;
  $('osBadge').textContent = `${info.label || info.os} · ${(info.shells || []).map((s) => s.label).join(' / ') || 'нет shell'}`;
  $('workspaceRoot').textContent = info.workspace_root || '—';
  $('workspaceRoot').title = info.workspace_root || '';
  const select = $('shellSelect');
  const previous = select.value;
  select.replaceChildren();
  (info.shells || []).forEach((shell) => {
    const option = document.createElement('option');
    option.value = shell.id;
    option.textContent = shell.label;
    select.appendChild(option);
  });
  if (info.default_shell) {
    select.value = (info.shells || []).some((s) => s.id === previous) ? previous : info.default_shell;
  }
  fillQuickCommands(info.os);
}

function applyFullAccess(state) {
  fullArmed = Boolean(state.armed);
  const pill = $('faState');
  pill.textContent = fullArmed ? 'АКТИВЕН' : 'ВЫКЛЮЧЕН';
  pill.classList.toggle('on', fullArmed);
  pill.classList.toggle('off', !fullArmed);
  $('faButton').textContent = fullArmed ? 'ПЕРЕЗАПУСТИТЬ' : 'ВКЛЮЧИТЬ';
  $('accessCard').classList.toggle('armed', fullArmed);
  if (fullArmed) {
    const remain = Math.max(0, Number(state.seconds_remaining || 0));
    $('faTimer').textContent = `До отключения ~${Math.ceil(remain / 60)} мин · idle ${Math.ceil(Number(state.idle_seconds_remaining || 0) / 60)} мин`;
    if (state.duration_seconds) $('faMinutes').value = Math.round(state.duration_seconds / 60);
  } else {
    $('faTimer').textContent = 'Таймер не активен';
  }
  updateRunControls();
  updateFileControls();
}

function updateRunControls() {
  $('runBtn').disabled = !fullArmed || jobRunning || !$('commandInput').value.trim();
  $('stopBtn').disabled = !jobRunning && !fullArmed;
  $('cmdStatus').textContent = jobRunning
    ? 'Команда выполняется…'
    : fullArmed ? 'Полный доступ активен — подтверждения не требуются' : 'Включите полный доступ, чтобы выполнять команды';
}

async function refreshAll() {
  try {
    const quick = await request('/api/quick');
    applyPlatform(quick.platform);
    capabilities = quick.capabilities || {};
    $('tunnelMode').textContent = quick.tunnel.mode || 'none';
    if ($('tunnelSelect') && document.activeElement !== $('tunnelSelect')) $('tunnelSelect').value = quick.tunnel.mode || 'none';
    $('tunnelUrl').textContent = quick.tunnel.url || 'нет';
    $('capabilityLine').textContent =
      `tailscale: ${capabilities.tailscale ? 'да' : 'нет'} · cloudflared: ${capabilities.cloudflared ? 'да' : 'нет'} · bwrap: ${capabilities.bubblewrap ? 'да' : 'нет'}`;
    $('connPill').classList.add('online');
    $('connPill').classList.remove('offline');
    $('connText').textContent = 'Подключено';
    applyFullAccess(quick.full_access);
    await loadQueue();
    return true;
  } catch (error) {
    $('connPill').classList.add('offline');
    $('connPill').classList.remove('online');
    $('connText').textContent = error.message === 'unauthorized' ? 'Токен не подошёл' : 'Нет связи';
    $('heroHint').textContent = error.message;
    return false;
  }
}

/* --------------------------------------------------------- full access */
$('faButton').addEventListener('click', async () => {
  const phrase = $('faPhrase').value;
  const minutes = Number($('faMinutes').value);
  if (!Number.isInteger(minutes) || minutes < 1 || minutes > 1440) {
    notify('Длительность должна быть от 1 до 1440 минут', 'error');
    return;
  }
  if (phrase !== PHRASE) {
    notify('Кодовая фраза не совпала', 'error');
    return;
  }
  try {
    const state = await request('/api/full-access/config', 'POST', {
      armed: true, duration_seconds: minutes * 60, confirmation: phrase
    });
    applyFullAccess(state);
    $('faPhrase').value = '';
    notify('Полный доступ включён');
  } catch (error) { notify(error.message, 'error'); }
});

$('faStop').addEventListener('click', async () => {
  try {
    applyFullAccess(await request('/api/full-access/stop', 'POST', {}));
    jobRunning = false;
    updateRunControls();
    notify('Полный доступ выключен, запущенные процессы остановлены');
  } catch (error) { notify(error.message, 'error'); }
});

/* ------------------------------------------------------------- terminal */
function fillQuickCommands(os) {
  const commands = os === 'windows'
    ? ['Get-Location', 'Get-ChildItem', 'git status', 'tailscale status', 'systeminfo', 'ipconfig /all']
    : os === 'android'
    ? ['pwd', 'ls -la', 'termux-battery-status', 'getprop ro.build.version.release', 'uname -a', 'df -h']
    : ['pwd', 'ls -la', 'git status', 'tailscale status', 'uname -a', 'df -h'];
  const holder = $('quickCommands');
  holder.replaceChildren();
  commands.forEach((command) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = command;
    button.addEventListener('click', () => {
      $('commandInput').value = command;
      updateRunControls();
      $('commandInput').focus();
    });
    holder.appendChild(button);
  });
}

$('commandInput').addEventListener('input', updateRunControls);
$('commandInput').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !$('runBtn').disabled) { event.preventDefault(); $('runBtn').click(); }
});

function appendOutput(text) {
  const node = $('output');
  if (node.textContent.startsWith('//')) node.textContent = '';
  node.textContent += text + '\n';
  node.scrollTop = node.scrollHeight;
}

async function waitForJob(id, target) {
  let lastStatus = '';
  while (true) {
    const job = await request('/api/full-access/jobs/' + encodeURIComponent(id));
    if (job.status !== lastStatus) {
      lastStatus = job.status;
      target.textContent = `Статус: ${job.status}` + (job.cwd ? ` · cwd: ${job.cwd}` : '');
    }
    if (!['queued', 'running'].includes(job.status)) {
      appendOutput(`[exit ${job.return_code ?? '-'}] ${job.status}`);
      if (job.output) appendOutput(job.output);
      return job;
    }
    await new Promise((resolve) => setTimeout(resolve, 700));
  }
}

$('runBtn').addEventListener('click', async () => {
  const command = $('commandInput').value.trim();
  const timeout = Number($('timeoutInput').value);
  const cwd = $('cwdInput').value.trim();
  if (!command || !fullArmed) return;
  if (!Number.isInteger(timeout) || timeout < 1 || timeout > 3600) {
    notify('Таймаут должен быть от 1 до 3600 секунд', 'error');
    return;
  }
  jobRunning = true;
  updateRunControls();
  appendOutput(`$ ${command}${cwd ? '   (cwd: ' + cwd + ')' : ''}`);
  try {
    const body = { command, timeout_seconds: timeout, shell: $('shellSelect').value };
    if (cwd) body.cwd = cwd;
    const job = await request('/api/full-access/run', 'POST', body);
    await waitForJob(job.id, $('cmdStatus'));
    notify('Команда завершена');
  } catch (error) {
    appendOutput('[error] ' + error.message);
    notify(error.message, 'error');
  } finally {
    jobRunning = false;
    updateRunControls();
    refreshAll();
  }
});

$('stopBtn').addEventListener('click', async () => {
  try {
    await request('/api/full-access/stop', 'POST', {});
    jobRunning = false;
    updateRunControls();
    appendOutput('[stop] полный доступ выключен, дерево процессов остановлено');
    await refreshAll();
  } catch (error) { notify(error.message, 'error'); }
});

/* ---------------------------------------------------------------- files */
function filesUnlocked() { return fullArmed || $('yoloToggle').checked; }

function updateFileControls() {
  const unlocked = filesUnlocked();
  ['fileSearch', 'fileUp', 'fileRefresh', 'filePathInput', 'fileOpen', 'sandboxCommand', 'sandboxRun']
    .forEach((id) => { $(id).disabled = !unlocked; });
  $('editorContent').disabled = !unlocked || !openedFile;
  $('editorSave').disabled = !unlocked || !openedFile || !editorDirty;
  if (!unlocked) $('fileList').innerHTML = '<div class="empty-note">Включите полный доступ или песочницу YOLO</div>';
}

$('yoloToggle').addEventListener('change', async () => {
  const enabled = $('yoloToggle').checked;
  try {
    const state = await localAdmin('/yolo/config', 'POST', { enabled });
    $('yoloToggle').checked = state.enabled;
    updateFileControls();
    if (state.enabled) loadFiles();
  } catch (error) {
    $('yoloToggle').checked = !enabled;
    notify(error.message, 'error');
  }
});

async function loadFiles(path = currentFolder) {
  if (!filesUnlocked()) return;
  currentFolder = path || '.';
  $('filePath').textContent = currentFolder;
  $('filePathInput').value = currentFolder;
  try {
    const data = await request('/api/workspace/list', 'POST', { path: currentFolder });
    currentEntries = data.result.entries || [];
    paintFiles();
  } catch (error) { notify(error.message, 'error'); }
}

function paintFiles() {
  const holder = $('fileList');
  holder.replaceChildren();
  const filter = $('fileSearch').value.trim().toLowerCase();
  const visible = currentEntries.filter((item) => item.name.toLowerCase().includes(filter));
  if (!visible.length) {
    const empty = document.createElement('div');
    empty.className = 'empty-note';
    empty.textContent = 'Пусто или ничего не найдено';
    holder.appendChild(empty);
    return;
  }
  visible.forEach((entry) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'file-entry';
    const glyph = document.createElement('span');
    glyph.className = 'glyph';
    glyph.textContent = entry.type === 'directory' ? '▰' : entry.type === 'file' ? '▤' : '⊘';
    const name = document.createElement('span');
    name.textContent = entry.name;
    const meta = document.createElement('span');
    meta.className = 'meta';
    meta.textContent = entry.type === 'directory' ? '' : (entry.size ?? '') + ' Б';
    button.append(glyph, name, meta);
    button.disabled = !['file', 'directory'].includes(entry.type);
    button.addEventListener('click', () => {
      const child = currentFolder === '.' ? entry.name : `${currentFolder}/${entry.name}`;
      if (entry.type === 'directory') loadFiles(child);
      else openFile(child);
    });
    holder.appendChild(button);
  });
}

async function openFile(path) {
  if (!filesUnlocked()) return;
  if (editorDirty && !window.confirm('Отбросить несохранённые изменения?')) return;
  try {
    const data = await request('/api/workspace/read', 'POST', { path });
    openedFile = path;
    editorDirty = false;
    $('editorName').textContent = path;
    $('editorStatus').textContent = `${data.result.bytes} байт`;
    $('editorContent').value = data.result.content;
    updateFileControls();
    paintFiles();
  } catch (error) { notify(error.message, 'error'); }
}

$('fileSearch').addEventListener('input', paintFiles);
$('fileUp').addEventListener('click', () => loadFiles(currentFolder.includes('/') ? currentFolder.split('/').slice(0, -1).join('/') || '.' : '.'));
$('fileRefresh').addEventListener('click', () => loadFiles());
$('fileOpen').addEventListener('click', () => openFile($('filePathInput').value.trim()));
$('editorContent').addEventListener('input', () => {
  editorDirty = true;
  $('editorStatus').textContent = 'Есть несохранённые изменения';
  updateFileControls();
});

$('editorSave').addEventListener('click', async () => {
  if (!openedFile) return;
  try {
    const data = await request('/api/workspace/write', 'POST', { path: openedFile, content: $('editorContent').value });
    editorDirty = false;
    $('editorStatus').textContent = data.result.backup ? `Бэкап: ${data.result.backup}` : 'Сохранено';
    updateFileControls();
    notify('Файл сохранён');
  } catch (error) { notify(error.message, 'error'); }
});

$('sandboxRun').addEventListener('click', async () => {
  const command = $('sandboxCommand').value.trim();
  if (!command) return;
  $('sandboxOutput').textContent = 'Задача отправлена…';
  try {
    const job = await request('/api/workspace/run', 'POST', { command, timeout_seconds: 120 });
    while (true) {
      const status = await request('/api/workspace/jobs/' + job.id);
      if (!['queued', 'running'].includes(status.status)) {
        $('sandboxOutput').textContent = `${status.status} · код ${status.return_code ?? '-'}\n${status.output || ''}`;
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 900));
    }
  } catch (error) { $('sandboxOutput').textContent = error.message; }
});

/* ---------------------------------------------------------------- queue */
async function loadQueue() {
  try {
    const state = await request('/api/state');
    const records = state.commands || [];
    const pending = records.filter((item) => item.status === 'pending' || item.status === 'executing');
    $('queueCount').textContent = String(pending.length);
    const holder = $('queueList');
    holder.replaceChildren();
    if (!pending.length) {
      const empty = document.createElement('div');
      empty.className = 'empty-note';
      empty.textContent = 'Очередь пуста';
      holder.appendChild(empty);
      return;
    }
    pending.forEach((item) => {
      const card = document.createElement('article');
      card.className = 'request-card ' + (item.status === 'pending' ? 'pending' : '');
      const created = item.created_at ? new Date(item.created_at * 1000).toLocaleString() : '';
      const body = item.command
        ? `<b>${escapeHtml(item.kind)} · ${escapeHtml(item.shell || '')}</b><pre>${escapeHtml(item.command)}</pre><small>cwd: ${escapeHtml(item.cwd || '')}</small>`
        : `<b>${escapeHtml(item.kind || 'system_action')}</b><pre>${escapeHtml(JSON.stringify(item, null, 2))}</pre>`;
      const actions = item.status === 'pending'
        ? `<div class="request-actions"><button class="primary-btn" data-id="${escapeHtml(item.id)}" data-do="approve" type="button">Разрешить</button><button class="quiet-btn" data-id="${escapeHtml(item.id)}" data-do="reject" type="button">Отклонить</button></div>`
        : '';
      card.innerHTML = `<div class="request-top"><b>${escapeHtml(item.status)}</b><span>${escapeHtml(item.kind || '')}</span><time>${created}</time></div>${body}${actions}`;
      holder.appendChild(card);
    });
  } catch (_) {}
}

$('queueList').addEventListener('click', async (event) => {
  const button = event.target.closest('button[data-do]');
  if (!button) return;
  try {
    await request(`/api/commands/${encodeURIComponent(button.dataset.id)}/${button.dataset.do}`, 'POST', {});
    await loadQueue();
  } catch (error) { notify(error.message, 'error'); }
});

/* ----------------------------------------------------------- HTTP journal */
let requestRows = [];

$('refreshRequests').addEventListener('click', loadRequests);
$('requestFilter').addEventListener('input', renderRequests);

async function loadRequests() {
  try {
    const data = await request('/api/requests?limit=300');
    requestRows = data.requests || [];
    renderRequests();
  } catch (error) { notify(error.message, 'error'); }
}

function renderRequests() {
  const holder = $('requestList');
  holder.replaceChildren();
  const filter = $('requestFilter').value.trim().toLowerCase();
  const visible = requestRows.filter((row) => {
    if (!filter) return true;
    return `${row.method} ${row.path} ${row.status} ${row.client}`.toLowerCase().includes(filter);
  });
  if (!visible.length) {
    holder.innerHTML = '<div class="empty-note">Запросов пока нет</div>';
    return;
  }
  visible.forEach((row) => {
    const node = document.createElement('div');
    node.className = 'event-row request';
    node.innerHTML = `<span>${new Date((row.time || 0) * 1000).toLocaleTimeString()}</span>`
      + `<span class="method">${escapeHtml(row.method)}</span>`
      + `<span class="path-cell" title="${escapeHtml(row.path)}">${escapeHtml(row.path)}</span>`
      + `<span class="status">${escapeHtml(row.status)}</span>`
      + `<span class="client-cell" title="${escapeHtml(row.client)}">${escapeHtml(row.client)}</span>`;
    holder.appendChild(node);
  });
}

/* ----------------------------------------------------------------- logs */
$('refreshEvents').addEventListener('click', loadEvents);
async function loadEvents() {
  try {
    const data = await request('/api/events');
    const holder = $('eventsList');
    holder.replaceChildren();
    (data.events || []).forEach((event) => {
      const row = document.createElement('div');
      row.className = 'event-row';
      row.innerHTML = `<span>${new Date((event.time || 0) * 1000).toLocaleString()}</span><b>${escapeHtml(event.action || event.source)}</b><span>${escapeHtml(event.path || event.job_id || event.shell || '')}</span>`;
      holder.appendChild(row);
    });
    if (!(data.events || []).length) holder.innerHTML = '<div class="empty-note">Событий пока нет</div>';
  } catch (error) { notify(error.message, 'error'); }
}

/* ----------------------------------------------------------------- start */
async function start() {
  paintToken();
  await bootstrapLocalSession();
  paintToken();
  await refreshAll();
  setInterval(refreshAll, 6000);
  setInterval(loadEvents, 12000);
  setInterval(loadRequests, 5000);
  loadFiles();
  loadEvents();
  loadRequests();
}
start();
