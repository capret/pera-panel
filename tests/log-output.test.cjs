const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {runInNewContext} = require('node:vm');
const window = {};
runInNewContext(readFileSync(join(__dirname, '..', 'pera_panel', 'static', 'log-output.js'), 'utf8'), {window});
const {plainText} = window.LogOutput;

test('SteamCMD resets and colors render as readable plain text', () => {
  const raw = 'Loading Steam API...\x1b[0mOK\n\x1b[0m\n' +
    'Connecting anonymously to Steam Public...\x1b[0mOK\n' +
    '\x1b[0mWaiting for client config...\x1b[0m\n\x1b[1;32mSuccess!\x1b[m';
  assert.equal(plainText(raw), 'Loading Steam API...OK\n\nConnecting anonymously to Steam Public...OK\nWaiting for client config...\nSuccess!');
});

test('polling snapshots do not leak partial control sequences', () => {
  const sequence = '\x1b[38;2;255;0;0m';
  for (let end = 1; end <= sequence.length; end++) {
    assert.equal(plainText('Downloading...' + sequence.slice(0, end)), 'Downloading...');
  }
  assert.equal(plainText('Downloading...' + sequence + 'OK\x1b[0m'), 'Downloading...OK');
  assert.equal(plainText('Ready\x1b]0;partial title'), 'Ready');
});

test('terminal titles, hyperlinks, cursor codes and controls are hidden', () => {
  assert.equal(plainText('\x1b]0;SteamCMD\x07\x1b[?25lReady\x1b[2K\x1b[?25h'), 'Ready');
  assert.equal(plainText('\x1b]8;;https://example.com\x1b\\link text\x1b]8;;\x1b\\'), 'link text');
  assert.equal(plainText('A\x1bPterminal payload\x1b\\B\x1b(B\x1b7C\x00\x07\x7f'), 'ABC');
  assert.equal(plainText('\x9b31m更新\x9b0m\x9dtitle\x9c'), '更新');
});

test('progress line endings are readable and ordinary log text survives', () => {
  assert.equal(plainText('10%\r20%\r\nDone\n'), '10%\n20%\nDone\n');
  const text = '[  0%] Checking...\n[02:21:34]\t玩家 joined 🌲\n[0m is literal text\n<img src=x onerror=alert(1)> & "quoted"';
  assert.equal(plainText(text), text);
  assert.equal(plainText(null), '');
});
