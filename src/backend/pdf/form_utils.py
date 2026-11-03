"""
form_utils.py — PDF AcroForm utilities for Patchwork PDF.

Reading:   extract_all_form_fields(pdf_bytes) → {page_idx: [field_dict, …]}
Writing:   add_field_to_page_pdf / delete_field_from_page_pdf
Save path: build_acroform_on_save(merged_bytes, session_pages)
"""

import io
import uuid
import logging

logger = logging.getLogger(__name__)


# ── Reading ───────────────────────────────────────────────────────────────────

def extract_all_form_fields(pdf_bytes):
    """Read every AcroForm Widget annotation from a full (un-split) PDF.
    Returns {page_index: [field_dict, …]} for pages that have form fields."""
    import pypdf
    result = {}
    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        for pi, page in enumerate(reader.pages):
            annots = page.get('/Annots')
            if not annots:
                continue
            if hasattr(annots, 'get_object'):
                annots = annots.get_object()
            mb = page.mediabox
            page_h = float(mb.height)
            fields = []
            for ref in annots:
                try:
                    f = _parse_widget(ref, page_h)
                    if f:
                        fields.append(f)
                except Exception:
                    continue
            if fields:
                result[pi] = fields
    except Exception as e:
        logger.warning(f'[form_utils] extract_all_form_fields: {e}')
    return result


def _resolve(v):
    if hasattr(v, 'get_object'):
        return v.get_object()
    return v


def _cascade(ann, parent, key, default=None):
    v = ann.get(key)
    if v is None and parent is not None:
        v = parent.get(key)
    if v is None:
        return default
    return _resolve(v)


def _parse_widget(ref, page_h):
    """Parse one annotation reference into a field dict.  Returns None if not a widget."""
    ann = _resolve(ref)
    if str(ann.get('/Subtype', '')) != '/Widget':
        return None

    # /FT may live on a parent field node
    ft = ann.get('/FT')
    parent = None
    if ft is None:
        pr = ann.get('/Parent')
        if pr is not None:
            parent = _resolve(pr)
            ft = parent.get('/FT')
    if ft is None:
        return None

    ft_str = str(ft)
    ff = int(_cascade(ann, parent, '/Ff', 0))

    if ft_str == '/Tx':
        ftype = 'multiline' if (ff & 4096) else 'text'
    elif ft_str == '/Btn':
        ftype = 'radio' if (ff & 32768) else 'checkbox'
    elif ft_str == '/Ch':
        ftype = 'dropdown' if (ff & 131072) else 'listbox'
    else:
        return None

    name = str(_cascade(ann, parent, '/T', ''))

    v_raw = _cascade(ann, parent, '/V')
    if v_raw is None:
        value = ''
    elif str(v_raw) == '/Off':
        value = 'off'
    elif str(v_raw).startswith('/') and ftype in ('checkbox', 'radio'):
        value = 'on' if str(v_raw) != '/Off' else 'off'
    else:
        value = str(v_raw)

    rect = _resolve(ann.get('/Rect'))
    if rect is None:
        return None
    rv = [float(v) for v in rect]
    if len(rv) < 4:
        return None

    options = _parse_options(ann, parent)
    font_size = _parse_font_size(ann, parent)
    label = str(_cascade(ann, parent, '/TU', '') or '')

    return {
        'id':        str(uuid.uuid4()),
        'name':      name,
        'type':      ftype,
        'x1':        rv[0],
        'y1':        rv[1],
        'x2':        rv[2],
        'y2':        rv[3],
        'page_h':    page_h,
        'value':     value,
        'options':   options,
        'font_size': font_size,
        'label':     label,
        'required':  bool(ff & 2),
        'readonly':  bool(ff & 1),
        'source':    'existing',
    }


def _parse_options(ann, parent):
    opt_raw = _cascade(ann, parent, '/Opt')
    if not opt_raw:
        return []
    options = []
    for o in opt_raw:
        if hasattr(o, '__iter__') and not isinstance(o, (str, bytes)):
            ol = list(o)
            options.append(str(ol[-1]) if ol else '')
        else:
            options.append(str(o))
    return options


def _parse_font_size(ann, parent):
    da_raw = str(_cascade(ann, parent, '/DA', ''))
    parts = da_raw.split()
    for i, p in enumerate(parts):
        if p == 'Tf' and i >= 1:
            try:
                return int(float(parts[i - 1]))
            except (ValueError, IndexError):
                pass
    return 10


# ── Button appearance streams (checkbox / radio) ──────────────────────────────
# Checkbox and radio widgets need an /AP appearance dictionary with an "on" and
# an "off" state, otherwise many PDF viewers render nothing (relying solely on
# /NeedAppearances is unreliable — Preview, Chrome and others ignore it for
# buttons).  These build the tiny Form XObjects that draw a tick (checkbox) or a
# filled dot (radio); the off state is an empty form, so it draws nothing.

def _sanitize_state_name(s):
    """Make a safe PDF Name (no '/', spaces, delimiters) from an export value."""
    import re
    out = re.sub(r'[^A-Za-z0-9_.\-]', '_', str(s or '')).strip('_')
    return out or 'On'


def _checkbox_on_ops(w, h):
    """Content stream drawing a tick mark filling the widget box."""
    lw  = max(0.6, min(w, h) * 0.11)
    ins = min(w, h) * 0.22
    return (f'q {lw:.2f} w 0 0 0 RG '
            f'{ins:.2f} {ins:.2f} m {w - ins:.2f} {h - ins:.2f} l S '
            f'{ins:.2f} {h - ins:.2f} m {w - ins:.2f} {ins:.2f} l S Q'
            ).encode('latin-1')


def _radio_on_ops(w, h):
    """Content stream drawing a filled dot centred in the widget box."""
    cx, cy = w / 2.0, h / 2.0
    r = min(w, h) * 0.28
    k = 0.5523 * r
    return (
        'q 0 0 0 rg '
        f'{cx + r:.2f} {cy:.2f} m '
        f'{cx + r:.2f} {cy + k:.2f} {cx + k:.2f} {cy + r:.2f} {cx:.2f} {cy + r:.2f} c '
        f'{cx - k:.2f} {cy + r:.2f} {cx - r:.2f} {cy + k:.2f} {cx - r:.2f} {cy:.2f} c '
        f'{cx - r:.2f} {cy - k:.2f} {cx - k:.2f} {cy - r:.2f} {cx:.2f} {cy - r:.2f} c '
        f'{cx + k:.2f} {cy - r:.2f} {cx + r:.2f} {cy - k:.2f} {cx + r:.2f} {cy:.2f} c '
        'f Q'
    ).encode('latin-1')


def _form_xobject(pikepdf, doc, w, h, ops):
    """Build an indirect Form XObject (appearance stream) of size w×h."""
    xo = pikepdf.Stream(doc, ops)
    xo['/Type']    = pikepdf.Name('/XObject')
    xo['/Subtype'] = pikepdf.Name('/Form')
    # Plain Python numbers: pikepdf maps float -> PDF Real, int -> PDF Integer.
    # (pikepdf.Real() only exists in pikepdf >= 10; macOS Intel is capped at 9.x.)
    xo['/BBox']    = pikepdf.Array([0.0, 0.0, float(w), float(h)])
    return doc.make_indirect(xo)


def _attach_button_appearance(pikepdf, doc, widget, ftype, on_state, is_on):
    """Give a checkbox/radio widget an /AP (on + off states) and set /AS.
    on_state is the export-value Name (without leading '/').  Returns nothing;
    mutates *widget* in place."""
    try:
        rect = widget.get('/Rect')
        rv = [float(v) for v in rect]
        w = abs(rv[2] - rv[0]) or 12.0
        h = abs(rv[3] - rv[1]) or 12.0
    except Exception:
        w = h = 12.0
    ops = _radio_on_ops(w, h) if ftype == 'radio' else _checkbox_on_ops(w, h)
    on_ref  = _form_xobject(pikepdf, doc, w, h, ops)
    off_ref = _form_xobject(pikepdf, doc, w, h, b'')
    n_dict = pikepdf.Dictionary()
    n_dict[pikepdf.Name('/' + on_state)] = on_ref
    n_dict[pikepdf.Name('/Off')]         = off_ref
    widget['/AP'] = pikepdf.Dictionary(N=n_dict)
    widget['/AS'] = pikepdf.Name('/' + (on_state if is_on else 'Off'))
    # A neutral border/background so the box is visible even when empty.
    widget['/MK'] = pikepdf.Dictionary(
        BC=pikepdf.Array([0.0, 0.0, 0.0]))


# ── Writing (per-page PDF) ────────────────────────────────────────────────────

def add_field_to_page_pdf(pdf_bytes, field):
    """Insert a new Widget annotation into a single-page PDF using pikepdf.
    Returns updated bytes, or original on failure."""
    try:
        import pikepdf
        with pikepdf.open(io.BytesIO(pdf_bytes)) as doc:
            if not doc.pages:
                return pdf_bytes
            page = doc.pages[0]
            widget = _build_widget(field)
            # Checkbox / radio need an appearance stream to render in-app and in
            # external viewers (see _attach_button_appearance).
            ftype = field.get('type')
            if ftype in ('checkbox', 'radio'):
                on_state = _button_on_state(field)
                is_on = _button_is_on(field, on_state)
                _attach_button_appearance(pikepdf, doc, widget, ftype, on_state, is_on)
                widget['/V'] = pikepdf.Name('/' + (on_state if is_on else 'Off'))
            ref = doc.make_indirect(widget)
            annots = page.get('/Annots')
            if annots is None:
                page['/Annots'] = pikepdf.Array([ref])
            else:
                page['/Annots'] = pikepdf.Array(list(annots) + [ref])
            buf = io.BytesIO()
            doc.save(buf)
            return buf.getvalue()
    except Exception as e:
        logger.warning(f'[form_utils] add_field_to_page_pdf: {e}')
        return pdf_bytes


def _button_on_state(field):
    """The export ("on") state Name for a checkbox/radio, derived from its first
    option when present, else 'Yes'."""
    opts = field.get('options') or []
    if field.get('type') == 'radio' and opts:
        return _sanitize_state_name(opts[0])
    return 'Yes'


def _button_is_on(field, on_state):
    """Whether a checkbox/radio field is currently in its 'on' state."""
    val = field.get('value')
    if val in (True, 'on', 'On', 'yes', 'Yes'):
        return True
    return _sanitize_state_name(val) == on_state if val else False


def _build_widget(field):
    """Build a pikepdf Dictionary for a form field dict."""
    import pikepdf
    ftype = field['type']
    name  = field.get('name') or f"Field_{field.get('id', '')[:8]}"
    x1, y1, x2, y2 = (float(field['x1']), float(field['y1']),
                       float(field['x2']), float(field['y2']))
    fs = int(field.get('font_size', 10))
    da = f'/Helv {fs} Tf 0 g'

    w = pikepdf.Dictionary(
        Type=pikepdf.Name('/Annot'),
        Subtype=pikepdf.Name('/Widget'),
        T=pikepdf.String(name),
        Rect=pikepdf.Array([x1, y1, x2, y2]),
        BS=pikepdf.Dictionary(W=1, S=pikepdf.Name('/S')),
    )
    if field.get('label'):
        w['/TU'] = pikepdf.String(field['label'])

    if ftype in ('text', 'multiline'):
        w['/FT'] = pikepdf.Name('/Tx')
        w['/Ff'] = 4096 if ftype == 'multiline' else 0
        w['/V']  = pikepdf.String(field.get('value', ''))
        w['/DV'] = pikepdf.String('')
        w['/DA'] = pikepdf.String(da)

    elif ftype == 'checkbox':
        w['/FT'] = pikepdf.Name('/Btn')
        w['/Ff'] = 0
        is_on = field.get('value') in ('on', 'Yes', True)
        v = pikepdf.Name('/Yes' if is_on else '/Off')
        w['/V']  = v
        w['/AS'] = v

    elif ftype == 'radio':
        w['/FT'] = pikepdf.Name('/Btn')
        w['/Ff'] = 32768
        w['/V']  = pikepdf.Name('/Off')
        w['/AS'] = pikepdf.Name('/Off')

    elif ftype in ('dropdown', 'listbox'):
        w['/FT'] = pikepdf.Name('/Ch')
        w['/Ff'] = 131072 if ftype == 'dropdown' else 0
        opts = field.get('options', [])
        w['/Opt'] = pikepdf.Array([pikepdf.String(o) for o in opts])
        w['/V']   = pikepdf.String(field.get('value', ''))
        w['/DA']  = pikepdf.String(da)

    return w


def delete_field_from_page_pdf(pdf_bytes, field_name):
    """Remove a Widget annotation by /T name from a single-page PDF."""
    try:
        import pikepdf
        with pikepdf.open(io.BytesIO(pdf_bytes)) as doc:
            if not doc.pages:
                return pdf_bytes
            page = doc.pages[0]
            annots = page.get('/Annots')
            if not annots:
                return pdf_bytes
            kept = []
            for a in annots:
                try:
                    if (str(a.get('/Subtype', '')) == '/Widget' and
                            str(a.get('/T', '')) == field_name):
                        continue
                    kept.append(a)
                except Exception:
                    kept.append(a)
            page['/Annots'] = pikepdf.Array(kept)
            buf = io.BytesIO()
            doc.save(buf)
            return buf.getvalue()
    except Exception as e:
        logger.warning(f'[form_utils] delete_field_from_page_pdf: {e}')
        return pdf_bytes


# ── Save-path post-processing ─────────────────────────────────────────────────

def build_acroform_on_save(merged_pdf_bytes, session_pages):
    """Apply field values and build a proper /AcroForm catalog in a merged PDF.

    Widget annotations already survive pypdf's add_page via the page /Annots.
    This function:
      1. Finds every /Widget across all pages in the merged PDF.
      2. Writes current field values from session memory.
      3. Inserts the /AcroForm root dict so PDF viewers render interactive fields.

    Returns updated bytes, or the original if no fields exist / on error.
    """
    name_to_field = {}
    for p in session_pages:
        for f in p.get('form_fields', []):
            if f.get('name'):
                name_to_field[f['name']] = f

    if not name_to_field:
        return merged_pdf_bytes

    try:
        import pikepdf
        with pikepdf.open(io.BytesIO(merged_pdf_bytes)) as doc:
            # Collect every widget, grouping by /T name so that radio widgets
            # sharing a name can be wired into ONE mutually-exclusive field.
            by_name = {}          # name -> [widget, ...]
            unnamed = []          # widgets with no /T (or unmanaged) — kept as-is
            for page in doc.pages:
                annots = page.get('/Annots')
                if not annots:
                    continue
                for a in annots:
                    try:
                        if str(a.get('/Subtype', '')) != '/Widget':
                            continue
                        t = a.get('/T')
                        if t is None:
                            unnamed.append(a)
                        else:
                            by_name.setdefault(str(t), []).append(a)
                    except Exception:
                        continue

            fields = list(unnamed)
            for name, widgets in by_name.items():
                field = name_to_field.get(name)
                ftype = (field or {}).get('type', '')
                try:
                    if ftype == 'radio' and len(widgets) > 1:
                        fields.append(_build_radio_group(pikepdf, doc, name,
                                                         widgets, field))
                    elif ftype in ('checkbox', 'radio'):
                        w = widgets[0]
                        on_state = _button_on_state(field or {})
                        is_on = _button_is_on(field or {}, on_state)
                        _attach_button_appearance(pikepdf, doc, w, ftype,
                                                  on_state, is_on)
                        w['/V'] = pikepdf.Name('/' + (on_state if is_on else 'Off'))
                        fields.extend(widgets)
                    else:
                        if field:
                            for w in widgets:
                                _apply_value(w, field)
                        fields.extend(widgets)
                except Exception as fe:
                    logger.warning(f'[form_utils] field {name!r} on-save: {fe}')
                    fields.extend(widgets)

            if fields:
                helv = doc.make_indirect(pikepdf.Dictionary(
                    Type=pikepdf.Name('/Font'),
                    Subtype=pikepdf.Name('/Type1'),
                    BaseFont=pikepdf.Name('/Helvetica'),
                    Encoding=pikepdf.Name('/WinAnsiEncoding'),
                ))
                doc.Root['/AcroForm'] = doc.make_indirect(pikepdf.Dictionary(
                    Fields=pikepdf.Array(fields),
                    DR=pikepdf.Dictionary(Font=pikepdf.Dictionary(Helv=helv)),
                    DA=pikepdf.String('/Helv 10 Tf 0 g'),
                    NeedAppearances=True,
                ))

            buf = io.BytesIO()
            doc.save(buf)
            return buf.getvalue()
    except Exception as e:
        logger.warning(f'[form_utils] build_acroform_on_save: {e}')
        return merged_pdf_bytes


def _build_radio_group(pikepdf, doc, name, widgets, field):
    """Wire several like-named radio widgets into one mutually-exclusive field.

    Standard PDF radio groups are a single /Btn field (the parent) with each
    button as a /Kids widget carrying a distinct on-state.  Previously each
    widget was an independent top-level field, so selecting one never cleared the
    others.  Here we build the parent, give each kid its own export state (from
    the field's options list, else Choice0/1/…), attach on/off appearances, and
    select the current value.  Returns the indirect parent-field reference to add
    to /AcroForm /Fields."""
    field = field or {}
    options = field.get('options') or []
    selected = _sanitize_state_name(field.get('value')) if field.get('value') else None

    parent = pikepdf.Dictionary(
        FT=pikepdf.Name('/Btn'),
        T=pikepdf.String(name),
        Ff=pikepdf.Integer(1 << 15),          # Radio
    )
    parent_ref = doc.make_indirect(parent)

    kids = []
    chosen = '/Off'
    for i, w in enumerate(widgets):
        on_state = (_sanitize_state_name(options[i]) if i < len(options)
                    else f'Choice{i}')
        is_on = (selected == on_state)
        if is_on:
            chosen = '/' + on_state
        _attach_button_appearance(pikepdf, doc, w, 'radio', on_state, is_on)
        # The name/type live on the parent now; the kid inherits them.
        for k in ('/T', '/FT', '/Ff', '/V'):
            if k in w:
                del w[k]
        w['/Parent'] = parent_ref
        kids.append(w)

    parent['/Kids'] = pikepdf.Array(kids)
    parent['/V']    = pikepdf.Name(chosen)
    parent['/DV']   = pikepdf.Name(chosen)
    return parent_ref


def _apply_value(widget, field):
    """Write the current session value into a pikepdf widget dict."""
    import pikepdf
    ftype = field.get('type', 'text')
    value = field.get('value', '')
    if ftype == 'checkbox':
        is_on = value in ('on', 'Yes', 'yes', True)
        v = pikepdf.Name('/Yes' if is_on else '/Off')
        widget['/V']  = v
        widget['/AS'] = v
    elif ftype == 'radio':
        if value and value not in ('off', 'Off', ''):
            widget['/V']  = pikepdf.Name('/' + str(value))
            widget['/AS'] = pikepdf.Name('/' + str(value))
    else:
        widget['/V'] = pikepdf.String(str(value))
