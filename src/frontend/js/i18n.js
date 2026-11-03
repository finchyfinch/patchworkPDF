/* i18n.js — lightweight client-side localization for Patchwork PDF.
 *
 * Loads a per-language JSON dictionary and translates the DOM in place via
 * two markers:
 *
 *   data-i18n="key"                     → replaces the element's visible text
 *                                         (child elements like <svg> icons are
 *                                          preserved — only the text node moves)
 *   data-i18n-attr="title:key;aria-label:key2"
 *                                       → sets one or more attributes
 *
 * Dynamic strings built in JS use window.t('key', {name: 'x'}).
 *
 * Fully offline: locale files are bundled next to the app and served by the
 * local Flask server at /locales/<lang>.json. English text stays in the HTML
 * as a natural fallback, so a missing key or failed load degrades gracefully.
 */
(function () {
  'use strict';

  var SUPPORTED = ['en', 'es', 'fr', 'de', 'it'];
  var FALLBACK  = 'en';
  var STORE_KEY = 'pw_lang';           // mirrors the pw_theme convention

  var dict     = {};                   // active language: flat key -> string
  var fallback = {};                   // English: shown when a key is untranslated
  var current  = FALLBACK;

  function pickInitialLang() {
    // The server's copy (lang.txt, injected as window.PW_PREFS before this
    // script) first: localStorage belongs to the page's origin, whose port can
    // change between launches, so on its own it forgets the choice.
    var server = window.PW_PREFS && window.PW_PREFS.lang;
    if (server && SUPPORTED.indexOf(server) !== -1) return server;
    try {
      var saved = localStorage.getItem(STORE_KEY);
      if (saved && SUPPORTED.indexOf(saved) !== -1) return saved;
    } catch (_) { /* private mode / disabled storage */ }
    var nav = (navigator.language || 'en').slice(0, 2).toLowerCase();
    return SUPPORTED.indexOf(nav) !== -1 ? nav : FALLBACK;
  }

  // Plural category for the supported languages. English/Spanish/German/Italian
  // pluralise at n != 1; French keeps 0 and 1 singular. Enough for two forms.
  function pluralCat(n, lang) {
    n = Math.abs(Number(n));
    if (lang === 'fr') return n <= 1 ? 'one' : 'other';
    return n === 1 ? 'one' : 'other';
  }

  function lookup(key) {
    return (dict && dict[key] != null) ? dict[key]
         : (fallback[key] != null)     ? fallback[key]
         : null;
  }

  // Translate one key. Supports {placeholder} interpolation and, when params
  // carries a numeric `count`, i18next-style plural keys (`key_one`/`key_other`).
  // Missing keys fall back to English, then to the key itself.
  function t(key, params) {
    var resolved = key;
    if (params && params.count != null) {
      var cat = key + '_' + pluralCat(params.count, current);
      if (lookup(cat) != null) resolved = cat;
      else if (lookup(key + '_other') != null) resolved = key + '_other';
    }
    var s = lookup(resolved);
    if (s == null) s = key;                       // last resort: the key
    if (params) {
      s = s.replace(/\{(\w+)\}/g, function (m, k) {
        return (params[k] != null) ? String(params[k]) : m;
      });
    }
    return s;
  }

  // Set an element's label without disturbing child elements (e.g. SVG icons):
  // replace the first non-whitespace text node, keeping its surrounding spacing
  // so the gap between an icon and its label is preserved.
  function setText(el, str) {
    for (var i = 0; i < el.childNodes.length; i++) {
      var n = el.childNodes[i];
      if (n.nodeType === 3 && n.nodeValue.trim() !== '') {
        var lead = n.nodeValue.match(/^\s*/)[0];
        var tail = n.nodeValue.match(/\s*$/)[0];
        n.nodeValue = lead + str + tail;
        return;
      }
    }
    el.appendChild(document.createTextNode(str));   // no existing text node
  }

  // Translate a subtree. Call after injecting dynamically-built markup so the
  // new nodes pick up the active language.
  function applyTo(root) {
    root = root || document;

    var textEls = root.querySelectorAll('[data-i18n]');
    for (var i = 0; i < textEls.length; i++) {
      setText(textEls[i], t(textEls[i].getAttribute('data-i18n')));
    }

    var attrEls = root.querySelectorAll('[data-i18n-attr]');
    for (var j = 0; j < attrEls.length; j++) {
      var el = attrEls[j];
      el.getAttribute('data-i18n-attr').split(';').forEach(function (pair) {
        var bits = pair.split(':');
        if (bits.length === 2) {
          var attr = bits[0].trim(), key = bits[1].trim();
          if (attr && key) el.setAttribute(attr, t(key));
        }
      });
    }

    // Keep every language <select> in sync — each of the three panels
    // (viewer / layout / editor) carries one, so this must not stop at the first.
    var sels = root.querySelectorAll ? root.querySelectorAll('[data-lang-select]') : [];
    for (var s = 0; s < sels.length; s++) sels[s].value = current;
  }

  function loadDict(lang) {
    return fetch('/locales/' + lang + '.json', { cache: 'no-cache' })
      .then(function (r) { return r.ok ? r.json() : {}; })
      .catch(function () { return {}; });   // fall back to HTML English text
  }

  // Mirror the choice to the server so pre-browser code (splash, single-instance
  // dialog) and native file dialogs can localize on the next launch. Best-effort;
  // uses the token-aware apiFetch when available (native app / served page).
  function persistServer(lang) {
    try {
      var f = window.apiFetch || window.fetch;
      f('/api/set_lang', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lang: lang }),
        keepalive: true
      });
    } catch (_) { /* persistence is optional */ }
  }

  // Public: switch language, persist the choice, re-translate the document.
  function setLang(lang) {
    if (SUPPORTED.indexOf(lang) === -1) lang = FALLBACK;
    if (lang === FALLBACK) {
      dict = fallback; current = FALLBACK;
      try { localStorage.setItem(STORE_KEY, FALLBACK); } catch (_) {}
      persistServer(FALLBACK);
      document.documentElement.setAttribute('lang', FALLBACK);
      applyTo(document);
      window.dispatchEvent(new CustomEvent('pw-lang-changed', { detail: { lang: FALLBACK } }));
      return Promise.resolve();
    }
    return loadDict(lang).then(function (d) {
      dict    = d || {};
      current = lang;
      try { localStorage.setItem(STORE_KEY, lang); } catch (_) {}
      persistServer(lang);
      document.documentElement.setAttribute('lang', lang);
      applyTo(document);
      // Notify scripts that render their own markup so they can re-localize.
      window.dispatchEvent(new CustomEvent('pw-lang-changed', { detail: { lang: lang } }));
    });
  }

  // Minimal public API for the rest of the app.
  window.I18N = {
    t: t,
    apply: applyTo,
    setLang: setLang,
    lang: function () { return current; },
    supported: SUPPORTED
  };
  window.t = t;   // convenience global for dynamic strings (Phase 2)

  // Boot: always load English as the fallback layer, plus the active language
  // (skipping a second fetch when the active language *is* English), then
  // translate as soon as the DOM exists.
  current = pickInitialLang();
  persistServer(current);   // sync lang.txt with the active language on load
  window.I18N.ready = loadDict(FALLBACK).then(function (en) {
    fallback = en || {};
    return current === FALLBACK ? fallback : loadDict(current);
  }).then(function (d) {
    dict = d || {};
    document.documentElement.setAttribute('lang', current);
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', function () { applyTo(document); });
    } else {
      applyTo(document);
    }
  });
})();
