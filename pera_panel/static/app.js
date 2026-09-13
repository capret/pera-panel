'use strict';
const $ = selector => document.querySelector(selector);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let state = null, selected = null, currentTab = 'overview', mods = [], confirmCallback = null, polling = false;
let importTarget = null, uploading = false;
const seenJobs = new Set();
const csrf = $('meta[name="csrf-token"]').content;

async function api(path, method = 'GET', data) {
  const response = await fetch('/api' + path, {method, headers: {'Content-Type':'application/json', 'X-CSRF-Token':csrf}, ...(data !== undefined ? {body:JSON.stringify(data)} : {})});
  const result = await response.json();
  if (response.status === 401) { location.assign('/'); throw new Error('Please sign in.'); }
  if (!response.ok) throw new Error(result.error || 'The request failed.');
  return result;
}
function toast(message, error = false) {
  $('#toast').textContent = message;
  $('#toast').classList.toggle('toast-error', error);
  $('#toast').hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { $('#toast').hidden = true; }, error ? 12000 : 6000);
}
function world() { return state?.worlds.find(item => item.id === selected); }
function worldURL() { if (!selected) throw new Error('Select a world first.'); return '/worlds/' + selected; }
function duration(seconds) { return seconds < 60 ? seconds + 's' : seconds < 3600 ? Math.floor(seconds / 60) + 'm' : Math.floor(seconds / 3600) + 'h ' + Math.floor(seconds % 3600 / 60) + 'm'; }
function date(value) { return new Date(value).toLocaleString(undefined, {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'}); }
async function queued(path, method = 'POST', data = {}) {
  const result = await api(path, method, data);
  toast(result.job.title + ' started.');
  await refresh();
  return result;
}
function confirmAction(title, description, label, value, callback) {
  $('#confirm-title').textContent = title;
  $('#confirm-description').textContent = description;
  $('#confirm-input-label').firstChild.textContent = label;
  $('#confirm-input').value = value;
  $('#confirm-input-label').hidden = !label;
  $('#confirm-input').required = Boolean(label);
  confirmCallback = callback;
  $('#confirm-dialog').showModal();
}
function setTab(tab) {
  currentTab = tab;
  document.querySelectorAll('[data-tab]').forEach(button => button.classList.toggle('active', button.dataset.tab === tab));
  document.querySelectorAll('.tab-panel').forEach(panel => { panel.hidden = panel.id !== 'tab-' + tab; });
  if (tab === 'backups') loadBackups().catch(error => toast(error.message, true));
  if (tab === 'logs') loadLogs().catch(error => toast(error.message, true));
}
function selectWorld(id) {
  selected = id;
  localStorage.setItem('pera-world', id);
  render();
  fillSettings();
  mods = structuredClone(world()?.mods || []);
  renderMods();
  setTab(currentTab);
}
function render() {
  if (!state) return;
  const item = world();
  $('#install-notice').hidden = state.game_installed;
  $('#empty-state').hidden = state.worlds.length > 0;
  $('#world-content').hidden = !item;
  $('#world-actions').hidden = !item;
  $('#world-list').innerHTML = state.worlds.map(w => `<button class="world-item ${w.id === selected ? 'selected' : ''}" data-world="${w.id}"><span class="world-glyph">◈</span><span>${escapeHTML(w.name)}<small>${w.caves ? 'Surface + caves' : 'Surface only'}</small></span><i class="world-dot ${w.runtime.state === 'running' ? 'online' : ''}"></i></button>`).join('');
  $('#world-name').textContent = item?.name || 'World overview';
  $('#world-description').textContent = item?.description || 'Build a camp. Bring your friends. Keep the fire going.';
  $('#breadcrumb-world').textContent = item?.name || 'Overview';
  const runtime = item?.runtime;
  $('#metric-status').textContent = runtime ? ({running:'Running',stopped:'Stopped',degraded:'Partial',failed:'Exited'}[runtime.state]) : 'No world yet';
  $('#metric-status').classList.toggle('green', runtime?.state === 'running');
  $('#metric-status-note').textContent = runtime?.uptime_seconds ? `Up ${duration(runtime.uptime_seconds)} · ${runtime.memory_mb} MB game memory` : item ? 'Ready when you are' : 'Create a world to begin';
  $('#metric-cpu').textContent = state.host.cpu_percent + '%';
  $('#metric-memory').textContent = state.host.memory_used_gb + ' GB';
  $('#metric-memory-note').textContent = `of ${state.host.memory_total_gb} GB · ${state.host.memory_percent}% used`;
  $('#metric-disk').textContent = state.host.disk_free_gb + ' GB';
  const busy = state.jobs.find(job => job.id === state.busy);
  $('#open-import').disabled = Boolean(busy) || uploading;
  $('#busy-banner').hidden = !busy;
  $('#busy-banner').textContent = busy ? busy.title + '… You can follow progress in the logs. Other changes are paused until this finishes.' : '';
  document.querySelectorAll('[data-action]').forEach(button => {
    const action = button.dataset.action;
    const active = runtime && ['running','degraded'].includes(runtime.state);
    button.disabled = Boolean(busy) || !item || (['save','announce','stop'].includes(action) && !active) || (action === 'start' && active);
  });
  $('#activity-list').innerHTML = state.jobs.slice(0, 5).map(job => `<div class="activity"><span class="activity-icon ${job.state === 'failed' ? 'failed' : ''}">${job.state === 'completed' ? '✓' : job.state === 'failed' ? '!' : '↻'}</span><div><strong>${escapeHTML(job.title)}</strong><small>${escapeHTML(job.error || job.state)} · ${date(job.created_at)}</small></div></div>`).join('') || '<p class="muted quiet-empty">Your world’s story will show up here.</p>';
  if (!item) return;
  $('#mod-count').textContent = item.mods.filter(mod => mod.enabled).length;
  $('#max-players').textContent = item.max_players + ' survivors';
  $('#game-mode').textContent = item.game_mode;
  $('#autostart-value').textContent = item.autostart ? 'Yes' : 'No';
  $('#shard-list').innerHTML = ['Master', ...(item.caves ? ['Caves'] : [])].map(name => {
    const shard = runtime.shards.find(s => s.name === name);
    return `<article class="shard"><div class="shard-art ${name === 'Caves' ? 'cave-art' : ''}">${name === 'Master' ? '♧' : '◇'}</div><div><h3>${name === 'Master' ? 'The surface' : 'The caves'}</h3><small>${name === 'Master' ? 'A world of possibility' : 'A little deeper into the unknown'}</small></div><span class="status-tag ${shard?.running ? 'running' : ''}">${shard?.running ? 'Process running' : shard ? 'Exited · ' + shard.exit_code : 'Stopped'}</span></article>`;
  }).join('');
}
async function refresh() {
  if (polling) return;
  polling = true;
  try {
    const first = !state;
    state = await api('/status');
    $('#connection-error').hidden = true;
    if (!world()) selected = state.worlds.find(w => w.id === localStorage.getItem('pera-world'))?.id || state.worlds[0]?.id || null;
    for (const job of state.jobs) {
      if (['failed','completed'].includes(job.state) && !seenJobs.has(job.id)) {
        seenJobs.add(job.id);
        if (!first) {
          toast(job.error || job.result?.message || job.title + ' completed.', job.state === 'failed');
          if (job.result?.world_id) selected = job.result.world_id;
          if (['Create world','Roll back world','Import local save'].includes(job.title)) { fillSettings(); mods = structuredClone(world()?.mods || []); renderMods(); }
          if (currentTab === 'backups') await loadBackups();
        }
      }
    }
    render();
    if (first) { fillSettings(); mods = structuredClone(world()?.mods || []); renderMods(); }
    if (currentTab === 'logs' && selected) await loadLogs();
  } catch (error) {
    $('#connection-error').textContent = 'Connection interrupted: ' + error.message;
    $('#connection-error').hidden = false;
  } finally { polling = false; }
}
function fillSettings() {
  const w = world();
  if (!w) return;
  const input = (key, title, type = 'text') => `<label>${title}<input name="${key}" type="${type}" value="${escapeHTML(w[key])}" ${key === 'name' ? 'required maxlength="80"' : ''}></label>`;
  const check = (key, title) => `<label class="checkbox"><input type="checkbox" name="${key}" ${w[key] ? 'checked' : ''}>${title}</label>`;
  const json = (key, title) => `<label>${title}<textarea class="code-input" name="${key}" rows="5">${escapeHTML(JSON.stringify(w[key], null, 2))}</textarea></label>`;
  const ids = (key, title) => `<label>${title}<textarea name="${key}" rows="3" placeholder="KU_abcdefgh, one per line">${escapeHTML(w[key].join('\n'))}</textarea></label>`;
  $('#settings-form').innerHTML = `<div class="form-grid">${input('name','World name')}${input('description','Description')}${input('password','Join password','password')}<label>Klei cluster token<input name="token" type="password" autocomplete="off" placeholder="${w.has_token ? 'Token saved · leave blank to keep' : 'Paste your Klei token'}"></label><label>Game mode<select name="game_mode">${['survival','endless','wilderness'].map(mode => `<option ${mode === w.game_mode ? 'selected' : ''}>${mode}</option>`).join('')}</select></label><label>Player slots<input type="number" min="1" max="64" name="max_players" value="${w.max_players}"></label></div><div class="checks">${check('caves','Include caves')}${check('pause_when_empty','Pause when empty')}${check('pvp','Allow PvP')}${check('autostart','Start this world at boot')}</div><details><summary>World generation & in-game snapshots</summary><p class="field-help">Overrides are JSON objects, for example {"season_start":"autumn","world_size":"default"}. Existing terrain will not regenerate. For a fresh layout, create a new world.</p><div class="form-grid">${json('master_overrides','Surface overrides')}${json('caves_overrides','Caves overrides')}<label>In-game save snapshots<input type="number" name="snapshots" min="1" max="50" value="${w.snapshots}"></label></div></details><details><summary>Player permissions</summary><p class="field-help">Use Klei user IDs, one per line. Admins can use the in-game console. Whitelisted players can bypass the join password; this is not an exclusive allowlist.</p><div class="form-grid">${ids('admins','Administrators (OP)')}${ids('banned','Banned players')}${ids('whitelist','Whitelisted players')}</div></details><div class="form-actions"><button type="submit" class="button primary">Save settings</button><span class="muted">Changes take effect on the next start.</span></div>`;
}
function collectSettings(form) {
  const values = Object.fromEntries(new FormData(form));
  for (const key of ['max_players','snapshots']) if (key in values) values[key] = Number(values[key]);
  for (const key of ['caves','pause_when_empty','pvp','autostart']) if (form.elements.namedItem(key)) values[key] = form.elements.namedItem(key).checked;
  for (const key of ['master_overrides','caves_overrides']) if (key in values) values[key] = JSON.parse(values[key] || '{}');
  for (const key of ['admins','banned','whitelist']) if (key in values) values[key] = values[key].split(/\s+/).filter(Boolean);
  return values;
}
function captureMods() {
  document.querySelectorAll('.mod-row').forEach(row => {
    const index = Number(row.dataset.index);
    mods[index].enabled = row.querySelector('input[type="checkbox"]').checked;
    try { mods[index].options = JSON.parse(row.querySelector('textarea').value || '{}'); }
    catch { throw new Error('Mod ' + mods[index].id + ': options must be valid JSON.'); }
  });
}
function renderMods() {
  $('#mods-list').innerHTML = mods.map((mod, index) => `<article class="mod-row" data-index="${index}"><div class="mod-heading"><label class="checkbox"><input type="checkbox" ${mod.enabled ? 'checked' : ''}><span>Workshop ${escapeHTML(mod.id)}</span></label><a href="https://steamcommunity.com/sharedfiles/filedetails/?id=${mod.id}" target="_blank" rel="noreferrer">View ↗</a><button class="icon-button remove-mod" data-index="${index}" aria-label="Remove mod ${mod.id}">×</button></div><label>Configuration options (JSON)<textarea class="code-input" rows="3">${escapeHTML(JSON.stringify(mod.options,null,2))}</textarea></label></article>`).join('') || '<div class="quiet-empty">A world in its original form. Add your first mod below.</div>';
}
async function loadBackups() {
  if (!selected) return;
  const id = selected;
  const {backups} = await api('/worlds/' + id + '/backups');
  if (id !== selected) return;
  $('#backup-list').innerHTML = backups.length ? `<div class="table-wrap"><table><thead><tr><th>BACKUP</th><th>CREATED</th><th>SIZE</th><th></th></tr></thead><tbody>${backups.map(backup => `<tr><td><strong>${escapeHTML(backup.label)}</strong><small>${backup.id}</small></td><td>${date(backup.created_at)}</td><td>${backup.size_mb} MB</td><td class="table-actions"><button class="button restore-backup" data-snapshot="${backup.id}">Roll back</button><button class="icon-button delete-backup" data-snapshot="${backup.id}" aria-label="Delete backup">×</button></td></tr>`).join('')}</tbody></table></div>` : '<div class="quiet-empty">No backups yet. Save a moment you can come back to.</div>';
}
async function loadLogs() {
  if (!selected) return;
  const id = selected, shard = $('#log-shard').value;
  const data = await api('/worlds/' + id + '/logs/' + shard);
  if (selected !== id || $('#log-shard').value !== shard) return;
  const output = $('#log-output');
  const atBottom = output.scrollTop + output.clientHeight >= output.scrollHeight - 60;
  output.textContent = data.log;
  if (atBottom) output.scrollTop = output.scrollHeight;
}
document.addEventListener('click', async event => {
  const target = event.target.closest('button');
  if (!target) return;
  try {
    if (target.matches('.close-dialog')) { target.closest('dialog').close(); return; }
    if (target.matches('[data-world]')) selectWorld(target.dataset.world);
    if (target.matches('[data-tab]')) setTab(target.dataset.tab);
    if (['sidebar-create','add-world','empty-create'].includes(target.id)) $('#create-dialog').showModal();
    if (target.id === 'open-import') {
      importTarget = {id: world().id, name: world().name};
      $('#import-form').reset();
      $('#import-description').textContent = 'Import a local save into “' + importTarget.name + '”. The imported world will stay stopped.';
      $('#import-progress').hidden = true;
      $('#import-status').textContent = '';
      $('#import-dialog').showModal();
    }
    if (target.id === 'logout') { await api('/logout','POST',{}); location.assign('/'); }
    if (target.dataset.action) {
      const action = target.dataset.action, url = worldURL();
      if (action === 'backup') confirmAction('Save this moment', 'A running world will save, stop briefly, and restart after the backup.', 'Backup name', 'Manual backup', label => queued(url + '/actions/backup','POST',{label}));
      else if (['stop','restart'].includes(action)) confirmAction(action === 'stop' ? 'Stop this world?' : 'Restart this world?', 'Both shards will save and connected players will disconnect.', '', '', () => queued(url + '/actions/' + action));
      else await queued(url + '/actions/' + action);
    }
    if (target.id === 'announce-button') { const url = worldURL(); confirmAction('A word for your survivors', 'Send an in-game announcement to the world.', 'Message', '', message => queued(url + '/actions/announce','POST',{message})); }
    if (target.id === 'delete-world') { const url = worldURL(); confirmAction('Archive this world?', 'Stop the world first. Type “' + world().name + '” to confirm. World files and a backup are kept on disk.', 'World name', '', confirmation => queued(url,'DELETE',{confirmation})); }
    if (target.matches('.restore-backup')) { const url = worldURL(); confirmAction('Roll back this world?', 'This replaces world progress and disconnects players. A safety backup is made first. Type “' + world().name + '” to continue.', 'World name', '', confirmation => queued(url + '/backups/' + target.dataset.snapshot + '/restore','POST',{confirmation})); }
    if (target.matches('.delete-backup')) { const url = worldURL(); confirmAction('Delete this backup?', 'This backup will be permanently removed.', '', '', () => queued(url + '/backups/' + target.dataset.snapshot,'DELETE',{})); }
    if (target.id === 'add-mod') {
      captureMods();
      const id = $('#new-mod-id').value.trim();
      if (!/^[1-9][0-9]{4,19}$/.test(id) || mods.some(mod => mod.id === id)) throw new Error('Enter a unique numeric Workshop ID (5–20 digits).');
      mods.push({id,enabled:true,options:{}}); $('#new-mod-id').value = ''; renderMods();
    }
    if (target.matches('.remove-mod')) { captureMods(); mods.splice(Number(target.dataset.index),1); renderMods(); }
    if (target.id === 'save-mods') { captureMods(); await queued(worldURL(),'PATCH',{mods}); }
  } catch (error) { toast(error.message, true); }
});
$('#create-form').addEventListener('submit', async event => {
  event.preventDefault();
  try { await queued('/worlds','POST',collectSettings(event.target)); $('#create-dialog').close(); event.target.reset(); }
  catch (error) { toast(error.message,true); }
});
$('#settings-form').addEventListener('submit', async event => {
  event.preventDefault();
  try { await queued(worldURL(),'PATCH',collectSettings(event.target)); }
  catch (error) { toast(error.message,true); }
});
$('#confirm-form').addEventListener('submit', async event => {
  event.preventDefault();
  $('#confirm-submit').disabled = true;
  try { await confirmCallback($('#confirm-input').value); $('#confirm-dialog').close(); }
  catch (error) { toast(error.message,true); }
  finally { $('#confirm-submit').disabled = false; }
});
$('#log-shard').addEventListener('change', () => loadLogs().catch(error => toast(error.message,true)));
$('#import-dialog').addEventListener('cancel', event => { if (uploading) event.preventDefault(); });
$('#import-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (uploading || !importTarget) return;
  const file = $('#save-zip').files[0];
  if (!file || !/\.zip$/i.test(file.name)) { toast('Choose a ZIP file.', true); return; }
  if (file.size > 256 * 1024 * 1024) { toast('The ZIP must be 256 MiB or smaller.', true); return; }
  if ($('#import-confirmation').value !== importTarget.name) { toast('Type the world name exactly to confirm.', true); return; }
  const form = new FormData(event.target), id = importTarget.id;
  form.set('inherit_mods', $('#inherit-mods').checked ? 'true' : 'false');
  uploading = true;
  $('#import-progress').hidden = false;
  $('#import-progress').value = 0;
  $('#import-status').textContent = 'Uploading save…';
  document.querySelectorAll('#import-dialog button').forEach(button => { button.disabled = true; });
  try {
    // FormData supplies its own multipart boundary; XHR provides actual upload progress.
    const result = await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/worlds/' + id + '/import-save');
      xhr.setRequestHeader('X-CSRF-Token', csrf);
      xhr.upload.onprogress = progress => {
        if (progress.lengthComputable) {
          const percent = Math.round(progress.loaded / progress.total * 100);
          $('#import-progress').value = percent;
          $('#import-status').textContent = percent === 100 ? 'Upload received. Preparing import…' : 'Uploading save… ' + percent + '%';
        }
      };
      xhr.onerror = () => reject(new Error('Upload connection failed. Check Recent activity before retrying.'));
      xhr.onload = () => {
        let data;
        try { data = JSON.parse(xhr.responseText); }
        catch { reject(new Error(xhr.status === 413 ? 'Upload rejected as too large. Check your reverse proxy upload limit.' : 'Unexpected server response. Check Recent activity before retrying.')); return; }
        if (xhr.status === 401) { location.assign('/'); reject(new Error('Please sign in again.')); return; }
        if (xhr.status < 200 || xhr.status >= 300) reject(new Error(data.error || 'Upload failed.'));
        else resolve(data);
      };
      xhr.send(form);
    });
    $('#import-dialog').close();
    toast(result.job.title + ' started. Validation and replacement appear in Recent activity.');
    await refresh();
  } catch (error) { $('#import-status').textContent = error.message; toast(error.message, true); }
  finally {
    uploading = false;
    document.querySelectorAll('#import-dialog button').forEach(button => { button.disabled = false; });
  }
});
refresh();
setInterval(refresh, 4000);
