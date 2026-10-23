const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {runInNewContext} = require('node:vm');
const window = {};
const root = join(__dirname, '..', 'pera_panel', 'static');
runInNewContext(readFileSync(join(root, 'world-settings.js'), 'utf8'), {window});
const {parse, change, choices, catalog} = window.WorldSettings;
const plain = value => JSON.parse(JSON.stringify(value));

test('common controls preserve unknown options and custom typed values', () => {
  const original = parse('{"world_size":"huge","mod_option":{"enabled":true},"seed":123,"custom":false}');
  const updated = change(original, 'grass', 'often');
  assert.deepEqual(plain(updated), {...plain(original),grass:'often'});
  assert.equal(original.grass, undefined);
  const defaults = change(updated, 'world_size', 'default');
  assert.equal(Object.hasOwn(defaults, 'world_size'), false);
  assert.deepEqual(plain(defaults.mod_option), {enabled:true});
  assert.equal(defaults.seed, 123);
  assert.equal(defaults.custom, false);
});

test('existing custom values are shown as custom without replacing the saved data', () => {
  const size = catalog.find(option => option.key === 'world_size');
  for (const value of ['modded-size', 12, false, {name:'custom'}, ['large']]) {
    const state = choices(size, {world_size:value});
    assert.equal(state.selected, '__custom__');
    assert.equal(state.items.at(-1)[1].params.value, JSON.stringify(value));
  }
  assert.equal(choices(size, {}).selected, 'default');
  assert.equal(choices(size, {world_size:'default'}).selected, 'default');
  assert.equal(choices(size, {world_size:'huge'}).selected, 'huge');
});

test('advanced input must be an object, never an array or scalar', () => {
  for (const text of ['[]','null','true','12','"text"','{invalid}']) assert.throws(() => parse(text));
  assert.deepEqual(plain(parse('')), {});
  assert.deepEqual(plain(parse('{"custom": [1,true,"string"]}')), {custom:[1,true,'string']});
});

test('shared world settings are shown on Surface and defaults are explicit', () => {
  for (const key of ['season_start','autumn','winter','spring','summer','day']) {
    assert.deepEqual(plain(catalog.find(item => item.key === key).shards), ['master_overrides']);
  }
  assert.equal(catalog.find(item => item.key === 'world_size').defaultLabel, 'Large');
  assert.equal(catalog.find(item => item.key === 'autumn').defaultLabel, '20 days');
  assert.equal(catalog.find(item => item.key === 'winter').defaultLabel, '15 days');
  assert.equal(catalog.find(item => item.key === 'rock').label, 'Boulders');
});

test('every control and choice has a Chinese translation', () => {
  const translations = JSON.parse(readFileSync(join(root, 'zh-CN.json'), 'utf8'));
  for (const option of catalog) {
    for (const text of [option.label,option.group,option.defaultLabel,...option.choices.map(([,label]) => label)]) {
      assert.ok(translations[text], `Missing translation: ${text}`);
    }
  }
});
