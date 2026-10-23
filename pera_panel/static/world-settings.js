'use strict';
// Values follow DST's map/customize.lua. Unknown advanced options are preserved.
window.WorldSettings = (() => {
  const amount = [['never','None'],['rare','Very few'],['uncommon','Fewer'],['often','More'],['mostly','Many'],['always','Very many'],['insane','Maximum']];
  const frequency = [['never','Never'],['rare','Rare'],['often','Often'],['always','Always']];
  const seasons = [['noseason','Disabled'],['veryshortseason','Very short'],['shortseason','Short'],['longseason','Long'],['verylongseason','Very long'],['random','Random']];
  const both = ['master_overrides','caves_overrides'];
  const field = (key, label, group, choices, shards = both, defaultLabel = 'Normal') => ({key,label,group,choices,shards,defaultLabel});
  const catalog = [
    field('world_size','World size','Layout', [['small','Small'],['medium','Medium'],['huge','Huge']], both, 'Large'),
    field('branching','Land branches','Layout', [['never','None'],['least','Least'],['most','Most'],['random','Random']], both, 'Standard'),
    field('loop','Land loops','Layout', [['never','Never'],['always','Always']], both, 'Random'),
    field('season_start','Starting season','Layout', [['winter','Winter'],['spring','Spring'],['summer','Summer'],['autumn|spring','Autumn or spring'],['winter|summer','Winter or summer'],['autumn|winter|spring|summer','Random']], ['master_overrides'], 'Autumn'),
    ...[['autumn','Autumn length'],['winter','Winter length'],['spring','Spring length'],['summer','Summer length']].map(([key,label]) => field(key,label,'Seasons & day cycle',seasons,['master_overrides'], ['autumn','spring'].includes(key) ? '20 days' : '15 days')),
    field('day','Day / dusk / night','Seasons & day cycle', [['longday','Long day'],['longdusk','Long dusk'],['longnight','Long night'],['noday','No day'],['nodusk','No dusk'],['nonight','No night'],['onlyday','Only day'],['onlydusk','Only dusk'],['onlynight','Only night']], ['master_overrides'], 'Seasonal'),
    ...[['grass','Grass'],['sapling','Saplings'],['berrybush','Berry bushes'],['trees','Trees'],['rock','Boulders'],['flint','Flint']].map(([key,label]) => field(key,label,'Resources',amount)),
    ...[['mushtree','Mushroom trees'],['flower_cave','Light flowers']].map(([key,label]) => field(key,label,'Resources',amount,['caves_overrides'])),
    field('spiders','Spider dens','Creatures & attacks',amount),
    ...[['beefalo','Beefalo'],['rabbits','Rabbit holes'],['pigs','Pig houses'],['bees','Beehives']].map(([key,label]) => field(key,label,'Creatures & attacks',amount,['master_overrides'])),
    ...[['bunnymen','Bunny hutches'],['rocky','Rock lobsters'],['cave_spiders','Cave spider dens']].map(([key,label]) => field(key,label,'Creatures & attacks',amount,['caves_overrides'])),
    ...[['hounds','Hound attacks'],['deerclops','Deerclops']].map(([key,label]) => field(key,label,'Creatures & attacks',frequency,['master_overrides'])),
  ];
  const groups = ['Layout','Seasons & day cycle','Resources','Creatures & attacks'];
  const shardNames = {master_overrides:'Surface',caves_overrides:'Caves'};
  const escape = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const message = (key, params = {}) => `data-i18n-message="${escape(JSON.stringify({key,params}))}"`;
  function parse(text) {
    const value = JSON.parse(text || '{}');
    if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error('Expected an object');
    return value;
  }
  function change(overrides, key, value) {
    const updated = {...overrides};
    if (value === 'default') delete updated[key];
    else updated[key] = value;
    return updated;
  }
  function choices(option, overrides) {
    const value = Object.hasOwn(overrides, option.key) ? overrides[option.key] : 'default';
    const items = [['default', {key:'Default · {value}',params:{value:{key:option.defaultLabel}}}], ...option.choices.map(([key,label]) => [key,{key:label}])];
    if (!items.some(([key]) => key === value)) items.push(['__custom__', {key:'Custom · {value}',params:{value:JSON.stringify(value)}}]);
    return {items, selected:items.some(([key]) => key === value) ? value : '__custom__'};
  }
  function renderOptions(select, overrides) {
    const option = catalog.find(item => item.key === select.dataset.worldOption);
    const {items,selected} = choices(option, overrides);
    select.innerHTML = items.map(([value,label]) => `<option value="${escape(value)}" ${message(label.key,label.params)} ${value === selected ? 'selected' : ''} ${value === '__custom__' ? 'disabled' : ''}></option>`).join('');
  }
  function sync(shard) {
    const textarea = shard.querySelector('textarea');
    const error = shard.querySelector('[data-world-error]');
    let overrides;
    try { overrides = parse(textarea.value); }
    catch {
      error.hidden = false;
      shard.querySelectorAll('[data-world-option]').forEach(select => { select.disabled = true; });
      return false;
    }
    error.hidden = true;
    shard.querySelectorAll('[data-world-option]').forEach(select => { select.disabled = false; renderOptions(select, overrides); });
    const count = Object.keys(overrides).length;
    I18n.text(shard.querySelector('[data-world-summary]'), count ? {key:'{count} saved overrides',params:{count}} : 'Standard preset · no overrides');
    I18n.bind(shard);
    return true;
  }
  function updateCaves(container) {
    const enabled = container.closest('form').elements.namedItem('caves')?.checked !== false;
    container.querySelector('[data-world-shard="caves_overrides"]').hidden = !enabled;
    container.querySelector('[data-caves-note]').hidden = enabled;
  }
  function mount(container, world = {}, {creating = false} = {}) {
    container.classList.add('world-generation');
    container.innerHTML = `<h3 data-i18n>World generation</h3><p class="field-help" data-i18n>Standard Surface and Caves presets. Default means DST’s standard value; mods may change it. The same settings can produce different map layouts.</p>
      <p class="notice" data-i18n>${creating ? 'Choose these settings before the first start, when DST generates the world.' : 'These are the panel’s saved generation choices, not readings from the live save. Configure them before the first start. Existing terrain will not regenerate, and imported saves may use different settings.'}</p>
      ${both.map(shard => `<details class="world-shard" data-world-shard="${shard}" ${shard === 'master_overrides' ? 'open' : ''}>
        <summary><span data-i18n>${shardNames[shard]}</span><small data-world-summary></small></summary>
        ${shard === 'caves_overrides' ? '<p class="field-help" data-i18n>Seasons and the day cycle are controlled by the Surface.</p>' : ''}
        ${groups.map(group => {
          const fields = catalog.filter(item => item.group === group && item.shards.includes(shard));
          return fields.length ? `<details class="world-option-group" ${group === 'Layout' ? 'open' : ''}><summary data-i18n>${group}</summary><div class="form-grid">${fields.map(option => `<label><span data-i18n>${option.label}</span><select data-world-option="${option.key}"></select></label>`).join('')}</div></details>` : '';
        }).join('')}
        <p class="notice error" data-world-error hidden data-i18n>Fix the advanced JSON object to use the controls or save these settings.</p>
        <details class="world-option-group"><summary data-i18n>Advanced overrides (JSON)</summary><p class="field-help" data-i18n>Both editors share the same settings. Unknown options and custom values are kept. Select Default to remove an override.</p><label><span data-i18n>${shard === 'master_overrides' ? 'Surface overrides' : 'Caves overrides'}</span><textarea class="code-input" name="${shard}" rows="6" spellcheck="false">${escape(JSON.stringify(world[shard] || {}, null, 2))}</textarea></label></details>
      </details>`).join('')}<p class="field-help" data-caves-note hidden data-i18n>Caves are disabled. Their settings are kept but will not be used.</p>`;
    container.querySelectorAll('[data-world-shard]').forEach(shard => {
      const textarea = shard.querySelector('textarea');
      textarea.addEventListener('input', () => sync(shard));
      shard.addEventListener('change', event => {
        if (!event.target.matches('[data-world-option]')) return;
        const updated = change(parse(textarea.value), event.target.dataset.worldOption, event.target.value);
        textarea.value = JSON.stringify(updated, null, 2);
        sync(shard);
      });
      sync(shard);
    });
    I18n.bind(container);
    updateCaves(container);
  }
  return {mount,updateCaves,parse,change,choices,catalog};
})();
