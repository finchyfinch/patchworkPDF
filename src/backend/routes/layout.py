"""
layout.py — Layout-panel API routes for Patchwork PDF.

These are the routes that exist specifically to serve the Layout view
(#panel-layout in viewer.html / layout.js): reorder, delete, insert blank page,
and export-as.  Everything they build on (sessions, thumbnails, blank-page
bytes, save/merge) lives in pdf_core.

Registered into the Flask app via register_layout_routes(), called from
entry.py.  Mirrors register_viewer_routes / register_editor_routes.
"""

import io
import os
import re
import time
import uuid
import logging

from flask import request, jsonify, send_file

from pdf.pdf_core import (_create_blank_page_bytes, render_page_thumbnail,
                      set_progress, clear_progress, get_session_lock, _fs_path)
from bootstrap import recovery as _recovery

logger = logging.getLogger(__name__)


def _median(values):
    """Return the median of a non-empty numeric list."""
    s = sorted(values)
    n = len(s)
    if n == 0:
        return 0
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def _clean_font_name(raw):
    """Turn a PDF /BaseFont name into a Word-friendly font family.

    Strips the subset prefix (``BCDEEE+Aptos`` → ``Aptos``) and any trailing
    style descriptor (``Arial-BoldMT`` → ``Arial``, ``Calibri,Bold`` → ``Calibri``)
    so bold/italic are carried by the run's own flags, not doubled by a bold
    font family.  Returns '' when nothing usable remains."""
    if not raw:
        return ''
    name = raw.strip()
    m = re.match(r'^[A-Z]{6}\+(.*)$', name)   # subset tag, e.g. BCDEEE+
    if m:
        name = m.group(1)
    name = re.split(r'[-,]', name, 1)[0]      # drop "-BoldMT", ",Bold", …
    name = re.sub(r'(PSMT|PS|MT)$', '', name) # PostScript name suffixes
    # Split CamelCase style words a subset may leave joined (AptosBold → Aptos).
    name = re.sub(r'(Bold|Italic|Oblique|Regular|Light|Medium|SemiBold|Black|Heavy)+$',
                  '', name)
    return name.strip()


def _extract_page_lines(page):
    """Extract a native PDF page's text layer as a list of styled lines.

    Returns a list of line dicts ordered top→bottom, each:
        {'text', 'size', 'bold', 'italic', 'left', 'right', 'top', 'bottom'}
    with positions in PDF points (origin bottom-left), or ``None`` when the
    page has no usable text layer (e.g. a scanned/image-only page) so the
    caller can fall back to embedding a page image.

    Lines are segmented using pdfium's OWN text stream: the CR/LF characters it
    inserts at line boundaries, in reading order, with spaces already placed
    correctly.  We deliberately do NOT re-derive lines from glyph-box geometry —
    that split ascenders/descenders and trailing commas onto phantom lines and
    mis-guessed word spacing (producing gibberish like "A i t d if i d" and
    "V1 1 1 1" in the Word export).  The glyph boxes are used only to position
    and size each line, never to order its text or decide its spaces.
    """
    import ctypes
    import pypdfium2.raw as pdfium_c

    # PDF font descriptor flag bits (FPDFText_GetFontInfo)
    FLAG_ITALIC     = 1 << 6    # 0x000040
    FLAG_FORCE_BOLD = 1 << 18   # 0x040000

    textpage = page.get_textpage()

    def _char_style(i):
        """Return (is_bold, is_italic, font_family) for char index i, using font
        weight, descriptor flags and the font name (base-14 fonts report
        weight 0)."""
        bold = italic = False
        font = ''
        try:
            if int(pdfium_c.FPDFText_GetFontWeight(textpage.raw, i)) >= 600:
                bold = True
        except Exception:
            pass
        try:
            flags = ctypes.c_int(0)
            namebuf = ctypes.create_string_buffer(128)
            nlen = pdfium_c.FPDFText_GetFontInfo(
                textpage.raw, i, namebuf, 128, ctypes.byref(flags))
            raw_name = namebuf.raw[:max(nlen - 1, 0)].split(b'\x00')[0].decode(
                'latin-1', 'ignore')
            name = raw_name.lower()
            font = _clean_font_name(raw_name)
            if flags.value & FLAG_FORCE_BOLD or 'bold' in name or 'black' in name or 'heavy' in name:
                bold = True
            if flags.value & FLAG_ITALIC or 'italic' in name or 'oblique' in name:
                italic = True
        except Exception:
            pass
        return bold, italic, font

    def _char_color(i):
        """Return the glyph fill colour of char index i as '#rrggbb' (default
        black; fully-transparent fills are ignored)."""
        try:
            r = ctypes.c_uint(0); g = ctypes.c_uint(0)
            b = ctypes.c_uint(0); a = ctypes.c_uint(0)
            if pdfium_c.FPDFText_GetFillColor(
                    textpage.raw, i, ctypes.byref(r), ctypes.byref(g),
                    ctypes.byref(b), ctypes.byref(a)) and a.value != 0:
                return '#%02x%02x%02x' % (r.value, g.value, b.value)
        except Exception:
            pass
        return '#000000'

    lines      = []
    cur        = []    # chars accumulated for the line currently being built
    real_count = 0     # visible (non-whitespace) chars on the whole page

    def _flush():
        """Finalise the current run of chars into one line dict (or drop it if
        it is blank).  The line's text is split into style-runs — maximal spans
        with the same colour / weight / italic / font / size — so a colour or
        weight change mid-line (e.g. one red word) survives the Word export."""
        if not cur:
            return
        text = ''.join(c['c'] for c in cur).strip()
        if text:
            boxed = [c for c in cur if c['box']]        # chars with a real glyph box
            vis   = [c for c in cur if c['c'].strip()]  # visible (non-space) chars
            sizes = [c['size'] for c in vis if c['size']]
            nbold = sum(1 for c in vis if c['bold'])
            nital = sum(1 for c in vis if c['italic'])
            if boxed:
                # ── Build style-runs ──────────────────────────────────────────
                # A visible char opens/continues a run keyed by its style; a
                # whitespace char just extends the current run (its colour is
                # invisible, so it must never split "red red" into three runs).
                def _key(c):
                    return (c['bold'], c['italic'], c['color'], c['font'],
                            round(c['size'] or 0, 1))
                runs = []
                for c in cur:
                    if not c['c'].strip():
                        if runs:
                            runs[-1]['text'] += c['c']
                        continue
                    k = _key(c)
                    if runs and runs[-1]['_key'] == k:
                        runs[-1]['text'] += c['c']
                    else:
                        runs.append({'_key': k, 'text': c['c'],
                                     'bold': c['bold'], 'italic': c['italic'],
                                     'color': c['color'], 'font': c['font'],
                                     'size': c['size']})
                if runs:
                    runs[0]['text']  = runs[0]['text'].lstrip()
                    runs[-1]['text'] = runs[-1]['text'].rstrip()
                runs = [{k: v for k, v in r.items() if k != '_key'}
                        for r in runs if r['text']]

                # Dominant colour among visible chars (for any single-run reader).
                col_counts = {}
                for c in vis:
                    col_counts[c['color']] = col_counts.get(c['color'], 0) + 1
                dom_color = (max(col_counts, key=col_counts.get)
                             if col_counts else '#000000')

                lines.append({
                    'text':   text,
                    'runs':   runs,
                    'size':   _median(sizes) if sizes else 0,
                    'bold':   len(vis) > 0 and nbold > len(vis) / 2.0,
                    'italic': len(vis) > 0 and nital > len(vis) / 2.0,
                    'color':  dom_color,
                    'left':   min(c['l'] for c in boxed),
                    'right':  max(c['r'] for c in boxed),
                    'top':    max(c['t'] for c in boxed),
                    'bottom': min(c['b'] for c in boxed),
                })
        cur.clear()

    try:
        n    = textpage.count_chars()
        full = textpage.get_text_range() if n > 0 else ''
        # count_chars() == len(full): char index i maps 1:1 to full[i], including
        # the CR/LF pdfium inserts at line breaks and every real space glyph.
        for i in range(n):
            ch = full[i] if i < len(full) else ''
            if ch in ('\r', '\n'):
                _flush()                    # pdfium line break → end this line
                continue
            if ch.strip():
                real_count += 1
            try:
                left, bottom, right, top = textpage.get_charbox(i)
                box = (right > left and top > bottom)   # real, non-degenerate box
            except Exception:
                left = bottom = right = top = 0.0
                box = False
            try:
                size = float(pdfium_c.FPDFText_GetFontSize(textpage.raw, i))
            except Exception:
                size = 0.0
            bold, italic, font = _char_style(i)
            color = _char_color(i)
            cur.append({'c': ch, 'l': left, 'r': right, 't': top, 'b': bottom,
                        'box': box, 'size': size, 'bold': bold, 'italic': italic,
                        'color': color, 'font': font})
        _flush()
    finally:
        try:
            textpage.close()
        except Exception:
            pass

    # Require a minimum amount of real (non-space) text to call this "native".
    if real_count < 3:
        return None
    return lines or None


def _extract_page_images(page):
    """Extract embedded raster images from a native PDF page.

    Returns a list of image block dicts ordered by document order:
        {'kind': 'image', 'pil': <PIL.Image>, 'left', 'right', 'top', 'bottom'}
    with positions in PDF points (origin bottom-left).  Returns [] when the
    page has no extractable images.
    """
    import pypdfium2.raw as pdfium_c

    out = []
    try:
        objs = list(page.get_objects())
    except Exception:
        return out

    for obj in objs:
        try:
            if obj.type != pdfium_c.FPDF_PAGEOBJ_IMAGE:
                continue
            left, bottom, right, top = obj.get_bounds()
            if (right - left) <= 1 or (top - bottom) <= 1:
                continue
            pil = None
            # render=False gives the raw embedded bitmap; fall back to a
            # composited render for images with soft masks / unusual filters.
            for render in (False, True):
                try:
                    pil = obj.get_bitmap(render=render).to_pil()
                    if pil is not None:
                        break
                except Exception:
                    pil = None
            if pil is None:
                continue
            if pil.mode not in ('RGB', 'RGBA', 'L'):
                pil = pil.convert('RGB')
            out.append({'kind': 'image', 'pil': pil,
                        'left': left, 'right': right,
                        'top': top, 'bottom': bottom})
        except Exception:
            continue
    return out


def _ocr_page_lines(pdf_bytes):
    """Build editable text lines for a scanned/image-only page by running OCR.

    Returns the same line-dict shape as _extract_page_lines (text / size /
    bold / italic / left / top / bottom, positions in PDF points), or ``None``
    when OCR is unavailable or produces nothing.
    """
    try:
        from pdf import ocr_utils
        if not ocr_utils.is_ocr_available():
            return None
        words = ocr_utils.ocr_page_words(pdf_bytes)
    except Exception:
        return None
    if not words:
        return None

    toks = [{'text': w['text'], 'l': w['x'], 'r': w['x'] + w['w'],
             'b': w['y'], 't': w['y'] + w['h']}
            for w in words if w.get('w', 0) > 0 and w.get('h', 0) > 0 and w.get('text')]
    if not toks:
        return None

    # Group OCR words into lines by vertical position (same approach as the
    # native char grouping, one level up at word granularity).
    toks.sort(key=lambda c: -((c['t'] + c['b']) / 2.0))
    raw_lines = []
    for c in toks:
        center = (c['t'] + c['b']) / 2.0
        h = (c['t'] - c['b']) or 8.0
        if raw_lines:
            last = raw_lines[-1]
            if abs(last['_center'] - center) <= 0.5 * max(h, last['_h']):
                last['toks'].append(c)
                continue
        raw_lines.append({'_center': center, '_h': h, 'toks': [c]})

    lines = []
    for rl in raw_lines:
        ts = sorted(rl['toks'], key=lambda c: c['l'])
        text = ' '.join(t['text'] for t in ts).strip()
        if not text:
            continue
        h = _median([t['t'] - t['b'] for t in ts]) or rl['_h']
        size = max(6.0, min(round(h * 0.85, 1), 48.0))   # word-box height → pt
        lines.append({
            'text': text,
            'size': size,
            'bold': False,
            'italic': False,
            'left': min(t['l'] for t in ts),
            'right': max(t['r'] for t in ts),
            'top': max(t['t'] for t in ts),
            'bottom': min(t['b'] for t in ts),
        })
    return lines or None


# ── Table extraction (Export to Excel) ───────────────────────────────────────
# The Excel export looks for RULED tables and writes each one to its own sheet
# tab.  Detection is delegated to pdfplumber's line-based table finder (the same
# geometry-of-ruling-lines approach as the viewer's "open selection in Excel"),
# so surrounding prose is excluded and multiple tables on a page are separated.
#
#   • Native text page          → cells are read straight from the text layer.
#   • Image/scanned page that
#     still has ruled borders    → the grid is taken from the ruling lines and
#                                   each cell is filled from OCR words by
#                                   position (the text isn't in a text layer).
#
# A page with no ruled table yields nothing; when the whole document yields
# nothing the caller tells the user no tables were found (rather than exporting
# a useless page image, which the old behaviour did).

def _ocr_words_top_origin(pdf_bytes, page_h):
    """OCR page 0 and return word dicts in pdfplumber's TOP-origin point space:
    {cx, cy, left, top, h, text}.  ocr_page_words returns y-up (origin bottom),
    so y is flipped to match pdfplumber cell bboxes.  [] when OCR unavailable."""
    try:
        from pdf import ocr_utils
        if not ocr_utils.is_ocr_available():
            return []
        words = ocr_utils.ocr_page_words(pdf_bytes)
    except Exception:
        logger.exception('[export_tables] OCR failed')
        return []
    out = []
    for w in words:
        txt = (w.get('text') or '').strip()
        if not txt or w.get('w', 0) <= 0 or w.get('h', 0) <= 0:
            continue
        top = page_h - (w['y'] + w['h'])     # flip: PDF y-up bottom → top-origin
        out.append({'cx': w['x'] + w['w'] / 2.0,
                    'cy': top + w['h'] / 2.0,
                    'left': w['x'], 'top': top, 'h': w['h'], 'text': txt})
    return out


def _words_to_cell_text(words):
    """Join OCR words that fall in one cell into reading-order text: cluster into
    lines by vertical position, sort each line left→right, join lines with a
    space (Excel wraps the cell)."""
    if not words:
        return ''
    ws = sorted(words, key=lambda w: w['cy'])
    med_h = sorted(w['h'] for w in ws)[len(ws) // 2] or 8.0
    lines, cur, base = [], [], ws[0]['cy']
    for w in ws:
        if w['cy'] - base > 0.6 * med_h and cur:
            lines.append(cur); cur = []
        cur.append(w); base = w['cy']
    if cur:
        lines.append(cur)
    parts = [' '.join(w['text'] for w in sorted(ln, key=lambda w: w['left']))
             for ln in lines]
    return ' '.join(p for p in parts if p).strip()


def _fill_table_from_words(table, words):
    """Fill a pdfplumber table's ruled cell grid from positioned OCR *words*."""
    rows = []
    for row in table.rows:
        cells = []
        for cell in row.cells:
            if cell is None:
                cells.append('')
                continue
            x0, top, x1, bottom = cell
            inside = [w for w in words
                      if x0 <= w['cx'] <= x1 and top <= w['cy'] <= bottom]
            cells.append(_words_to_cell_text(inside))
        rows.append(cells)
    return rows


def _clean_table_rows(rows):
    """Normalise extracted rows: cells → stripped strings, ragged rows padded to
    the ruled grid's column count, fully-empty rows dropped.  The grid width is
    preserved (a genuinely ruled 2-column table stays 2 columns even when one
    column is empty).  Returns [] if what's left isn't table-shaped (needs ≥2
    rows, ≥2 columns and at least a few non-empty cells)."""
    if not rows:
        return []
    ncols = max((len(r) for r in rows), default=0)
    if ncols < 2:
        return []
    norm = []
    for r in rows:
        cells = [('' if c is None else str(c).replace('\r', ' ').strip()) for c in r]
        cells += [''] * (ncols - len(cells))     # pad ragged rows to grid width
        if any(cells):
            norm.append(cells)
    if len(norm) < 2:
        return []
    nonempty = sum(1 for r in norm for c in r if c)
    if nonempty < 3:
        return []
    return norm


def _extract_page_tables(pdf_bytes):
    """Return a list of ruled tables on page 0, each as a list of row-lists of
    cell strings.  [] when pdfplumber is unavailable or no ruled table is found.

    Uses pdfplumber's default line-based strategy so only genuinely ruled tables
    match (prose and bulleted lists have no ruling lines and are ignored)."""
    try:
        import pdfplumber
    except ImportError:
        logger.warning('[export_tables] pdfplumber not installed')
        return []
    out = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            page = pdf.pages[0]
            found = page.find_tables()
            if not found:
                return []
            has_text = len(page.chars) > 0
            words = None
            for t in found:
                if has_text:
                    raw = t.extract()
                else:
                    if words is None:
                        words = _ocr_words_top_origin(pdf_bytes, float(page.height))
                    raw = _fill_table_from_words(t, words)
                rows = _clean_table_rows(raw)
                if rows:
                    out.append(rows)
    except Exception:
        logger.exception('[export_tables] extraction failed')
        return []
    return out


_ILLEGAL_SHEET = re.compile(r'[\[\]:*?/\\]')

def _unique_sheet_name(base, used):
    """Excel sheet-name-safe (≤31 chars, no []:*?/\\), unique within *used*."""
    name = _ILLEGAL_SHEET.sub(' ', base).strip() or 'Table'
    name = name[:31]
    if name not in used:
        used.add(name); return name
    i = 2
    while True:
        suffix = ' (%d)' % i
        cand = name[:31 - len(suffix)] + suffix
        if cand not in used:
            used.add(cand); return cand
        i += 1


def _build_tables_workbook(session, progress_id):
    """Scan every page for ruled tables and build an .xlsx with one sheet per
    table.  Returns (xlsx_bytes, table_count); (b'', 0) when nothing is found."""
    from openpyxl import Workbook
    total = len(session['pages'])
    collected = []                       # (page_no, rows)
    for pidx, page_data in enumerate(session['pages']):
        set_progress(progress_id, pidx + 1, total,
                     f"Scanning page {pidx + 1} of {total} for tables…")
        try:
            tables = _extract_page_tables(page_data['pdf_bytes'])
        except Exception:
            logger.exception('[export_tables] page %d failed', pidx + 1)
            tables = []
        for rows in tables:
            collected.append((pidx + 1, rows))

    if not collected:
        return b'', 0

    set_progress(progress_id, total, total, 'Building spreadsheet…')
    wb = Workbook()
    wb.remove(wb.active)                  # drop the default empty sheet
    used = set()
    multipage = total > 1
    for k, (page_no, rows) in enumerate(collected, 1):
        base = ('Table %d (p%d)' % (k, page_no)) if multipage else ('Table %d' % k)
        ws = wb.create_sheet(title=_unique_sheet_name(base, used))
        for r in rows:
            ws.append([('' if c is None else str(c)) for c in r])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), len(collected)


def _export_excel_tables(session, session_id, data, progress_id):
    """Excel export handler.  Three modes on POST /api/export_doc (format=excel):

        probe=true        → detect + build, cache the workbook on the session,
                            return {count}; no file written.
        save_path=<path>  → write the (cached, else freshly built) workbook to
                            that path; return {success, saved_to}.
        neither           → return the workbook as a download (browser flow).

    When no table is found in the document, returns {no_tables: true} (HTTP 200)
    so the caller tells the user instead of producing a useless file."""
    probe     = bool(data.get('probe', False))
    save_path = (data.get('save_path') or '').strip()

    # The save step reuses the workbook the immediately-preceding probe built, so
    # detection (the expensive part — pdfplumber + OCR) runs only once.
    cache = session.get('_xlsx_cache')
    if save_path and cache and (time.time() - cache[0] < 300):
        xlsx_bytes, count = cache[1], cache[2]
    else:
        xlsx_bytes, count = _build_tables_workbook(session, progress_id)
        session['_xlsx_cache'] = (time.time(), xlsx_bytes, count)

    clear_progress(progress_id)

    if count == 0:
        session.pop('_xlsx_cache', None)
        return jsonify({'no_tables': True, 'error_key': 'err.no_tables_found'}), 200

    if probe:
        return jsonify({'count': count})

    mime = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    if save_path:
        try:
            os.makedirs(_fs_path(os.path.dirname(os.path.abspath(save_path))), exist_ok=True)
            with open(_fs_path(save_path), 'wb') as f:
                f.write(xlsx_bytes)
            session.pop('_xlsx_cache', None)
            return jsonify({'success': True, 'saved_to': save_path})
        except Exception:
            logger.exception('[export_tables] save_path write failed')
            # fall through to a download

    session.pop('_xlsx_cache', None)
    return send_file(io.BytesIO(xlsx_bytes), mimetype=mime,
                     as_attachment=True, download_name='tables.xlsx')


def _float_image_behind_text(run, x_emu, y_emu):
    """Convert the inline picture in *run* into a floating image anchored at the
    absolute page offset (x_emu, y_emu) and placed *behind* the text, so text
    flows over the top of it (background images / watermarks)."""
    import copy
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    drawing = run._r.find(qn('w:drawing'))
    inline = drawing.find(qn('wp:inline'))
    extent = inline.find(qn('wp:extent'))
    cx, cy = extent.get('cx'), extent.get('cy')
    docPr = inline.find(qn('wp:docPr'))
    graphic = inline.find(qn('a:graphic'))

    anchor = OxmlElement('wp:anchor')
    for k, v in (('behindDoc', '1'), ('distT', '0'), ('distB', '0'),
                 ('distL', '0'), ('distR', '0'), ('simplePos', '0'),
                 ('locked', '0'), ('layoutInCell', '1'),
                 ('allowOverlap', '1'), ('relativeHeight', '0')):
        anchor.set(k, v)

    sp = OxmlElement('wp:simplePos')
    sp.set('x', '0')
    sp.set('y', '0')
    anchor.append(sp)

    posH = OxmlElement('wp:positionH')
    posH.set('relativeFrom', 'page')
    offH = OxmlElement('wp:posOffset')
    offH.text = str(int(x_emu))
    posH.append(offH)
    anchor.append(posH)

    posV = OxmlElement('wp:positionV')
    posV.set('relativeFrom', 'page')
    offV = OxmlElement('wp:posOffset')
    offV.text = str(int(y_emu))
    posV.append(offV)
    anchor.append(posV)

    ext2 = OxmlElement('wp:extent')
    ext2.set('cx', cx)
    ext2.set('cy', cy)
    anchor.append(ext2)

    eff = OxmlElement('wp:effectExtent')
    for a in ('l', 't', 'r', 'b'):
        eff.set(a, '0')
    anchor.append(eff)

    anchor.append(OxmlElement('wp:wrapNone'))
    anchor.append(copy.deepcopy(docPr))
    anchor.append(OxmlElement('wp:cNvGraphicFramePr'))
    anchor.append(copy.deepcopy(graphic))

    drawing.remove(inline)
    drawing.append(anchor)


def _img_behind_text(img, text_boxes):
    """True when image block *img* overlaps text enough to be a background
    image (so it should sit behind the text rather than flow inline)."""
    il, ib, ir, it = img['left'], img['bottom'], img['right'], img['top']
    for (tl, tb, tr, tt) in text_boxes:
        ox = min(ir, tr) - max(il, tl)
        oy = min(it, tt) - max(ib, tb)
        if ox > 0 and oy > 0:
            tarea = max(1.0, (tr - tl) * (tt - tb))
            if (ox * oy) / tarea > 0.3:
                return True
    return False


def _add_content_page(doc, blocks, page_w_pts, page_h_pts, margin_pts, Pt, Emu):
    """Write a page's content blocks (text lines and/or images) into the docx
    document's current section as editable content.  Text and standalone images
    flow top→bottom (preserving font size, bold/italic, left indent and image
    size); images that overlap text are anchored behind the text at their
    absolute page position so the text reads over the top of them."""
    PT_TO_EMU = 914400 / 72
    usable_pts = page_w_pts - 2 * margin_pts

    text_boxes = [(b['left'], b['bottom'], b.get('right', b['left']), b['top'])
                  for b in blocks if b['kind'] == 'text']
    floating = [b for b in blocks
                if b['kind'] == 'image' and _img_behind_text(b, text_boxes)]
    flow = [b for b in blocks if b not in floating]
    flow.sort(key=lambda b: -b['top'])

    def _img_buf(img):
        ibuf = io.BytesIO()
        if img.mode == 'RGBA':
            img.save(ibuf, 'PNG')
        else:
            img.convert('RGB').save(ibuf, 'JPEG', quality=85)
        ibuf.seek(0)
        return ibuf

    prev_bottom = None
    first_para = None
    for b in flow:
        size = b.get('size') or 11
        # Blank paragraph where there is a noticeable vertical gap
        if prev_bottom is not None:
            gap = prev_bottom - b['top']
            if gap > 1.4 * size:
                doc.add_paragraph()
        prev_bottom = b['bottom']

        para = doc.add_paragraph()
        if first_para is None:
            first_para = para
        pf = para.paragraph_format
        pf.space_before = Pt(0)
        pf.space_after = Pt(0)
        indent_pts = b['left'] - margin_pts
        if indent_pts > 2:
            pf.left_indent = Emu(int(indent_pts * PT_TO_EMU))

        if b['kind'] == 'image':
            w_pts = min(b['right'] - b['left'], usable_pts)
            run = para.add_run()
            run.add_picture(_img_buf(b['pil']), width=Emu(int(w_pts * PT_TO_EMU)))
        else:
            from docx.shared import RGBColor

            def _style_run(run, *, text_size, bold, italic, color, font):
                if text_size and text_size > 0:
                    run.font.size = Pt(round(text_size, 1))
                if bold:
                    run.bold = True
                if italic:
                    run.italic = True
                if font:
                    run.font.name = font
                # Only set an explicit colour when it isn't the default black —
                # leaving black unset keeps the document's default/theme colour.
                if color and color.lower() not in ('#000000', '#000'):
                    try:
                        run.font.color.rgb = RGBColor(
                            int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16))
                    except Exception:
                        pass

            # Native lines carry style-runs (per-run colour/weight/font); OCR
            # lines have none, so fall back to one run for the whole line.
            runs = b.get('runs')
            if runs:
                for rn in runs:
                    if not rn.get('text'):
                        continue
                    _style_run(para.add_run(rn['text']),
                               text_size=rn.get('size') or b.get('size'),
                               bold=rn.get('bold'), italic=rn.get('italic'),
                               color=rn.get('color'), font=rn.get('font'))
            else:
                _style_run(para.add_run(b['text']),
                           text_size=b.get('size'), bold=b.get('bold'),
                           italic=b.get('italic'), color=b.get('color'),
                           font=b.get('font'))

    # Background images: anchored behind the text at their absolute position.
    if floating:
        host = first_para if first_para is not None else doc.add_paragraph()
        for b in floating:
            run = host.add_run()
            w_emu = int((b['right'] - b['left']) * PT_TO_EMU)
            h_emu = int((b['top'] - b['bottom']) * PT_TO_EMU)
            run.add_picture(_img_buf(b['pil']), width=Emu(w_emu), height=Emu(h_emu))
            x_emu = int(b['left'] * PT_TO_EMU)
            y_emu = int((page_h_pts - b['top']) * PT_TO_EMU)
            _float_image_behind_text(run, x_emu, y_emu)


def register_layout_routes(app, sessions, inject_token=None):
    """Register the Layout-panel routes.  Called from entry.py after the Flask
    app is created."""

    @app.route('/api/reorder', methods=['POST'])
    def reorder_pages():
        data = request.json
        session_id = data['session_id']
        order = data['order']
        session = sessions.get(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        with get_session_lock(session):
            page_map = {p['id']: p for p in session['pages']}
            session['pages'] = [page_map[pid] for pid in order if pid in page_map]
        # The new order lives only in the manifest, so no page bytes are rewritten.
        _recovery.mark_meta_dirty(session)
        return jsonify({'success': True})

    @app.route('/api/delete', methods=['POST'])
    def delete_page():
        data = request.json
        session_id = data['session_id']
        page_id = data['page_id']
        session = sessions.get(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        with get_session_lock(session):
            session['pages'] = [p for p in session['pages'] if p['id'] != page_id]
        _recovery.mark_meta_dirty(session)     # drops the page from the manifest
        return jsonify({'success': True})

    @app.route('/api/insert_blank_page', methods=['POST'])
    def insert_blank_page():
        """Insert a blank white page before or after a reference page.

        Body: {session_id, ref_page_id, insert_before, width_pts, height_pts}
        width_pts / height_pts default to 595.28 × 841.89 (A4 portrait).
        """
        data        = request.json
        session_id  = data.get('session_id', '')
        ref_page_id = data.get('ref_page_id', '')
        before      = bool(data.get('insert_before', False))
        width_pts   = float(data.get('width_pts',  595.28))
        height_pts  = float(data.get('height_pts', 841.89))

        session = sessions.get(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404

        blank_bytes = _create_blank_page_bytes(width_pts, height_pts)
        page_id     = str(uuid.uuid4())
        thumbnail   = render_page_thumbnail(blank_bytes, 0)

        new_page = {
            'id':          page_id,
            'filename':    'blank.pdf',
            'page_num':    1,
            'total_pages': 1,
            'rotation':    0,
            'thumbnail':   thumbnail,
            'pdf_bytes':   blank_bytes,
            'form_fields': [],
            'width_pts':   round(width_pts,  2),
            'height_pts':  round(height_pts, 2),
        }

        with get_session_lock(session):
            ref_idx = next(
                (i for i, p in enumerate(session['pages']) if p['id'] == ref_page_id), -1
            )
            insert_at = ref_idx if (before and ref_idx >= 0) else (ref_idx + 1)
            if insert_at < 0:
                session['pages'].append(new_page)
            else:
                session['pages'].insert(insert_at, new_page)

        _recovery.mark_dirty(session, page_id)   # the blank page's bytes are new

        return jsonify({'success': True,
                        'page': {k: v for k, v in new_page.items() if k != 'pdf_bytes'}})

    @app.route('/api/export_doc', methods=['POST'])
    def export_doc():
        """Export all pages as a Word (.docx) or Excel (.xlsx) document.

          - Word:  one page per section (editable text/images, or a page image).
          - Excel: each RULED table found in the document is written to its own
                   sheet tab; pages without a table contribute nothing, and a
                   document with no tables reports that to the user rather than
                   exporting page images.
        """
        data       = request.json
        session_id = data.get('session_id', '')
        fmt        = data.get('format', 'word').lower()   # 'word' or 'excel'
        save_path  = data.get('save_path', '').strip()
        dpi        = int(data.get('dpi', 150))
        editable   = bool(data.get('editable', False))    # Word: extract text
        progress_id = data.get('progress_id', '')

        session = sessions.get(session_id)
        if not session or not session['pages']:
            return jsonify({'error': 'No pages in session', 'error_key': 'err.no_pages_in_session'}), 400

        if fmt == 'excel':
            return _export_excel_tables(session, session_id, data, progress_id)

        import pypdfium2 as _pdfium
        total_pages = len(session['pages'])

        # ── Process every page ────────────────────────────────────────────
        # For editable Word export we build a list of content "blocks" per
        # page: native text lines + embedded images, or OCR text for scanned
        # pages.  Pages we can't turn into editable content (and all pages when
        # the option is off) are rasterised to a full-page image instead.
        want_text   = (fmt == 'word' and editable)
        page_images = []   # PIL Image per page, or None when blocks were built
        page_sizes  = []   # (width_pts, height_pts) for each page
        page_blocks = []   # list of content blocks per page, or None
        scale = dpi / 72.0

        for pidx, page_data in enumerate(session['pages']):
            set_progress(progress_id, pidx + 1, total_pages,
                         f"{'Reading' if want_text else 'Rendering'} page "
                         f"{pidx + 1} of {total_pages}…")
            pdf_bytes = page_data['pdf_bytes']
            doc  = _pdfium.PdfDocument(pdf_bytes)
            page = doc[0]
            pw   = page.get_width()
            ph   = page.get_height()
            page_sizes.append((pw, ph))

            blocks = None
            if want_text:
                try:
                    lines = _extract_page_lines(page)
                except Exception:
                    lines = None
                if lines is not None:
                    # Native text page → editable text plus any embedded images
                    try:
                        imgs = _extract_page_images(page)
                    except Exception:
                        imgs = []
                    blocks = [dict(kind='text', **ln) for ln in lines] + imgs
                else:
                    # No text layer → try OCR (scanned / image-only page)
                    try:
                        ocr_lines = _ocr_page_lines(pdf_bytes)
                    except Exception:
                        ocr_lines = None
                    if ocr_lines:
                        blocks = [dict(kind='text', **ln) for ln in ocr_lines]
            page_blocks.append(blocks)

            if blocks is None:
                bmp = page.render(scale=scale)
                img = bmp.to_pil().convert('RGB')
                page_images.append(img)
            else:
                page_images.append(None)

        set_progress(progress_id, total_pages, total_pages,
                     f"Building {'Word document' if fmt == 'word' else 'spreadsheet'}…")
        out_buf = io.BytesIO()

        if fmt == 'word':
            from docx import Document as _DocxDoc
            from docx.shared import Pt, Emu
            from docx.enum.section import WD_ORIENT

            doc = _DocxDoc()
            # Remove the blank paragraph that python-docx creates by default
            for para in doc.paragraphs:
                p = para._element
                p.getparent().remove(p)

            for idx, (img, (pw, ph)) in enumerate(zip(page_images, page_sizes)):
                # Each page gets its own section so orientation can vary
                if idx > 0:
                    doc.add_section()
                section = doc.sections[-1]

                # Set page size and orientation to match the PDF page
                # 1 pt = 914400 / 72 EMU  (1 inch = 914400 EMU, 72 pt/inch)
                PT_TO_EMU = 914400 / 72
                margin_emu = int(0.5 * 914400)   # 0.5 inch margins
                margin_pts = 0.5 * 72
                page_w_emu = int(pw * PT_TO_EMU)
                page_h_emu = int(ph * PT_TO_EMU)

                if pw > ph:
                    section.orientation = WD_ORIENT.LANDSCAPE
                    section.page_width  = Emu(page_w_emu)
                    section.page_height = Emu(page_h_emu)
                else:
                    section.orientation = WD_ORIENT.PORTRAIT
                    section.page_width  = Emu(page_w_emu)
                    section.page_height = Emu(page_h_emu)

                section.top_margin    = Emu(margin_emu)
                section.bottom_margin = Emu(margin_emu)
                section.left_margin   = Emu(margin_emu)
                section.right_margin  = Emu(margin_emu)

                blocks = page_blocks[idx]
                if blocks:
                    # Editable content (native text + images, or OCR text)
                    _add_content_page(doc, blocks, pw, ph, margin_pts, Pt, Emu)
                    continue

                # Not convertible to editable content → embed a full-page image
                img_buf = io.BytesIO()
                img.save(img_buf, 'JPEG', quality=85)
                img_buf.seek(0)

                para = doc.add_paragraph()
                para.paragraph_format.space_before = Pt(0)
                para.paragraph_format.space_after  = Pt(0)
                run = para.add_run()
                usable_w = page_w_emu - 2 * margin_emu
                run.add_picture(img_buf, width=Emu(usable_w))

            doc.save(out_buf)
            mime = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
            dl_name = 'exported.docx'

        clear_progress(progress_id)
        out_buf.seek(0)

        if save_path:
            try:
                os.makedirs(_fs_path(os.path.dirname(os.path.abspath(save_path))), exist_ok=True)
                with open(_fs_path(save_path), 'wb') as f:
                    f.write(out_buf.getvalue())
                return jsonify({'success': True, 'saved_to': save_path})
            except Exception as _e:
                out_buf.seek(0)
                return send_file(out_buf, mimetype=mime,
                                 as_attachment=True, download_name=dl_name)

        out_buf.seek(0)
        return send_file(out_buf, mimetype=mime,
                         as_attachment=True, download_name=dl_name)
