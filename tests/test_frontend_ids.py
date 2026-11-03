"""Every element the front end looks up by a literal id must still exist.

v3.1 removed the Editor panel from viewer.html, and with it `#page-label` —
an element in the EDITOR's header that the viewer had been quietly writing the
page name into.  After the removal `getElementById('page-label')` returned null
and the write threw on every page change and on the opening render.  The throw
also skipped what came after it: the Edit text tool and the Watermark preview
stopped following the page you scrolled to, and the loading overlay's
hideLoading() after the first render never ran.

The JS regression suites could not see it: their DOM stub creates any element
asked for, so a missing id never comes back null there.  This checks the source
statically instead — every `getElementById('literal')` has to name an id that
viewer.html declares or that the scripts create themselves.  Ids built by
concatenation (`'page-block-' + id`) are dynamic and out of scope.
"""
import glob
import os
import re

import _bootstrap  # noqa: F401  (path setup)

_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src', 'frontend')


def _read(p):
    with open(p, encoding='utf-8') as f:
        return f.read()


def test_every_literal_id_lookup_names_an_element_that_exists():
    html = _read(os.path.join(_ROOT, 'viewer.html'))
    js = {p: _read(p) for p in glob.glob(os.path.join(_ROOT, 'js', '*.js'))}
    alljs = '\n'.join(js.values())
    known = set(re.findall(r'\bid\s*=\s*["\']([\w-]+)["\']', html + alljs))
    known |= set(re.findall(r'\.id\s*=\s*["\']([\w-]+)["\']', alljs))
    known |= set(re.findall(r"setAttribute\(\s*['\"]id['\"]\s*,\s*['\"]([\w-]+)", alljs))
    missing = []
    for path, src in js.items():
        for m in re.finditer(r"getElementById\(\s*['\"]([\w-]+)['\"]\s*\)", src):
            if m.group(1) not in known:
                line = src[:m.start()].count('\n') + 1
                missing.append(f'{os.path.basename(path)}:{line} #{m.group(1)}')
    assert not missing, 'lookups of elements that no longer exist: ' + ', '.join(missing)


if __name__ == '__main__':
    import sys
    sys.exit(_bootstrap.run_module_tests(dict(globals())))
