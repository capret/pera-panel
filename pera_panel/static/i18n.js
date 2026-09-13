'use strict';
// Only annotated interface text is translated. Saved content and log output are never scanned.
window.I18n = (() => {
  const catalog = JSON.parse(document.querySelector('#i18n-catalog').textContent);
  const supported = ['en', 'zh-CN'];
  let language;
  try { language = localStorage.getItem('pera-language'); } catch { /* Storage may be disabled. */ }
  if (!supported.includes(language)) language = (navigator.languages || [navigator.language]).some(value => /^zh\b/i.test(value)) ? 'zh-CN' : 'en';
  const bindings = new Map();
  const attributes = new Map();
  const descriptor = (key, params = {}) => ({key, params});
  function t(message, params = {}) {
    if (message && typeof message === 'object') return t(message.key, message.params);
    const key = String(message ?? '');
    const pattern = language === 'zh-CN' && Object.hasOwn(catalog, key) ? catalog[key] : key;
    return pattern.replace(/\{(\w+)\}/g, (match, name) => Object.hasOwn(params, name)
      ? (typeof params[name] === 'object' && params[name] !== null ? t(params[name]) : String(params[name])) : match);
  }
  function text(target, message, params = {}) {
    const element = typeof target === 'string' ? document.querySelector(target) : target;
    element.textContent = t(message, params);
    if (!element.firstChild) element.appendChild(document.createTextNode(''));
    bindings.set(element.firstChild, {message: descriptor(message, params), prefix:'', suffix:''});
  }
  function bind(root = document) {
    root.querySelectorAll('[data-i18n-message]').forEach(element => {
      const value = JSON.parse(element.dataset.i18nMessage);
      if (!bindings.has(element.firstChild)) text(element, value);
    });
    root.querySelectorAll('[data-i18n]').forEach(element => {
      for (const node of element.childNodes) {
        if (node.nodeType !== Node.TEXT_NODE || !node.textContent.trim() || bindings.has(node)) continue;
        const source = node.textContent;
        bindings.set(node, {message: source.trim(), prefix: source.match(/^\s*/)[0], suffix: source.match(/\s*$/)[0]});
      }
    });
    for (const attribute of ['placeholder', 'aria-label', 'title']) {
      root.querySelectorAll(`[data-i18n-${attribute}]`).forEach(element => {
        if (!attributes.has(element)) attributes.set(element, {});
        const entries = attributes.get(element);
        if (!entries[attribute]) entries[attribute] = element.getAttribute(`data-i18n-${attribute}`)
          ? JSON.parse(element.getAttribute(`data-i18n-${attribute}`)) : element.getAttribute(attribute);
      });
    }
    update();
  }
  function update() {
    document.documentElement.lang = language;
    for (const [node, value] of bindings) {
      if (!node.isConnected) { bindings.delete(node); continue; }
      node.textContent = value.prefix + t(value.message) + value.suffix;
    }
    for (const [element, entries] of attributes) {
      if (!element.isConnected) { attributes.delete(element); continue; }
      for (const [attribute, value] of Object.entries(entries)) element.setAttribute(attribute, t(value));
    }
    document.querySelectorAll('[data-language]').forEach(select => { select.value = language; });
  }
  function setLanguage(value) {
    if (!supported.includes(value)) return;
    language = value;
    try { localStorage.setItem('pera-language', value); } catch { /* Still switch for this page. */ }
    update();
    document.dispatchEvent(new CustomEvent('languagechange'));
  }
  function message(payload) {
    if (!payload || typeof payload !== 'object') return payload;
    // These parameters are internal field identifiers, never user-provided world names.
    const params = {...payload.params};
    for (const key of ['name', 'key', 'shard']) if (typeof params[key] === 'string') params[key] = descriptor(params[key]);
    return descriptor(payload.key, params);
  }
  class LocalizedError extends Error {
    constructor(value, params = {}) { super(typeof value === 'string' ? value : value.key); this.i18n = descriptor(value, params); }
  }
  function error(value) {
    if (value.i18n) return value.i18n;
    if (value instanceof TypeError) return 'Connection failed. Check your network and try again.';
    return value.message || 'The request failed.';
  }
  bind();
  document.querySelectorAll('[data-language]').forEach(select => select.addEventListener('change', () => setLanguage(select.value)));
  return {t, text, bind, setLanguage, descriptor, message, Error: LocalizedError, error,
    get language() { return language; }, get locale() { return language === 'zh-CN' ? 'zh-CN' : 'en-US'; }};
})();
