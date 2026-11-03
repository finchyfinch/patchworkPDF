/* Minimal headless DOM for exercising the viewer's front-end code in Node.
 *
 * Not a browser: it models only what the viewer's scroll/tab code actually
 * reads — a scrollable #canvas-area, a #canvas-column whose children stack
 * vertically, and element ids/classes/styles.  Everything else resolves to an
 * inert stub element so unrelated UI code (toolbars, thumbnails, toasts) runs
 * without the test having to describe it.
 *
 * Layout model: an element's height is its explicit style.height, or the
 * tallest child's height, or 0.  offsetTop is the sum of the preceding
 * siblings' heights.  That is exactly the geometry the page column relies on,
 * and it lets a test grow a placeholder to simulate a page rendering at its
 * real height.
 */

export const GAP_H       = 20;   // .page-gap height (css: viewer.css)
export const COLUMN_PAD  = 24;   // #canvas-column top padding

class FakeEl {
  constructor(doc, tag) {
    this._doc = doc;
    this.tagName = String(tag || 'div').toUpperCase();
    this._id = '';
    this.className = '';
    // A real CSSStyleDeclaration carries these; production code uses them for
    // custom properties (e.g. the layout grid's --card-min), so the stub must
    // too or it fails on a call every browser supports.
    this.style = {
      setProperty(name, value) { this[name] = String(value); },
      getPropertyValue(name)   { return this[name] != null ? this[name] : ''; },
      removeProperty(name)     { const v = this[name]; delete this[name]; return v || ''; },
    };
    this.dataset = {};
    this.children = [];
    this.parentNode = null;
    this.textContent = '';
    this.value = '';
    this._html = '';
    this._listeners = {};
    this._attrs = {};
    this._explicitHeight = null;   // set by tests to simulate a rendered page
    this.clientHeight = 0;
    this.clientWidth  = 0;
    this._scrollTop  = 0;
    this._scrollLeft = 0;
    this._scrollEventPending = false;
    this.classList = {
      add:    (...c) => { c.forEach(x => { if (!this._cls().includes(x)) this.className = (this.className + ' ' + x).trim(); }); },
      remove: (...c) => { this.className = this._cls().filter(x => !c.includes(x)).join(' '); },
      toggle: (c, on) => { if (on === undefined) on = !this._cls().includes(c);
                           if (on) this.classList.add(c); else this.classList.remove(c); return on; },
      contains: (c) => this._cls().includes(c),
    };
  }

  _cls() { return this.className ? this.className.split(/\s+/) : []; }

  // Scrolling — programmatic and user scrolls are indistinguishable to a
  // listener, and both fire one coalesced event on the next turn, exactly as a
  // browser does.  The viewer relies on that (it flags its own scrolls so a
  // real user scroll can cancel a pending re-pin), so the stub must model it.
  get scrollTop()  { return this._scrollTop; }
  set scrollTop(v) { if (v !== this._scrollTop) { this._scrollTop = v; this._queueScrollEvent(); } }
  get scrollLeft()  { return this._scrollLeft; }
  set scrollLeft(v) { if (v !== this._scrollLeft) { this._scrollLeft = v; this._queueScrollEvent(); } }
  _queueScrollEvent() {
    if (this._scrollEventPending) return;
    this._scrollEventPending = true;
    setTimeout(() => { this._scrollEventPending = false; this.dispatch('scroll'); }, 0);
  }

  get id() { return this._id; }
  set id(v) { this._id = v; if (v) this._doc._register(v, this); }

  get innerHTML() { return this._html; }
  set innerHTML(v) {
    this._html = String(v == null ? '' : v);
    // Wiping a container drops its children (and their id registrations) —
    // this is how the viewer clears the page column between documents.
    this.children.forEach(c => this._doc._unregisterTree(c));
    this.children = [];
  }

  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  removeChild(c) {
    this.children = this.children.filter(x => x !== c);
    this._doc._unregisterTree(c);
    return c;
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  insertBefore(c, ref) {
    const i = this.children.indexOf(ref);
    c.parentNode = this;
    if (i < 0) this.children.push(c); else this.children.splice(i, 0, c);
    return c;
  }
  replaceChild(nu, old) { const i = this.children.indexOf(old);
    if (i >= 0) { this.children[i] = nu; nu.parentNode = this; this._doc._unregisterTree(old); } return old; }

  setAttribute(k, v) { this._attrs[k] = String(v); }
  getAttribute(k) { return k in this._attrs ? this._attrs[k] : null; }
  removeAttribute(k) { delete this._attrs[k]; }
  hasAttribute(k) { return k in this._attrs; }

  querySelector() { return null; }
  querySelectorAll() { return []; }
  closest() { return null; }
  contains(el) { return el === this || this.children.some(c => c.contains && c.contains(el)); }
  getContext() { return _canvasCtxStub(); }
  focus() {} blur() {} select() {} scrollIntoView() {}
  addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
  removeEventListener(type, fn) {
    this._listeners[type] = (this._listeners[type] || []).filter(f => f !== fn);
  }
  dispatch(type, ev) { (this._listeners[type] || []).slice().forEach(fn => fn(ev || {type, target: this})); }
  _scrollArea() {
    let n = this;
    while (n && !n._isScrollArea) n = n.parentNode;
    return n;
  }
  getBoundingClientRect() {
    if (this._isScrollArea) {
      return { top: 0, left: 0, width: this.clientWidth, height: this.clientHeight,
               right: this.clientWidth, bottom: this.clientHeight };
    }
    const area = this._scrollArea();
    const top  = this.offsetTop - (area ? area.scrollTop : 0);
    const left = -(area ? area.scrollLeft : 0);
    const h = this.offsetHeight;
    return { top, left, width: area ? area.clientWidth : 0, height: h,
             right: left + (area ? area.clientWidth : 0), bottom: top + h };
  }

  // Web Animations: real enough that code can start and cancel one.
  animate() { return { cancel() {}, finish() {}, play() {}, finished: Promise.resolve() }; }

  // ── layout ────────────────────────────────────────────────────────────────
  setHeight(px) { this._explicitHeight = px; }
  get offsetHeight() {
    if (this._explicitHeight != null) return this._explicitHeight;
    if (this._cls().includes('page-gap')) return GAP_H;
    const styled = parseFloat(this.style.height);
    if (!isNaN(styled)) return styled;
    // A canvas is laid out at its bitmap size unless CSS overrides it — that is
    // how a page block grows and shrinks with the zoom.
    if (this.tagName === 'CANVAS' && this.height) return this.height;
    return this.children.reduce((m, c) => Math.max(m, c.offsetHeight), 0);
  }
  get offsetWidth() {
    if (this._cls().includes('page-gap')) return 0;
    const styled = parseFloat(this.style.width);
    if (!isNaN(styled)) return styled;
    if (this.tagName === 'CANVAS' && this.width) return this.width;
    return this.children.reduce((m, c) => Math.max(m, c.offsetWidth), 0);
  }
  get offsetLeft() {
    // The column centres its pages; everything else starts at its parent's edge.
    const par = this.parentNode;
    if (!par) return 0;
    const base = par._isScrollRoot ? COLUMN_PAD : par.offsetLeft;
    if (!par._isScrollRoot) return base;
    const avail = Math.max(0, (par.parentNode ? par.parentNode.clientWidth : 0) - COLUMN_PAD * 2);
    return base + Math.max(0, (avail - this.offsetWidth) / 2);
  }
  get offsetTop() {
    const par = this.parentNode;
    if (!par) return 0;
    let y = 0;
    for (const c of par.children) { if (c === this) break; y += c.offsetHeight; }
    return y + (par._isScrollRoot ? COLUMN_PAD : par.offsetTop);
  }
  get scrollHeight() {
    return this.children.reduce((s, c) => s + c.offsetHeight, 0) + COLUMN_PAD * 2;
  }
}

function _canvasCtxStub() {
  const noop = () => {};
  // measureText has to answer with a real object: the editor sizes its inline
  // text box from `ctx.measureText(line).width`, and a no-op would throw there
  // rather than just draw nothing.  7px per character is a plausible monospace.
  const real = { measureText: (s) => ({ width: String(s == null ? '' : s).length * 7 }) };
  return new Proxy(real, {
    get: (t, k) => (k in t ? t[k] : (k === 'canvas' ? undefined : noop)),
  });
}

export function makeDocument() {
  const doc = {
    _byId: new Map(),
    _register(id, el) { this._byId.set(id, el); },
    _unregisterTree(el) {
      if (!el) return;
      if (el._id) this._byId.delete(el._id);
      (el.children || []).forEach(c => this._unregisterTree(c));
    },
    createElement(tag) { return new FakeEl(doc, tag); },
    createElementNS(_ns, tag) { return new FakeEl(doc, tag); },
    createTextNode(t) { const e = new FakeEl(doc, 'text'); e.textContent = t; return e; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    addEventListener() {}, removeEventListener() {},
    getElementById(id) {
      if (doc._byId.has(id)) return doc._byId.get(id);
      // Page blocks and thumbs must report "missing" honestly — the viewer
      // branches on their absence.  Any other id gets an inert stub so
      // incidental UI code doesn't need to be modelled.
      if (/^page-block-|^thumb-(?!list$)/.test(id)) return null;
      const el = new FakeEl(doc, 'div');
      el.id = id;
      return el;
    },
  };
  doc.body = new FakeEl(doc, 'body');
  doc.documentElement = new FakeEl(doc, 'html');
  doc.head = new FakeEl(doc, 'head');

  // The two elements with real geometry.
  const area = new FakeEl(doc, 'div');
  area.id = 'canvas-area';
  area._isScrollArea = true;
  area.clientHeight = 800;
  area.clientWidth  = 900;
  const column = new FakeEl(doc, 'div');
  column.id = 'canvas-column';
  column._isScrollRoot = true;
  area.appendChild(column);

  // The thumbnail pane: a scrollable element of its own.  Its items are built
  // from an innerHTML string, so it has no children here — the tests that care
  // about it drive its scrollTop, which is exactly what the viewer remembers.
  const thumbs = new FakeEl(doc, 'div');
  thumbs.id = 'thumb-list';
  thumbs._isScrollArea = true;
  thumbs.clientHeight = 700;
  thumbs.clientWidth  = 180;

  // The editor's scroll area (one element, shared by every tab) and its canvas.
  const edArea = new FakeEl(doc, 'div');
  edArea.id = 'ed-canvas-area';
  edArea._isScrollArea = true;
  edArea.clientHeight = 800;
  edArea.clientWidth  = 900;
  const edCanvas = new FakeEl(doc, 'canvas');
  edCanvas.id = 'editor-canvas';
  edArea.appendChild(edCanvas);

  return { doc, area, column, thumbs, edArea, edCanvas };
}

export { FakeEl };
