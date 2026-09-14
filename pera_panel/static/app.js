'use strict';
const $ = selector => document.querySelector(selector);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const {t, descriptor: msg} = I18n;
const h = (key, params = {}) => `<span data-i18n-message="${escapeHTML(JSON.stringify(msg(key, params)))}">${escapeHTML(t(key, params))}</span>`;
let state = null, selected = null, currentTab = 'overview', mods = [], confirmCallback = null, polling = false;
let importTarget = null, uploading = false, currentPage = 'server';
let archivedWorlds = [], archivesLoading = false;
let playerWorld = null, playerRows = [], characterRows = [], characterWorld = null, playerPolling = false, playerPollAt = 0;
const seenJobs = new Set();
const csrf = $('meta[name="csrf-token"]').content;

async function api(path, method = 'GET', data) {
  const response = await fetch('/api' + path, {method, headers: {'Content-Type':'application/json', 'X-CSRF-Token':csrf}, ...(data !== undefined ? {body:JSON.stringify(data)} : {})});
  const result = await response.json();
  if (response.status === 401) { location.reload(); throw new I18n.Error('Please sign in.'); }
  if (!response.ok) throw new I18n.Error(I18n.message(result.error_i18n) || result.error || 'The request failed.');
  return result;
}
function toast(message, error = false) {
  I18n.text('#toast', message);
  $('#toast').classList.toggle('toast-error', error);
  $('#toast').hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { $('#toast').hidden = true; }, error ? 12000 : 6000);
}
function world() { return state?.worlds.find(item => item.id === selected); }
function worldURL() { if (!selected) throw new I18n.Error('Select a world first.'); return '/worlds/' + selected; }
function duration(seconds) { return seconds < 60 ? t('{seconds}s', {seconds}) : seconds < 3600 ? t('{minutes}m', {minutes: Math.floor(seconds / 60)}) : t('{hours}h {minutes}m', {hours: Math.floor(seconds / 3600), minutes: Math.floor(seconds % 3600 / 60)}); }
function date(value) { return new Date(value).toLocaleString(I18n.locale, {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'}); }
async function queued(path, method = 'POST', data = {}) {
  const result = await api(path, method, data);
  toast(msg('{job} started.', {job: msg(result.job.title)}));
  await refresh();
  return result;
}
function confirmAction(title, description, label, value, callback) {
  I18n.text('#confirm-title', title);
  I18n.text('#confirm-description', description);
  I18n.text('#confirm-label-text', label);
  $('#confirm-input').value = value;
  $('#confirm-input-label').hidden = !label;
  $('#confirm-input').required = Boolean(label);
  confirmCallback = callback;
  $('#confirm-dialog').showModal();
}
function routeFor(path) {
  const match = path.match(/^\/worlds\/([a-f0-9]{12})\/(overview|settings|mods|backups|logs)\/?$/);
  if (match) return {page:'world', id:match[1], tab:match[2], path:`/worlds/${match[1]}/${match[2]}`};
  return path === '/archives' ? {page:'archives',id:null,tab:'overview',path:'/archives'} : {page:'server',id:null,tab:'overview',path:'/overview'};
}
async function navigate(path, {replace = false} = {}) {
  const route = routeFor(path), previous = selected;
  if (location.pathname !== route.path) history[replace ? 'replaceState' : 'pushState']({}, '', route.path);
  currentPage = route.page; selected = route.id; currentTab = route.tab;
  if (state && selected && !world()) { await navigate('/overview', {replace:true}); toast('World not found.', true); return; }
  if (previous !== selected) {
    playerWorld = null; characterWorld = null; playerRows = []; characterRows = []; playerPollAt = 0;
    renderCharacters(); fillSettings(); mods = structuredClone(world()?.mods || []); renderMods();
  }
  render();
  if (!state) return;
  if (currentPage === 'archives' && !state.busy) await loadArchives();
  if (selected) {
    if (!state.busy && ['settings','backups'].includes(currentTab)) await loadCharacters();
    if (currentTab === 'backups') await loadBackups();
    if (currentTab === 'logs') await loadLogs();
    await loadPlayers();
  }
}
function setTab(tab) { return selected ? navigate(`/worlds/${selected}/${tab}`) : Promise.resolve(); }
function selectWorld(id) { return navigate(`/worlds/${id}/overview`); }
function renderActivity(selector, jobs, global = false) {
  $(selector).innerHTML = jobs.map(job => `<div class="activity"><span class="activity-icon ${job.state === 'failed' ? 'failed' : ''}">${job.state === 'completed' ? '✓' : job.state === 'failed' ? '!' : '↻'}</span><div><strong>${escapeHTML(t(job.title))}</strong>${global ? `<small>${escapeHTML(job.world_name || state.worlds.find(w => w.id === job.world_id)?.name || job.world_id || t('Panel / legacy activity'))}</small>` : ''}<small>${escapeHTML(t(I18n.message(job.error_i18n) || job.error || job.state))} · ${date(job.created_at)}</small></div></div>`).join('') || `<p class="muted quiet-empty">${h('No activity for this view yet.')}</p>`;
  I18n.bind($(selector));
}
function render() {
  if (!state) return;
  const item = world();
  renderArchives();
  $('#install-notice').hidden = state.game_installed;
  $('#empty-state').hidden = currentPage !== 'server' || state.worlds.length > 0;
  $('#server-overview').hidden = currentPage !== 'server';
  $('#server-metrics').hidden = currentPage !== 'server';
  $('#archives-page').hidden = currentPage !== 'archives';
  $('#world-state').hidden = !item;
  $('#world-content').hidden = !item;
  $('#world-actions').hidden = !item;
  $('#world-list').innerHTML = state.worlds.map(w => `<button class="world-item ${w.id === selected ? 'selected' : ''}" data-world="${w.id}"><span class="world-glyph">◈</span><span>${escapeHTML(w.name)}<small>${t(w.caves ? 'Surface + caves' : 'Surface only')}</small></span><i class="world-dot ${w.runtime.state === 'running' ? 'online' : ''}"></i></button>`).join('');
  $('#world-name').textContent = item?.name || t(currentPage === 'archives' ? 'Archived worlds' : 'Server overview');
  $('#world-description').textContent = item ? item.description || t('Build a camp. Bring your friends. Keep the fire going.') : t(currentPage === 'archives' ? 'Manage archived worlds and reclaim disk space.' : 'Host resources, all worlds, and panel activity.');
  document.title = (item ? item.name + ' · ' + t({overview:'World overview',settings:'World settings',mods:'Workshop mods',backups:'Backups & rollback',logs:'Live logs'}[currentTab]) : t(currentPage === 'archives' ? 'Archived worlds' : 'Server overview')) + ' · Pera Panel';
  $('#breadcrumb-world').textContent = item ? item.name + ' / ' + t({overview:'World overview',settings:'World settings',mods:'Workshop mods',backups:'Backups & rollback',logs:'Live logs'}[currentTab]) : t(currentPage === 'archives' ? 'Archived worlds' : 'Server overview');
  document.querySelectorAll('[data-tab]').forEach(link => { link.classList.toggle('active', link.dataset.tab === currentTab); link.setAttribute('aria-current', link.dataset.tab === currentTab ? 'page' : 'false'); link.href = selected ? `/worlds/${selected}/${link.dataset.tab}` : '/overview'; });
  document.querySelectorAll('.tab-panel').forEach(panel => { panel.hidden = panel.id !== 'tab-' + currentTab; });
  const runtime = item?.runtime;
  $('#native-rollback').disabled = Boolean(state.busy) || runtime?.state !== 'running';
  $('#rollback-count').max = item?.snapshots || 50;
  updateRecoveryButton();
  $('#refresh-characters').disabled = Boolean(state.busy);
  $('#world-state').textContent = runtime ? t({running:'Running',stopped:'Stopped',degraded:'Partial',failed:'Exited'}[runtime.state]) + (runtime.uptime_seconds ? ' · ' + duration(runtime.uptime_seconds) : '') : '';
  $('#metric-status').textContent = state.worlds.length;
  $('#metric-status-note').textContent = t('{count} running worlds', {count:state.worlds.filter(w => ['running','degraded'].includes(w.runtime.state)).length});
  $('#metric-cpu').textContent = state.host.cpu_percent + '%';
  $('#metric-memory').textContent = state.host.memory_used_gb + ' GB';
  $('#metric-memory-note').textContent = t('of {total} GB · {percent}% used', {total: state.host.memory_total_gb, percent: state.host.memory_percent});
  $('#metric-disk').textContent = state.host.disk_free_gb + ' GB';
  const busy = state.jobs.find(job => job.id === state.busy);
  $('#open-import').disabled = Boolean(busy) || uploading;
  $('#busy-banner').hidden = !busy;
  $('#busy-banner').textContent = busy ? t('{job}… You can follow progress in the logs. Other changes are paused until this finishes.', {job: msg(busy.title)}) : '';
  document.querySelectorAll('[data-action]').forEach(button => {
    const action = button.dataset.action;
    const active = runtime && ['running','degraded'].includes(runtime.state);
    button.disabled = Boolean(busy) || !item || (['save','announce','stop'].includes(action) && !active) || (action === 'start' && active);
  });
  renderActivity('#activity-list', state.jobs.filter(job => selected && job.world_id === selected).slice(0, 5));
  renderActivity('#server-activity-list', state.jobs.slice(0, 10), true);
  $('#server-worlds').innerHTML = state.worlds.map(w => `<button class="server-world-row" data-world="${w.id}"><span><strong>${escapeHTML(w.name)}</strong><small>${escapeHTML(t(w.caves ? 'Surface + caves' : 'Surface only'))}</small></span><span class="status-tag">${escapeHTML(t({running:'Running',stopped:'Stopped',degraded:'Partial',failed:'Exited'}[w.runtime.state]))}</span></button>`).join('') || `<p class="quiet-empty">${h('No world yet')}</p>`;
  if (!item) return;
  $('#mod-count').textContent = item.mods.filter(mod => mod.enabled).length;
  $('#max-players').textContent = t('{count} survivors', {count: item.max_players});
  $('#game-mode').textContent = t(item.game_mode);
  $('#autostart-value').textContent = t(item.autostart ? 'Yes' : 'No');
  $('#shard-list').innerHTML = ['Master', ...(item.caves ? ['Caves'] : [])].map(name => {
    const shard = runtime.shards.find(s => s.name === name);
    return `<article class="shard"><div class="shard-art ${name === 'Caves' ? 'cave-art' : ''}">${name === 'Master' ? '♧' : '◇'}</div><div><h3>${t(name === 'Master' ? 'The surface' : 'The caves')}</h3><small>${t(name === 'Master' ? 'A world of possibility' : 'A little deeper into the unknown')}</small></div><span class="status-tag ${shard?.running ? 'running' : ''}">${shard?.running ? t('Process running') : shard ? t('Exited · {code}', {code: shard.exit_code}) : t('Stopped')}</span></article>`;
  }).join('');
  I18n.bind($('#activity-list'));
}
async function refresh() {
  if (polling) return;
  polling = true;
  try {
    const first = !state;
    state = await api('/status');
    $('#connection-error').hidden = true;
    if (first) await navigate(location.pathname, {replace:true});
    else if (selected && !world()) await navigate('/overview', {replace:true});
    for (const job of state.jobs) {
      if (['failed','completed'].includes(job.state) && !seenJobs.has(job.id)) {
        seenJobs.add(job.id);
        if (!first) {
          toast(I18n.message(job.error_i18n) || job.error || job.result?.message_i18n || job.result?.message || msg('{job} completed.', {job: msg(job.title)}), job.state === 'failed');
          if (job.title === 'Create world' && job.result?.world_id) await selectWorld(job.result.world_id);
          if (job.world_id === selected) {
            if (['Create world','Restore backup','Import local save'].includes(job.title)) { fillSettings(); mods = structuredClone(world()?.mods || []); renderMods(); playerPollAt = 0; }
            if (currentTab === 'backups' && !state.busy) { await loadBackups(); await loadCharacters(); }
          }
          if (currentPage === 'archives' && !state.busy) await loadArchives();
        }
      }
    }
    render();
    if (currentTab === 'logs' && selected) await loadLogs();
    if (selected) await loadPlayers();
  } catch (error) {
    $('#connection-error').textContent = t('Connection interrupted: {error}', {error: msg(I18n.error(error))});
    $('#connection-error').hidden = false;
  } finally { polling = false; }
}
function renderArchives() {
  const list = $('#archive-list');
  $('#refresh-archives').disabled = archivesLoading || Boolean(state?.busy);
  if (archivesLoading) { list.innerHTML = `<p class="quiet-empty">${h('Loading archived worlds…')}</p>`; }
  else if (!archivedWorlds.length) { list.innerHTML = `<p class="quiet-empty">${h('No archived worlds.')}</p>`; }
  else {
    list.innerHTML = archivedWorlds.map(w => `<div class="archive-row"><div><strong>${escapeHTML(w.name)}</strong><small>${escapeHTML(w.id)}</small><p>${h('Archived copies: {copies} · Backups: {backups} · {size} MiB including logs', {copies:w.copies,backups:w.backup_count,size:w.size_mb})}</p></div><button class="button danger" data-delete-archive="${escapeHTML(w.id)}" ${state?.busy ? 'disabled' : ''}>${h('Delete permanently')}</button></div>`).join('');
  }
  I18n.bind(list);
}
async function loadArchives() {
  if (archivesLoading) return;
  archivesLoading = true;
  renderArchives();
  try { archivedWorlds = (await api('/archived-worlds')).worlds; }
  finally { archivesLoading = false; renderArchives(); }
}
function fillSettings() {
  const w = world();
  if (!w) return;
  const input = (key, title, type = 'text') => `<label data-i18n>${title}<input name="${key}" type="${type}" value="${escapeHTML(w[key])}" ${key === 'name' ? 'required maxlength="80"' : ''}></label>`;
  const check = (key, title) => `<label data-i18n class="checkbox"><input type="checkbox" name="${key}" ${w[key] ? 'checked' : ''}>${title}</label>`;
  const json = (key, title) => `<label data-i18n>${title}<textarea class="code-input" name="${key}" rows="5">${escapeHTML(JSON.stringify(w[key], null, 2))}</textarea></label>`;
  const ids = (key, title) => `<label data-i18n>${title}<textarea name="${key}" rows="3" data-i18n-placeholder placeholder="KU_abcdefgh, one per line">${escapeHTML(w[key].join('\n'))}</textarea></label>`;
  $('#settings-form').innerHTML = `<div class="form-grid">${input('name','World name')}${input('description','Description')}${input('password','Join password','password')}<label data-i18n>Klei cluster token<input name="token" type="password" autocomplete="off" data-i18n-placeholder placeholder="${w.has_token ? 'Token saved · leave blank to keep' : 'Paste your Klei token'}"></label><label data-i18n>Game mode<select name="game_mode">${['survival','endless','wilderness'].map(mode => `<option data-i18n value="${mode}" ${mode === w.game_mode ? 'selected' : ''}>${mode}</option>`).join('')}</select></label><label data-i18n>Player slots<input type="number" min="1" max="64" name="max_players" value="${w.max_players}"></label></div><div class="checks">${check('caves','Include caves')}${check('pause_when_empty','Pause when empty')}${check('pvp','Allow PvP')}${check('autostart','Start this world at boot')}</div><details><summary data-i18n>World generation & in-game snapshots</summary><p data-i18n class="field-help">Overrides are JSON objects, for example {"season_start":"autumn","world_size":"default"}. Existing terrain will not regenerate. For a fresh layout, create a new world.</p><div class="form-grid">${json('master_overrides','Surface overrides')}${json('caves_overrides','Caves overrides')}<label data-i18n>In-game save snapshots<input type="number" name="snapshots" min="1" max="50" value="${w.snapshots}"></label></div></details><details><summary data-i18n>Player permissions</summary><p data-i18n class="field-help">Use Klei user IDs, one per line. Admins can use the in-game console. Whitelisted players can bypass the join password; this is not an exclusive allowlist.</p><div id="player-candidates"></div><p class="field-help" data-i18n>Choose a candidate to add an ID below, then save settings. Names and imported IDs are hints; verify the account before granting permissions. New joins appear automatically.</p><div class="form-grid">${ids('admins','Administrators (OP)')}${ids('banned','Banned players')}${ids('whitelist','Whitelisted players')}</div></details><div class="form-actions"><button data-i18n type="submit" class="button primary">Save settings</button><span data-i18n class="muted">Changes take effect on the next start.</span></div>`;
  I18n.bind($('#settings-form'));
  renderPlayers();
}
function collectSettings(form) {
  const values = Object.fromEntries(new FormData(form));
  for (const key of ['max_players','snapshots']) if (key in values) values[key] = Number(values[key]);
  for (const key of ['caves','pause_when_empty','pvp','autostart']) if (form.elements.namedItem(key)) values[key] = form.elements.namedItem(key).checked;
  for (const key of ['master_overrides','caves_overrides']) if (key in values) {
    try { values[key] = JSON.parse(values[key] || '{}'); }
    catch { throw new I18n.Error('{field}: enter a valid JSON object.', {field: msg(key === 'master_overrides' ? 'Surface overrides' : 'Caves overrides')}); }
  }
  for (const key of ['admins','banned','whitelist']) if (key in values) values[key] = values[key].split(/\s+/).filter(Boolean);
  return values;
}
function captureMods() {
  document.querySelectorAll('.mod-row').forEach(row => {
    const index = Number(row.dataset.index);
    mods[index].enabled = row.querySelector('input[type="checkbox"]').checked;
    try { mods[index].options = JSON.parse(row.querySelector('textarea').value || '{}'); }
    catch { throw new I18n.Error(msg('Mod {id}: options must be valid JSON.', {id: mods[index].id})); }
  });
}
function renderMods() {
  $('#mods-list').innerHTML = mods.map((mod, index) => `<article class="mod-row" data-index="${index}"><div class="mod-heading"><label class="checkbox"><input type="checkbox" ${mod.enabled ? 'checked' : ''}>${h('Workshop {id}', {id: mod.id})}</label><a href="https://steamcommunity.com/sharedfiles/filedetails/?id=${mod.id}" target="_blank" rel="noreferrer" data-i18n>View ↗</a><button class="icon-button remove-mod" data-index="${index}" aria-label="Remove mod ${mod.id}" data-i18n-aria-label="${escapeHTML(JSON.stringify(msg('Remove mod {id}', {id: mod.id})))}">×</button></div><label data-i18n>Configuration options (JSON)<textarea class="code-input" rows="3">${escapeHTML(JSON.stringify(mod.options,null,2))}</textarea></label></article>`).join('') || '<div data-i18n class="quiet-empty">A world in its original form. Add your first mod below.</div>';
  I18n.bind($('#mods-list'));
}
function renderPlayers() {
  const rows = playerWorld === selected ? playerRows : [];
  const candidates = $('#player-candidates');
  if (candidates) {
    candidates.innerHTML = rows.map(player => `<div class="player-candidate"><div><strong>${escapeHTML(player.name || player.userid)}</strong><small>${escapeHTML(player.userid)} · ${escapeHTML(t(player.sources.includes('join') || player.sources.includes('console') ? 'Seen by this server' : player.sources.includes('save') ? 'Saved player' : player.sources.includes('imported_log') ? 'Saved log' : player.sources.includes('cached_owner') ? 'Cached owner · character unverified' : 'Permission list'))}</small></div><div class="player-permission-actions">${[['admins','Add admin'],['banned','Add ban'],['whitelist','Add whitelist']].map(([key,label]) => `<button type="button" class="button" data-player-id="${escapeHTML(player.userid)}" data-permission="${key}">${h(label)}</button>`).join('')}</div></div>`).join('') || `<p class="muted">${h('No known Klei IDs yet. Import a save or wait for a player to join.')}</p>`;
    const saved = characterWorld === selected ? characterRows : [];
    candidates.innerHTML += saved.map(c => `<div class="player-candidate"><div><strong>${escapeHTML(c.character || c.folder)}</strong><small>${escapeHTML(c.folder)} · ${c.userid ? h(c.identity_source === 'saved_log' ? 'Saved log account: {userid}' : 'Account hint: {userid}', {userid:c.userid}) : h('Identity not linked')} · ${escapeHTML(t(c.shard === 'Master' ? 'Surface' : 'Caves'))}</small></div><button type="button" class="button" data-match-character="${escapeHTML(c.path)}">${h('View saved character')}</button></div>`).join('');
    I18n.bind(candidates);
  }
  renderDestinations();
}
async function loadPlayers(force = false) {
  if (!selected || playerPolling || (!force && playerWorld === selected && Date.now() - playerPollAt < 5000)) return;
  playerPolling = true;
  const id = selected;
  try {
    const result = await api('/worlds/' + id + '/players');
    if (id !== selected) return;
    playerWorld = id; playerRows = result.players; playerPollAt = Date.now(); renderPlayers();
  } finally { playerPolling = false; }
}
async function loadCharacters() {
  if (!selected || state?.busy) return;
  const id = selected;
  const result = await api('/worlds/' + id + '/characters');
  if (id !== selected) return;
  characterWorld = id; characterRows = result.characters;
  renderCharacters();
  await loadPlayers(true);
}
function renderCharacters() {
  const select = $('#character-source'), previous = select.value;
  select.innerHTML = `<option value="" data-i18n>Choose a saved character</option>` + characterRows.map(c => `<option value="${escapeHTML(c.path)}">${escapeHTML(t(c.shard === 'Master' ? 'Surface' : 'Caves') + ' · ' + (c.character || c.folder) + ' · ' + c.folder + ' / ' + c.snapshot + ' · ' + c.session)}</option>`).join('');
  if (previous && [...select.options].some(option => option.value === previous)) select.value = previous;
  else if (characterRows.length === 1) select.value = characterRows[0].path;
  I18n.bind(select);
  renderDestinations();
}
function updateRecoveryButton() {
  const hasDestination = Boolean($('#character-source').value && $('#character-account').value);
  $('#recover-character').disabled = Boolean(state?.busy) || !world() || !hasDestination || ['running','degraded'].includes(world()?.runtime.state);
}
function renderDestinations() {
  const source = characterWorld === selected ? characterRows.find(c => c.path === $('#character-source').value) : null;
  const targets = source ? characterRows.filter(c => c.shard === source.shard && c.session === source.session && c.path !== source.path) : [];
  const select = $('#character-account'), previous = select.value;
  select.innerHTML = `<option value="">${escapeHTML(t(targets.length ? 'Choose a destination save' : 'No destination save yet'))}</option>` + targets.map(c => `<option value="${escapeHTML(c.path)}">${escapeHTML((c.userid || t('Unidentified account')) + ' · ' + (c.character ? c.character + ' · ' : '') + c.folder + ' / ' + c.snapshot)}</option>`).join('');
  if ([...select.options].some(option => option.value === previous)) select.value = previous;
  I18n.bind(select);
  select.disabled = !targets.length || Boolean(state?.busy);
  $('#character-steps').hidden = !source || targets.length > 0;
  $('#character-identity').hidden = !source?.userid;
  if (source?.userid) I18n.text('#character-identity', source.identity_source === 'saved_log' ? 'A saved log associates this folder with {userid}. Try joining with this account first. If your character loads, recovery is unnecessary.' : 'This folder has an account hint for {userid}. Verify it by joining the world.', {userid:source.userid});
  I18n.text('#character-info', !characterRows.length ? 'No character snapshots found. Include the complete shard save folders in your ZIP.' : !source ? 'Choose the original saved character first.' : !targets.length ? 'Only the original character is saved here. A known Klei account is not a separate character save, so there is nothing to copy into yet.' : 'Choose the destination save created by your online join. Recovery copies into that exact folder. Account labels are hints; no character is assigned automatically.');
  updateRecoveryButton();
}
async function loadBackups() {
  if (!selected) return;
  const id = selected;
  const {backups} = await api('/worlds/' + id + '/backups');
  if (id !== selected) return;
  $('#backup-list').innerHTML = backups.length ? `<div class="table-wrap"><table><thead><tr><th data-i18n>BACKUP</th><th data-i18n>CREATED</th><th data-i18n>SIZE</th><th></th></tr></thead><tbody>${backups.map(backup => `<tr><td><strong>${escapeHTML(backup.label_i18n ? t(backup.label_i18n) : backup.label)}</strong><small>${backup.id}</small></td><td>${date(backup.created_at)}</td><td>${backup.size_mb} MB</td><td class="table-actions"><button class="button restore-backup" data-snapshot="${backup.id}" data-i18n>Restore backup</button><button class="icon-button delete-backup" data-snapshot="${backup.id}" data-i18n-aria-label aria-label="Delete backup">×</button></td></tr>`).join('')}</tbody></table></div>` : '<div data-i18n class="quiet-empty">No backups yet. Save a moment you can come back to.</div>';
  I18n.bind($('#backup-list'));
}
async function loadLogs() {
  if (!selected) return;
  const id = selected, shard = $('#log-shard').value;
  const data = await api('/worlds/' + id + '/logs/' + shard);
  if (selected !== id || $('#log-shard').value !== shard) return;
  const output = $('#log-output');
  const atBottom = output.scrollTop + output.clientHeight >= output.scrollHeight - 60;
  if (data.log === 'No output yet. Start this world to see its logs.') I18n.text(output, data.log);
  else output.textContent = data.log;
  if (atBottom) output.scrollTop = output.scrollHeight;
}
document.addEventListener('click', async event => {
  const link = event.target.closest('a[data-route]');
  if (link && !event.ctrlKey && !event.metaKey && !event.shiftKey && event.button === 0) { event.preventDefault(); navigate(link.getAttribute('href')).catch(error => toast(I18n.error(error), true)); return; }
  const target = event.target.closest('button');
  if (!target) return;
  try {
    if (target.matches('.close-dialog')) { target.closest('dialog').close(); return; }
    if (target.matches('[data-world]')) await selectWorld(target.dataset.world);
    if (target.matches('[data-tab]')) await setTab(target.dataset.tab);
    if (['sidebar-create','add-world','empty-create'].includes(target.id)) $('#create-dialog').showModal();
    if (target.id === 'open-archives') await navigate('/archives');
    if (target.id === 'refresh-archives') await loadArchives();
    if (target.dataset.deleteArchive) {
      const archived = archivedWorlds.find(w => w.id === target.dataset.deleteArchive);
      if (!archived) return;
      confirmAction('Permanently delete this archived world?', msg('Delete “{name}” ({id}), including all {copies} archived copies, {backups} backups, and its logs? This cannot be undone and no new backup will be made. Type “{name}” to confirm.', {name:archived.name,id:archived.id,copies:archived.copies,backups:archived.backup_count}), 'World name', '', confirmation => queued('/archived-worlds/' + archived.id, 'DELETE', {confirmation}));
    }
    if (target.id === 'open-import') {
      importTarget = {id: world().id, name: world().name};
      $('#import-form').reset();
      I18n.text('#save-filename', 'No file chosen');
      I18n.text('#import-description', 'Import a local save into “{name}”. The imported world will stay stopped.', {name: importTarget.name});
      $('#import-progress').hidden = true;
      $('#import-status').textContent = '';
      $('#import-dialog').showModal();
    }
    if (target.id === 'logout') { await api('/logout','POST',{}); location.assign('/'); }
    if (target.dataset.action) {
      const action = target.dataset.action, url = worldURL();
      if (action === 'backup') confirmAction('Save this moment', 'A running world will save, stop briefly, and restart after the backup.', 'Backup name', t('Manual backup'), label => queued(url + '/actions/backup','POST',{label}));
      else if (['stop','restart'].includes(action)) confirmAction(action === 'stop' ? 'Stop this world?' : 'Restart this world?', 'Both shards will save and connected players will disconnect.', '', '', () => queued(url + '/actions/' + action));
      else await queued(url + '/actions/' + action);
    }
    if (target.id === 'announce-button') { const url = worldURL(); confirmAction('A word for your survivors', 'Send an in-game announcement to the world.', 'Message', '', message => queued(url + '/actions/announce','POST',{message})); }
    if (target.id === 'delete-world') { const url = worldURL(); confirmAction('Archive this world?', msg('Stop the world first. Type “{name}” to confirm. World files and a backup are kept on disk.', {name: world().name}), 'World name', '', confirmation => queued(url,'DELETE',{confirmation})); }
    if (target.matches('.restore-backup')) { const url = worldURL(); confirmAction('Restore this backup?', msg('This replaces world progress and disconnects players. A safety backup is made first. Type “{name}” to continue.', {name: world().name}), 'World name', '', confirmation => queued(url + '/backups/' + target.dataset.snapshot + '/restore','POST',{confirmation})); }
    if (target.matches('.delete-backup')) { const url = worldURL(); confirmAction('Delete this backup?', 'This backup will be permanently removed.', '', '', () => queued(url + '/backups/' + target.dataset.snapshot,'DELETE',{})); }
    if (target.dataset.permission) {
      const input = $('#settings-form').elements.namedItem(target.dataset.permission);
      const ids = input.value.split(/\s+/).filter(Boolean);
      if (!ids.includes(target.dataset.playerId)) ids.push(target.dataset.playerId);
      input.value = ids.join('\n');
      toast('ID added to the form. Save settings to apply it.');
    }
    if (target.dataset.matchCharacter) { await setTab('backups'); $('#character-source').value = target.dataset.matchCharacter; renderDestinations(); $('#character-source').focus(); }
    if (target.id === 'refresh-characters') await loadCharacters();
    if (target.id === 'recover-character') {
      const url = worldURL(), source = $('#character-source').value, destination = $('#character-account').value;
      if (!source || !destination) throw new I18n.Error('Select an original character and a destination save.');
      confirmAction('Recover this character?', msg('Copy {source} into {destination}, replacing its character files? A safety backup is made first. Type “{name}” to continue.', {source,destination,name:world().name}), 'World name', '', confirmation => queued(url + '/recover-character','POST',{source,destination,confirmation}));
    }
    if (target.id === 'native-rollback') {
      const url = worldURL(), count = Number($('#rollback-count').value);
      if (!Number.isInteger(count) || count < 1 || count > world().snapshots) throw new I18n.Error('Choose a rollback count between 1 and {maximum}.', {maximum:world().snapshots});
      confirmAction('Request native rollback?', msg('Request {count} game snapshots back. Unsaved progress will be lost. DST reloads the world. Type “{name}” to continue.', {count,name:world().name}), 'World name', '', confirmation => queued(url + '/actions/rollback','POST',{count,confirmation}));
    }
    if (target.id === 'add-mod') {
      captureMods();
      const id = $('#new-mod-id').value.trim();
      if (!/^[1-9][0-9]{4,19}$/.test(id) || mods.some(mod => mod.id === id)) throw new I18n.Error('Enter a unique numeric Workshop ID (5–20 digits).');
      mods.push({id,enabled:true,options:{}}); $('#new-mod-id').value = ''; renderMods();
    }
    if (target.matches('.remove-mod')) { captureMods(); mods.splice(Number(target.dataset.index),1); renderMods(); }
    if (target.id === 'save-mods') { captureMods(); await queued(worldURL(),'PATCH',{mods}); }
  } catch (error) { toast(I18n.error(error), true); }
});
$('#create-form').addEventListener('submit', async event => {
  event.preventDefault();
  try { await queued('/worlds','POST',collectSettings(event.target)); $('#create-dialog').close(); event.target.reset(); }
  catch (error) { toast(I18n.error(error),true); }
});
$('#settings-form').addEventListener('submit', async event => {
  event.preventDefault();
  try { await queued(worldURL(),'PATCH',collectSettings(event.target)); }
  catch (error) { toast(I18n.error(error),true); }
});
$('#confirm-form').addEventListener('submit', async event => {
  event.preventDefault();
  $('#confirm-submit').disabled = true;
  try { await confirmCallback($('#confirm-input').value); $('#confirm-dialog').close(); }
  catch (error) { toast(I18n.error(error),true); }
  finally { $('#confirm-submit').disabled = false; }
});
$('#log-shard').addEventListener('change', () => loadLogs().catch(error => toast(I18n.error(error),true)));
$('#character-source').addEventListener('change', renderDestinations);
$('#character-account').addEventListener('change', updateRecoveryButton);
$('#choose-save').addEventListener('click', () => $('#save-zip').click());
$('#save-zip').addEventListener('change', () => {
  const file = $('#save-zip').files[0];
  if (file) $('#save-filename').textContent = file.name;
  else I18n.text('#save-filename', 'No file chosen');
});
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
  I18n.text('#import-status', 'Uploading save…');
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
          I18n.text('#import-status', percent === 100 ? 'Upload received. Preparing import…' : msg('Uploading save… {percent}%', {percent}));
        }
      };
      xhr.onerror = () => reject(new I18n.Error('Upload connection failed. Check Recent activity before retrying.'));
      xhr.onload = () => {
        let data;
        try { data = JSON.parse(xhr.responseText); }
        catch { reject(new I18n.Error(xhr.status === 413 ? 'Upload rejected as too large. Check your reverse proxy upload limit.' : 'Unexpected server response. Check Recent activity before retrying.')); return; }
        if (xhr.status === 401) { location.reload(); reject(new I18n.Error('Please sign in again.')); return; }
        if (xhr.status < 200 || xhr.status >= 300) reject(new I18n.Error(I18n.message(data.error_i18n) || data.error || 'Upload failed.'));
        else resolve(data);
      };
      xhr.send(form);
    });
    $('#import-dialog').close();
    toast(msg('{job} started. Validation and replacement appear in Recent activity.', {job: msg(result.job.title)}));
    await refresh();
  } catch (error) { I18n.text('#import-status', I18n.error(error)); toast(I18n.error(error), true); }
  finally {
    uploading = false;
    document.querySelectorAll('#import-dialog button').forEach(button => { button.disabled = false; });
  }
});
document.addEventListener('languagechange', () => {
  render(); renderPlayers(); renderCharacters();
  if (currentTab === 'backups') loadBackups().catch(error => toast(I18n.error(error), true));
});
window.addEventListener('popstate', () => navigate(location.pathname, {replace:true}).catch(error => toast(I18n.error(error), true)));
refresh();
setInterval(refresh, 4000);
