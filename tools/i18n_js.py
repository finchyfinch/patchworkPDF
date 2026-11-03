#!/usr/bin/env python3
"""i18n_js.py — wrap user-facing toast strings in window.t() calls.

Conservative, idempotent JS transform for the Phase-2b pass. It targets only
the dominant, unambiguous pattern:

    showToast('literal' ...)   ->   showToast(window.t('toast.<slug>') ...)
    showToast("literal" ...)   ->   showToast(window.t('toast.<slug>') ...)

Only a *plain leading string literal* right after `showToast(` is replaced;
everything after it (concatenated variables, the `type` argument) is left
untouched. Calls whose first argument is a template literal, a ternary, or a
variable are skipped — nothing to mistranslate. Re-running is a no-op because a
rewritten call now starts with `window.t(`, not a quote.

window.t (not bare t) is used because showToast's own body and a few call sites
shadow `t` with a local const.

Default run reports proposed changes to the scratchpad. --apply rewrites the JS
files and merges the new keys into locales/en.json.

Usage:
    python tools/i18n_js.py                 # dry report
    python tools/i18n_js.py --apply         # rewrite JS + en.json
"""
from __future__ import annotations

import argparse
import json
import os
import re

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCALES = os.path.join(APP_DIR, "locales")
# Frontend JS moved under src/frontend/js/ (v2.15 reorg).
JS_DIR = os.path.join(APP_DIR, "src", "frontend", "js")
JS_FILES = ("shared.js", "layout.js", "viewer-panel.js", "editor.js")

# showToast(  <quote> ...(escaped)... <quote>
CALL = re.compile(r"""showToast\(\s*(['"])((?:\\.|(?!\1).)*)\1""")


def unescape_js(s: str) -> str:
    return (s.replace("\\'", "'").replace('\\"', '"')
             .replace("\\n", "\n").replace("\\t", "\t").replace("\\\\", "\\"))


def slugify(text: str, max_words: int = 6) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "_".join(words[:max_words]) or "msg"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.environ.get("PW_I18N_OUT", "."))
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    en_path = os.path.join(LOCALES, "en.json")
    en = json.load(open(en_path, encoding="utf-8"))

    key_by_text: dict[str, str] = {}
    used = set(k.split(".", 1)[1] for k in en if k.startswith("toast."))
    rows = []          # (file, english, key)
    edits_per_file = {}

    def assign_key(english: str) -> str:
        if english in key_by_text:
            return key_by_text[english]
        base = slugify(english)
        slug, n = base, 2
        while slug in used:
            slug, n = f"{base}_{n}", n + 1
        used.add(slug)
        key = f"toast.{slug}"
        key_by_text[english] = key
        return key

    for name in JS_FILES:
        path = os.path.join(JS_DIR, name)
        src = open(path, encoding="utf-8").read()

        def repl(m):
            english = unescape_js(m.group(2))
            key = assign_key(english)
            rows.append((name, english, key))
            return f"showToast(window.t('{key}')"

        new_src = CALL.sub(repl, src)
        edits_per_file[name] = new_src

    # Proposed catalog additions (English source of truth)
    additions = {k: t for t, k in key_by_text.items()}
    proposed = dict(en)
    proposed.update({k: v for k, v in additions.items() if k not in proposed})

    os.makedirs(args.out, exist_ok=True)
    report = ["# JS toast extraction\n",
              f"- showToast literals found: **{len(rows)}**",
              f"- unique keys: **{len(key_by_text)}**",
              f"- en.json: {len(en)} -> {len(proposed)} keys\n",
              "| key | english |", "|-----|---------|"]
    for eng, key in sorted(key_by_text.items(), key=lambda kv: kv[1]):
        report.append(f"| `{key}` | {eng.replace(chr(10), '\\n')[:70]} |")
    open(os.path.join(args.out, "_i18n_js_report.md"), "w",
         encoding="utf-8").write("\n".join(report))

    print(f"{len(rows)} showToast literals across {len(JS_FILES)} files; "
          f"{len(key_by_text)} unique keys.")
    per = {}
    for f, _, _ in rows:
        per[f] = per.get(f, 0) + 1
    for f in JS_FILES:
        print(f"  {f}: {per.get(f, 0)}")

    if args.apply:
        for name, new_src in edits_per_file.items():
            open(os.path.join(JS_DIR, name), "w", encoding="utf-8").write(new_src)
        json.dump(proposed, open(en_path, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        open(en_path, "a", encoding="utf-8").write("\n")
        print(f"APPLIED: JS files rewritten; en.json now {len(proposed)} keys.")
    else:
        print("Dry run — nothing written. Re-run with --apply.")


if __name__ == "__main__":
    main()
