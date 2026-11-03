"""Every tool button sits on the tab the code says it belongs to.

v3.2 split the right panel into three tabs — Comment, Edit, Fill & Sign — by
what a tool does to the document.  Two things record which tab a tool is on:
where its button sits in viewer.html (inside `#rp-group-<tab>`), and
`_TOOL_TAB` in viewer-panel.js, which decides which tab to bring forward when
the tool is armed and which tools to put away when the user leaves a tab.

If the two disagree, arming a tool shows a tab that does not contain it — the
button the user just pressed vanishes, and the tool is armed with none of its
settings in view.  The JS suites cannot see this (their DOM stub does not parse
viewer.html), so it is checked statically here.
"""
import os
import re

import _bootstrap  # noqa: F401  (path setup)

_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src', 'frontend')


def _read(name):
    with open(os.path.join(_ROOT, name), encoding='utf-8') as f:
        return f.read()


def _buttons_by_group():
    """{tool: tab} from where each at-tool button sits in viewer.html."""
    html = _read('viewer.html')
    out = {}
    starts = [(m.start(), m.group(1))
              for m in re.finditer(r'<div class="rp-group" id="rp-group-(\w+)"', html)]
    assert [t for _, t in starts] == ['comment', 'edit', 'sign'], starts
    ends = [s for s, _ in starts[1:]] + [html.index('<input type="file" id="at-image-input"')]
    for (start, tab), end in zip(starts, ends):
        for tool in re.findall(r'id="at-tool-(\w+)"', html[start:end]):
            out[tool] = tab
    return out


def _tool_tab_in_js():
    js = _read(os.path.join('js', 'viewer-panel.js'))
    body = re.search(r'const _TOOL_TAB = \{(.*?)\};', js, re.S).group(1)
    return dict(re.findall(r"(\w+):\s*'(\w+)'", body))


def test_every_tool_button_is_on_the_tab_the_code_expects():
    assert _buttons_by_group() == _tool_tab_in_js()


def test_every_tool_is_on_a_tab():
    html = _read('viewer.html')
    every = set(re.findall(r'id="at-tool-(\w+)"', html))
    assert every == set(_buttons_by_group()), (
        'a tool button outside every tab is unreachable: '
        + ', '.join(sorted(every - set(_buttons_by_group()))))


def test_the_tool_list_and_the_tab_map_name_the_same_tools():
    js = _read(os.path.join('js', 'viewer-panel.js'))
    ids = re.search(r'const _TOOL_IDS = \[(.*?)\];', js, re.S).group(1)
    assert set(re.findall(r"'(\w+)'", ids)) == set(_tool_tab_in_js())


if __name__ == '__main__':
    import sys
    sys.exit(_bootstrap.run_module_tests(dict(globals())))
