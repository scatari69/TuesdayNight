'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, 'web_static/i18n.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, 'web_static/index.html'), 'utf8');

function load(saved, blocked = false) {
  const values = new Map(saved === undefined ? [] : [['tn-language', saved]]);
  const context = {
    localStorage: {
      getItem(key) { if (blocked) throw Error('Storage blocked'); return values.get(key) ?? null; },
      setItem(key, value) { if (blocked) throw Error('Storage blocked'); values.set(key, value); }
    },
    document: {documentElement: {lang: 'ru'}, querySelectorAll: () => []}
  };
  context.window = context;
  vm.runInNewContext(source, context, {filename: 'i18n.js'});
  return {api: context.I18n, values, context};
}

const {api, values} = load();
assert.equal(api.getLanguage(), 'ru', 'Default language must be Russian');
api.setLanguage('en');
assert.equal(api.getLanguage(), 'en');
assert.equal(values.get('tn-language'), 'en', 'Selection must persist in browser storage');
assert.equal(load('en').api.getLanguage(), 'en', 'Saved selection must restore after reload');
for (const invalid of ['de', '', '<script>', 'RU']) {
  assert.equal(load(invalid).api.getLanguage(), 'ru', 'Invalid stored locale must fall back to Russian');
}
api.setLanguage('de');
assert.equal(api.getLanguage(), 'ru', 'Unsupported selection must use the safe default');
const blocked = load('en', true).api;
assert.equal(blocked.getLanguage(), 'ru');
assert.doesNotThrow(() => blocked.setLanguage('en'), 'Blocked localStorage must not break the panel');

const translations = api.translations;
assert.ok(Object.keys(translations).length > 20, 'Navigation and form labels need English translations');
for (const [ru, en] of Object.entries(translations)) {
  assert.equal(typeof en, 'string', 'English translations must be strings');
  assert.ok(en.trim(), `Missing translation for ${ru}`);
  api.setLanguage('ru');
  assert.equal(api.t(ru), ru, 'Russian source text must be preserved');
  api.setLanguage('en');
  assert.equal(api.t(ru), en, `Dictionary translation failed: ${ru}`);
  assert.doesNotMatch(en, /[А-Яа-яЁё]/u, `Russian text leaked into English dictionary: ${ru}`);
}
let staticLabels = 0;
for (const match of html.matchAll(/data-i18n(?:-[a-z-]+)?="([^"]+)"/g)) {
  staticLabels++;
  assert.ok(Object.hasOwn(translations, match[1]), `Static label is missing from dictionary: ${match[1]}`);
}
assert.ok(staticLabels >= 20, 'Static HTML labels must opt into translation');

const textElement = {dataset: {i18n: 'К содержимому'}};
const placeholderElement = {dataset: {i18nPlaceholder: 'Выберите класс'}};
const ariaElement = {dataset: {i18nAriaLabel: 'Навигация'}, setAttribute(key, value) { this[key] = value; }};
const document = {documentElement: {lang: 'ru'}, querySelectorAll(selector) {
  return selector === '[data-i18n]' ? [textElement] : selector === '[data-i18n-placeholder]' ? [placeholderElement] : [ariaElement];
}};
api.setLanguage('en');
api.applyStatic(document);
assert.equal(document.documentElement.lang, 'en');
assert.equal(textElement.textContent, 'Skip to content');
assert.equal(placeholderElement.placeholder, 'Choose a class');
assert.equal(ariaElement['aria-label'], 'Navigation');
assert.equal(api.dateLocale(), 'en-GB');
api.setLanguage('ru');
api.applyStatic(document);
assert.equal(document.documentElement.lang, 'ru');
assert.equal(textElement.textContent, 'К содержимому');
assert.equal(api.dateLocale(), 'ru-RU');
api.setLanguage('en');
const app = fs.readFileSync(path.join(__dirname, 'web_static/app.js'), 'utf8');
for (const match of app.matchAll(/'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*"/g)) {
  if (!/[А-Яа-яЁё]/u.test(match[0])) continue;
  const label = vm.runInNewContext(match[0]);
  assert.ok(Object.hasOwn(translations, label), `Runtime UI label is missing from dictionary: ${label}`);
}
for (const userText of ['Гильдия <script>alert(1)</script>', 'Warrior / Protection', 'Мой сервер №42']) {
  assert.equal(api.t(userText), userText, 'Unknown user text must not be translated or modified');
}
console.log('RU/EN fallback, persistence, dictionary and user-content checks passed');
