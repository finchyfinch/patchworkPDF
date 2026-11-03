"""The locale files stay complete and actually translated.

Two ways a string silently stays English in a translated UI, both of which
shipped: a key added to en.json and copied into the others untranslated (about
60 strings per language — toasts, errors, confirm dialogs, update checks, print
navigation — until v3.2), and a message built in JS from a string literal
instead of a key (the tool hints and "added" toasts, also fixed in v3.2).  The
first is checked here for EVERY key; the second by viewer_comment_panel.test.mjs
and review.

A string that is legitimately the same in a language — a font or library name,
a unit, a word both languages spell alike — goes in the lists below.  Adding to
them is a decision, not a way to make the test pass: check with a speaker.
"""
import json
import os
import re

import _bootstrap  # noqa: F401  (path setup)

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'locales')
_LANGS = ('de', 'fr', 'es', 'it')

# Names, never translated: fonts, libraries, the publisher.
_NAMES = re.compile(r'^(at_text_font|at_sig_font|hf_font|ml_font|about_description)\.'
                    r'|^about_publisher\.')
# Units and marks every language writes the same way.
_EVERYWHERE = {'compress_custom.dpi', 'hf_modal.pt', 'insert_custom_row.mm',
               'zoom_pill.zoom_aria'}
# Words that are genuinely the same in that language.
_SAME_ON_PURPOSE = {
    'de': {'about_version.version', 'annot_tools.text', 'at_sig_props.name',
           'at_tool_ellipse.ellipse', 'at_tool_text.text', 'comments.type_ellipse',
           'comments.type_text', 'confirm.ok', 'ed_tools.text', 'ed_wm_props.text',
           'ff_type.dropdown', 'rp_group.your_name',
           # "Layout" is the name of the screen, used as-is in German UIs
           'sticky_top.layout', 'viewer.header.layout', 'viewer_empty_state.layout'},
    'fr': {'about_version.version', 'annot_tools.style', 'at_sig_props.style',
           'at_sticky_props.note', 'at_tool_ellipse.ellipse', 'at_tool_image.image',
           'at_tool_rect.rectangle_tip', 'badge.count_pages_one', 'badge.count_pages_other',
           'comments.page', 'comments.type_ellipse', 'comments.type_rect', 'confirm.ok',
           'ed_ff_section.type', 'ed_wm_props.angle', 'ed_wm_props.style',
           'filetype.documents', 'filetype.images', 'hf_modal.centre',
           'lp_title_text.pages', 'print_section.pages_range', 'toolbar.page'},
    'es': {'about_modal.manual'},
    'it': {'confirm.ok', 'file_menu.file_aria', 'file_menu_btn.file',
           'pp_set_section.password',
           'sticky_top.layout', 'viewer.header.layout', 'viewer_empty_state.layout'},
}


def _load(lang):
    with open(os.path.join(_DIR, lang + '.json'), encoding='utf-8') as f:
        return json.load(f)


def test_every_locale_has_exactly_the_english_keys():
    en = set(_load('en'))
    for lang in _LANGS:
        keys = set(_load(lang))
        assert keys == en, (lang, sorted(en - keys), sorted(keys - en))


def test_every_string_is_translated():
    en = _load('en')
    for lang in _LANGS:
        d = _load(lang)
        untranslated = sorted(
            k for k, v in en.items()
            if d.get(k) == v and re.search('[A-Za-z]{2}', v)
            and not _NAMES.match(k) and k not in _EVERYWHERE
            and k not in _SAME_ON_PURPOSE[lang])
        assert not untranslated, f'{lang}: still English: {untranslated}'


def test_the_same_on_purpose_lists_do_not_go_stale():
    """A key listed as 'same on purpose' that is now translated (or gone) is a
    stale exemption, and would hide the next regression on that key."""
    en = _load('en')
    for lang in _LANGS:
        d = _load(lang)
        stale = sorted(k for k in _SAME_ON_PURPOSE[lang] if k not in en or d[k] != en[k])
        assert not stale, (lang, stale)


def test_placeholders_survive_translation():
    en = _load('en')
    for lang in _LANGS:
        d = _load(lang)
        for k, v in en.items():
            want = sorted(re.findall(r'\{\w+\}', v))
            assert sorted(re.findall(r'\{\w+\}', d[k])) == want, (lang, k, d[k])


if __name__ == '__main__':
    import sys
    sys.exit(_bootstrap.run_module_tests(dict(globals())))
