#!/usr/bin/env python3
"""i18n_extract.py — find translatable UI strings and (optionally) tag them.

Scans viewer.html for
  * visible text nodes, and
  * localizable attributes (title, placeholder, aria-label, alt)
skipping <script>/<style>/<svg> subtrees, the language <select>, and anything
already carrying data-i18n / data-i18n-attr (or an explicit data-i18n-skip).

It proposes a dotted key for each string (namespaced by the nearest ancestor
id) and:

  * writes a human-readable report + a proposed catalog to the scratchpad
    (default, non-destructive), and
  * with --apply, injects the markers into viewer.html and merges the new keys
    into locales/en.json.

Auto-tagging rule for TEXT nodes: a parent is tagged with data-i18n only when
it holds exactly one significant text run — the safe, no-new-element,
no-layout-change case that i18n.js handles by design. Parents with several text
runs (e.g. the "Patchwork PDF — Viewer" logo) are reported for manual <span>
wrapping instead of being rewritten blindly.

JS files are scanned in report-only mode: user-facing string literals (toasts,
confirms, textContent/title assignments) are listed as candidates for the
Phase-2 manual pass — JS is not rewritten automatically.

Usage:
    python tools/i18n_extract.py                # dry report only
    python tools/i18n_extract.py --apply        # rewrite viewer.html + en.json
    python tools/i18n_extract.py --html viewer.html --out <dir>
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from html.parser import HTMLParser

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Frontend sources moved under src/frontend/ (v2.15 reorg).
FRONTEND_DIR = os.path.join(APP_DIR, "src", "frontend")
JS_DIR = os.path.join(FRONTEND_DIR, "js")

LOCALIZABLE_ATTRS = ("title", "placeholder", "aria-label", "alt")
SKIP_SUBTREE_TAGS = {"script", "style", "svg"}          # ignore all text/attrs inside
VOID_TAGS = {"br", "hr", "img", "input", "meta", "link", "source", "track", "wbr"}

# Elements whose single text run we are willing to auto-tag with data-i18n.
LABEL_TAGS = {
    "button", "span", "div", "label", "option", "a", "p", "li", "td", "th",
    "h1", "h2", "h3", "h4", "h5", "h6", "summary", "legend", "figcaption",
    "small", "strong", "b", "em", "i",
}

JS_FILES = ("shared.js", "layout.js", "viewer-panel.js", "editor.js")

# High-signal, user-facing JS string patterns (report-only).
JS_PATTERNS = [
    (re.compile(r"""showToast\s*\(\s*(['"])(?P<s>(?:\\.|(?!\1).)*?)\1"""), "toast"),
    (re.compile(r"""confirm\s*\(\s*(['"])(?P<s>(?:\\.|(?!\1).)*?)\1"""), "confirm"),
    (re.compile(r"""\.textContent\s*=\s*(['"])(?P<s>(?:\\.|(?!\1).)*?)\1"""), "textContent"),
    (re.compile(r"""\.title\s*=\s*(['"])(?P<s>(?:\\.|(?!\1).)*?)\1"""), "title"),
    (re.compile(r"""\.placeholder\s*=\s*(['"])(?P<s>(?:\\.|(?!\1).)*?)\1"""), "placeholder"),
]


def slugify(text: str, max_words: int = 4) -> str:
    """Turn a UI string into a short, stable key fragment."""
    text = html.unescape(text).lower()
    words = re.findall(r"[a-z0-9]+", text)
    if not words:
        return "str"
    return "_".join(words[:max_words])


# A string that is *entirely* a keyboard shortcut ("Ctrl+N", "F5", "Shift+Enter")
# stays literal. A shortcut embedded in a sentence ("Find in document (Ctrl+F)")
# is still translated — hence fullmatch, not search.
SHORTCUT_ONLY = re.compile(
    r"^(Ctrl|Shift|Alt|Cmd|Meta|Win|Fn|⌘|⌥|⇧)(\s*\+\s*\w+)+$|^F\d{1,2}$", re.I)

# Brand tokens are never translated.
BRAND_TERMS = {"patchwork", "pdf", "patchworkpdf", "patchwork pdf"}


def looks_translatable(text: str) -> bool:
    """A string worth translating: has letters, more than one char, not code,
    not a bare keyboard shortcut, not a brand token."""
    core = html.unescape(text).strip()
    if len(core) < 2:
        return False
    if not re.search(r"[A-Za-z]", core):
        return False
    # Skip obvious template/interpolation fragments and bare symbols.
    if "{{" in core or "${" in core:
        return False
    if SHORTCUT_ONLY.match(core):
        return False
    if core.lower() in BRAND_TERMS:
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# HTML extraction
# ─────────────────────────────────────────────────────────────────────────────
class Candidate:
    __slots__ = ("kind", "attr", "text", "line", "key")

    def __init__(self, kind, text, line, attr=None):
        self.kind = kind          # "text" | "attr" | "manual-text"
        self.attr = attr          # attribute name for kind == "attr"
        self.text = text          # decoded english string
        self.line = line
        self.key = None


class Frame:
    __slots__ = ("tag", "attrs", "start_off", "starttag_text", "el_id",
                 "runs", "has_i18n", "i18n_attrs")

    def __init__(self, tag, attrs, start_off, starttag_text):
        self.tag = tag
        self.attrs = dict(attrs)
        self.start_off = start_off
        self.starttag_text = starttag_text
        self.el_id = self.attrs.get("id")
        self.runs = []            # list of (abs_off, raw, decoded, line)
        self.has_i18n = "data-i18n" in self.attrs
        self.i18n_attrs = self.attrs.get("data-i18n-attr", "")


class Extractor(HTMLParser):
    def __init__(self, source: str):
        # convert_charrefs=False keeps entities raw so source offsets stay exact.
        super().__init__(convert_charrefs=False)
        self.source = source
        self.line_starts = [0]
        for line in source.splitlines(keepends=True):
            self.line_starts.append(self.line_starts[-1] + len(line))
        self.stack: list[Frame] = []
        self.candidates: list[Candidate] = []
        # Edits collected for --apply: (abs_off, old_len, new_text).
        self.edits: list[tuple[int, int, str]] = []
        self._key_by_text: dict[tuple[str, str], str] = {}   # (ns, text) -> key
        self._used_keys: set[str] = set()

    # -- position helpers -----------------------------------------------------
    def _abs(self) -> int:
        line, col = self.getpos()
        return self.line_starts[line - 1] + col

    def _in_skip_subtree(self) -> bool:
        return any(f.tag in SKIP_SUBTREE_TAGS for f in self.stack)

    def _in_lang_select(self) -> bool:
        return any("data-lang-select" in f.attrs or f.tag == "select"
                   and "data-lang-select" in f.attrs for f in self.stack) or \
               any("data-lang-select" in f.attrs for f in self.stack)

    def _skip_flagged(self) -> bool:
        return any("data-i18n-skip" in f.attrs or "data-lang-select" in f.attrs
                   for f in self.stack)

    def _namespace(self) -> str:
        for f in reversed(self.stack):
            if f.el_id:
                return f.el_id.replace("-", "_")
        return "ui"

    def _make_key(self, text: str, suffix: str = "") -> str:
        ns = self._namespace()
        decoded = html.unescape(text).strip()
        cache_key = (ns, decoded + "|" + suffix)
        if cache_key in self._key_by_text:
            return self._key_by_text[cache_key]
        base = f"{ns}.{slugify(decoded)}" + (f"_{suffix}" if suffix else "")
        key = base
        n = 2
        while key in self._used_keys:
            key = f"{base}_{n}"
            n += 1
        self._used_keys.add(key)
        self._key_by_text[cache_key] = key
        return key

    # -- parser callbacks -----------------------------------------------------
    def handle_starttag(self, tag, attrs):
        start_off = self._abs()
        starttag_text = self.get_starttag_text() or ""
        frame = Frame(tag, attrs, start_off, starttag_text)
        # Attribute candidates (skip inside skip-subtrees / lang select / flagged).
        if not self._in_skip_subtree() and not self._skip_flagged() \
                and "data-lang-select" not in frame.attrs:
            existing = frame.i18n_attrs
            for name, val in attrs:
                if name in LOCALIZABLE_ATTRS and val and looks_translatable(val) \
                        and f"{name}:" not in existing:
                    self.stack.append(frame)     # namespace uses this element's id too
                    suffix = {"title": "tip", "aria-label": "aria",
                              "placeholder": "ph", "alt": "alt"}[name]
                    key = self._make_key(val, suffix)
                    self.stack.pop()
                    c = Candidate("attr", html.unescape(val),
                                  self.getpos()[0], attr=name)
                    c.key = key
                    self.candidates.append(c)
                    self._queue_attr_edit(frame, name, key)
        if tag not in VOID_TAGS:
            self.stack.append(frame)

    def handle_startendtag(self, tag, attrs):
        # e.g. <input .../>  — treat as start tag for attrs, no push.
        self.handle_starttag(tag, attrs)
        if self.stack and self.stack[-1].tag == tag \
                and self.stack[-1].start_off == self._abs():
            self.stack.pop()

    def handle_data(self, data):
        if not data.strip() or self._in_skip_subtree() or self._skip_flagged():
            return
        if not self.stack:
            return
        if looks_translatable(data):
            abs_off = self._abs()
            self.stack[-1].runs.append(
                (abs_off, data, html.unescape(data).strip(), self.getpos()[0]))

    def handle_endtag(self, tag):
        # Pop back to the matching frame (tolerate minor imbalance).
        idx = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i].tag == tag:
                idx = i
                break
        if idx is None:
            return
        frame = self.stack[idx]
        del self.stack[idx:]
        self._finalize(frame)

    def _finalize(self, frame: Frame):
        runs = frame.runs
        if not runs or frame.has_i18n:
            return
        if frame.tag not in LABEL_TAGS:
            # Non-label container with direct text — report, don't auto-tag.
            for _, _, decoded, line in runs:
                c = Candidate("manual-text", decoded, line)
                self.candidates.append(c)
            return
        if len(runs) == 1:
            abs_off, raw, decoded, line = runs[0]
            self.stack.append(frame)
            key = self._make_key(decoded)
            self.stack.pop()
            c = Candidate("text", decoded, line)
            c.key = key
            self.candidates.append(c)
            self._queue_text_edit(frame, key)
        else:
            # Several text runs in one element → needs manual <span> wrapping.
            for _, _, decoded, line in runs:
                c = Candidate("manual-text", decoded, line)
                self.candidates.append(c)

    # -- edit queue -----------------------------------------------------------
    def _queue_text_edit(self, frame: Frame, key: str):
        """Add data-i18n="key" to the frame's start tag."""
        insert = f' data-i18n="{key}"'
        self._insert_into_starttag(frame, insert)

    def _queue_attr_edit(self, frame: Frame, attr: str, key: str):
        """Add/extend data-i18n-attr on the frame's start tag."""
        # Merge multiple attrs on the same element into one data-i18n-attr.
        for i, (off, ln, txt) in enumerate(self.edits):
            if off == self._starttag_attr_anchor(frame) and 'data-i18n-attr="' in txt:
                merged = txt.rstrip('"') + f';{attr}:{key}"'
                self.edits[i] = (off, ln, merged)
                return
        insert = f' data-i18n-attr="{attr}:{key}"'
        self._insert_into_starttag(frame, insert)

    def _starttag_attr_anchor(self, frame: Frame) -> int:
        st = frame.starttag_text
        gt = st.rfind("/>")
        if gt == -1:
            gt = st.rfind(">")
        return frame.start_off + gt

    def _insert_into_starttag(self, frame: Frame, insert: str):
        anchor = self._starttag_attr_anchor(frame)
        self.edits.append((anchor, 0, insert))

    # -- apply ----------------------------------------------------------------
    def apply_edits(self) -> str:
        out = self.source
        for off, old_len, new_text in sorted(self.edits, key=lambda e: e[0],
                                             reverse=True):
            out = out[:off] + new_text + out[off + old_len:]
        return out


# ─────────────────────────────────────────────────────────────────────────────
# JS extraction (report-only)
# ─────────────────────────────────────────────────────────────────────────────
def scan_js(path: str) -> list[dict]:
    hits = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return hits
    for lineno, line in enumerate(lines, 1):
        for rx, kind in JS_PATTERNS:
            for m in rx.finditer(line):
                s = m.group("s")
                if looks_translatable(s):
                    hits.append({"line": lineno, "kind": kind, "text": s})
    return hits


# ─────────────────────────────────────────────────────────────────────────────
# Driver
# ─────────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--html", default=os.path.join(FRONTEND_DIR, "viewer.html"))
    ap.add_argument("--locales", default=os.path.join(APP_DIR, "locales"))
    ap.add_argument("--out", default=os.environ.get("PW_I18N_OUT", "."),
                    help="directory for the report + proposed catalog")
    ap.add_argument("--apply", action="store_true",
                    help="rewrite viewer.html and merge keys into locales/en.json")
    args = ap.parse_args()

    with open(args.html, "r", encoding="utf-8") as f:
        source = f.read()

    ex = Extractor(source)
    ex.feed(source)
    ex.close()

    # Existing en.json (source of truth) — never overwrite keyed strings.
    en_path = os.path.join(args.locales, "en.json")
    existing = {}
    if os.path.isfile(en_path):
        with open(en_path, "r", encoding="utf-8") as f:
            existing = json.load(f)

    proposed = dict(existing)
    auto_text = auto_attr = manual = 0
    for c in ex.candidates:
        if c.kind == "manual-text":
            manual += 1
            continue
        if c.key and c.key not in proposed:
            proposed[c.key] = c.text
        if c.kind == "text":
            auto_text += 1
        elif c.kind == "attr":
            auto_attr += 1

    # JS candidates
    js_report = {}
    js_total = 0
    for name in JS_FILES:
        hits = scan_js(os.path.join(JS_DIR, name))
        js_report[name] = hits
        js_total += len(hits)

    os.makedirs(args.out, exist_ok=True)
    cat_path = os.path.join(args.out, "_i18n_candidates.en.json")
    with open(cat_path, "w", encoding="utf-8") as f:
        json.dump(proposed, f, ensure_ascii=False, indent=2)

    # Human-readable report
    report = []
    report.append("# i18n extraction report\n")
    report.append(f"- HTML source: `{os.path.relpath(args.html, APP_DIR)}`")
    report.append(f"- Existing keys in en.json: **{len(existing)}**")
    report.append(f"- New auto-taggable TEXT nodes: **{auto_text}**")
    report.append(f"- New auto-taggable ATTRIBUTES: **{auto_attr}**")
    report.append(f"- Manual-review TEXT runs (multi-run / container): **{manual}**")
    report.append(f"- Proposed total catalog size: **{len(proposed)}**")
    report.append(f"- JS candidate strings (report-only): **{js_total}**\n")

    report.append("## Sample of proposed HTML keys\n")
    report.append("| key | english | kind | line |")
    report.append("|-----|---------|------|------|")
    for c in ex.candidates[:60]:
        k = c.key or "—"
        t = c.text.replace("|", "\\|")[:50]
        extra = f"attr:{c.attr}" if c.kind == "attr" else c.kind
        report.append(f"| `{k}` | {t} | {extra} | {c.line} |")

    report.append("\n## Manual-review text runs\n")
    manual_runs = [c for c in ex.candidates if c.kind == "manual-text"]
    if manual_runs:
        for c in manual_runs[:40]:
            report.append(f"- line {c.line}: `{c.text[:60]}`")
    else:
        report.append("_none_")

    report.append("\n## JS candidate strings (manual Phase-2 pass)\n")
    for name, hits in js_report.items():
        report.append(f"### {name} — {len(hits)}")
        for h in hits[:25]:
            report.append(f"- L{h['line']} [{h['kind']}] `{h['text'][:60]}`")
        report.append("")

    rep_path = os.path.join(args.out, "_i18n_report.md")
    with open(rep_path, "w", encoding="utf-8") as f:
        f.write("\n".join(report))

    print(f"HTML: {auto_text} text + {auto_attr} attrs auto-taggable, "
          f"{manual} manual, {len(existing)} already keyed.")
    print(f"JS:   {js_total} candidate strings across {len(JS_FILES)} files.")
    print(f"Report:   {os.path.relpath(rep_path)}")
    print(f"Catalog:  {os.path.relpath(cat_path)}")

    if args.apply:
        new_html = ex.apply_edits()
        with open(args.html, "w", encoding="utf-8") as f:
            f.write(new_html)
        with open(en_path, "w", encoding="utf-8") as f:
            json.dump(proposed, f, ensure_ascii=False, indent=2)
        print(f"APPLIED: {len(ex.edits)} markers written to "
              f"{os.path.relpath(args.html)}; en.json now has {len(proposed)} keys.")
    else:
        print("Dry run — no files modified. Re-run with --apply to write changes.")


if __name__ == "__main__":
    main()
