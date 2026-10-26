'use strict';
// The dashboard displays plain text, not a terminal. Clean each full snapshot so
// existing saved output and escape sequences split across polling updates work too.
window.LogOutput = (() => {
  function plainText(value) {
    return String(value ?? '')
      // Terminal titles/hyperlinks and other terminal-only string payloads.
      .replace(/(?:\x1b\]|\x9d)[\s\S]*?(?:\x07|\x1b\\|\x9c|$)/g, '')
      .replace(/(?:\x1b[P^_X]|[\x90\x98\x9e\x9f])[\s\S]*?(?:\x1b\\|\x9c|$)/g, '')
      // CSI includes SteamCMD's color/reset codes and cursor/erase commands.
      // An unfinished sequence at the end stays hidden until the next snapshot.
      .replace(/(?:\x1b\[|\x9b)[0-?]*[ -/]*(?:[@-~]|$)/g, '')
      .replace(/\x1b[ -/]*(?:[0-~]|$)/g, '')
      .replace(/\r\n?/g, '\n')
      .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]/g, '');
  }
  return {plainText};
})();
