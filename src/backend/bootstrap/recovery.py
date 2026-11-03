# ── Applied/committed-but-unsaved document recovery (disk-backed) ────────────
# Complements the editor's *un-applied* JSON-delta draft store (see entry.py
# /api/drafts/*).  This module snapshots the actual working PDF bytes that live
# in each session's per-page `pdf_bytes` — the state produced by viewer
# annotations and by editor Apply — so those changes survive a crash even though
# they were never written to a file.
#
# Design:
#   • A background daemon thread does all disk I/O, off the Flask request path.
#     Mutating routes call mark_dirty(session, page_id); the daemon debounces a
#     burst and writes only the changed pages.
#   • Storage is on disk under the app state dir (NOT browser IndexedDB), one
#     directory per document keyed by a per-session recovery id.  Full page bytes
#     mean recovery reconstructs the exact working document with no replay and no
#     dependency on the original file — it even recovers never-saved documents.
#   • Encrypted / password-protected sessions are skipped entirely, so decrypted
#     plaintext never lands on disk.
import os
import io
import json
import time
import uuid
import shutil
import hashlib
import logging
import threading

from bootstrap import single_instance as _single_instance

logger = logging.getLogger(__name__)

_DEBOUNCE_SEC    = 1.5
_STORE_CAP_BYTES = 500 * 1024 * 1024      # total recovery store budget
_MAX_AGE_SEC     = 14 * 24 * 3600         # prune abandoned docs after 14 days
_MANIFEST        = 'manifest.json'

# Session dicts that need a flush, keyed by id(session) to dedupe.  Each carries a
# '_rec_dirty' set of page ids (or None → all pages) and a '_rec_id' doc id.
_lock  = threading.Lock()
_dirty = {}
_wake  = threading.Event()
_started = False


# ── Paths ────────────────────────────────────────────────────────────────────
def _root():
    d = os.path.join(_single_instance._state_dir(), 'recovery')
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return None
    return d


def _doc_dir(rec_id):
    r = _root()
    return os.path.join(r, rec_id) if (r and rec_id) else None


def _page_file(page_id):
    return hashlib.sha1((page_id or '').encode('utf-8')).hexdigest()[:16] + '.pdf'


def _atomic_write(path, data):
    tmp = path + '.tmp'
    mode = 'wb' if isinstance(data, (bytes, bytearray)) else 'w'
    kw = {} if 'b' in mode else {'encoding': 'utf-8'}
    with open(tmp, mode, **kw) as f:
        f.write(data)
    os.replace(tmp, path)


# ── Encryption gate ──────────────────────────────────────────────────────────
def _is_encrypted(session):
    return bool(session.get('password') or session.get('needs_password'))


# ── Public API: mark / clear ─────────────────────────────────────────────────
def mark_dirty(session, page_id=None):
    """Flag a session (optionally one page) for a background recovery snapshot.
    Cheap and safe to call from any request handler.  No-op for encrypted docs
    (any stale snapshot for them is purged)."""
    if not session or not session.get('pages'):
        return
    if _is_encrypted(session):
        rid = session.get('_rec_id')
        if rid:
            _purge(rid)
        return
    if not session.get('_rec_id'):
        session['_rec_id'] = uuid.uuid4().hex
    with _lock:
        ds = session.get('_rec_dirty')
        if ds is None:
            ds = session['_rec_dirty'] = set()
        if page_id is None:
            # list() copy: pages may be mutated on other request threads.
            ds.update(p['id'] for p in list(session.get('pages') or []))
        else:
            ds.add(page_id)
        _dirty[id(session)] = session
    _wake.set()


def mark_meta_dirty(session):
    """Flag a session whose change touched no page BYTES — page order, a
    deletion or insertion, filled form values, bookmarks, metadata, the save
    password.  Enqueues the session with an EMPTY dirty-page set, so the writer
    rewrites the manifest (which carries the page order and all of that
    session-level state) and prunes files for pages that are gone, without
    rewriting a single unchanged page.

    Distinct from mark_dirty(session) with no page id, which means "every page's
    bytes changed" and rewrites the lot."""
    if not session or not session.get('pages'):
        return
    if _is_encrypted(session):
        rid = session.get('_rec_id')
        if rid:
            _purge(rid)
        return
    if not session.get('_rec_id'):
        session['_rec_id'] = uuid.uuid4().hex
    with _lock:
        if session.get('_rec_dirty') is None:
            session['_rec_dirty'] = set()
        _dirty[id(session)] = session
    _wake.set()


def clear(session):
    """Drop a session's recovery snapshot (call on successful Save).  The doc id
    is kept so later edits reuse the same directory rather than orphaning one."""
    if not session:
        return
    with _lock:
        _dirty.pop(id(session), None)
        session['_rec_dirty'] = set()
        rid = session.get('_rec_id')
    if rid:
        _purge(rid)


def has_pending(session):
    """True when a session holds working-bytes changes that are not yet written
    to a real file — a recovery snapshot is queued or exists on disk for it.

    Used to protect such sessions from in-memory LRU eviction: dropping one would
    break the user's still-open, unsaved tab (though the disk snapshot would still
    allow restore on next launch).  A saved session (recovery.clear() called)
    reports False and so is freely evictable."""
    if not session:
        return False
    if session.get('_rec_dirty'):
        return True
    with _lock:
        if id(session) in _dirty:
            return True      # queued — including a meta-only (empty-set) mark
    rid = session.get('_rec_id')
    d = _doc_dir(rid) if rid else None
    return bool(d and os.path.isdir(d))


def purge(rec_id):
    """Remove a snapshot directory by doc id (used by Discard)."""
    _purge(rec_id)


def purge_all():
    """Drop every working-bytes snapshot (used on a clean, commanded shutdown —
    the crash guard only needs to survive an ABNORMAL exit).  Also drops any
    pending in-memory dirty flags so the background writer can't re-materialise a
    snapshot after the wipe."""
    with _lock:
        _dirty.clear()
    r = _root()
    if not r or not os.path.isdir(r):
        return
    for rec_id in os.listdir(r):
        _purge(rec_id)


def _purge(rec_id):
    d = _doc_dir(rec_id)
    if d and os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)


# ── Background writer ────────────────────────────────────────────────────────
def start():
    """Launch the writer daemon and run a one-time prune of stale/oversized
    snapshots.  Idempotent."""
    global _started
    if _started:
        return
    _started = True
    try:
        _prune_old()
        _enforce_cap()
    except Exception as e:
        logger.warning(f'recovery: startup prune failed: {e}')
    t = threading.Thread(target=_run, name='recovery-writer', daemon=True)
    t.start()
    import atexit
    atexit.register(flush_all)          # capture the last, un-debounced edits on clean exit


def flush_all():
    """Synchronously flush every pending session (clean-shutdown safety net)."""
    with _lock:
        jobs = []
        for sess in _dirty.values():
            ds = sess.get('_rec_dirty')
            sess['_rec_dirty'] = set()
            jobs.append(_snapshot(sess, ds))
        _dirty.clear()
    for snap in jobs:
        if not snap:
            continue
        try:
            _flush(snap)
        except Exception as e:
            logger.warning(f'recovery: flush_all failed: {e}')


def _run():
    while True:
        _wake.wait()
        time.sleep(_DEBOUNCE_SEC)          # coalesce a burst of edits
        _wake.clear()
        with _lock:
            jobs = []
            for sess in _dirty.values():
                ds = sess.get('_rec_dirty')
                sess['_rec_dirty'] = set()
                jobs.append(_snapshot(sess, ds))
            _dirty.clear()
        for snap in jobs:
            if not snap:
                continue
            try:
                _flush(snap)
            except Exception as e:
                logger.warning(f'recovery: flush failed: {e}')
        try:
            _enforce_cap()
        except Exception as e:
            logger.warning(f'recovery: cap enforce failed: {e}')


def _snapshot(session, dirty_ids):
    """Take a consistent, cheap snapshot of a session (holding refs to the
    immutable page-byte objects) so the actual disk write can run outside the
    lock.  Returns None to skip, or a {'purge': rec_id} record for encrypted."""
    rec_id = session.get('_rec_id')
    if not rec_id:
        return None
    if _is_encrypted(session):
        return {'purge': rec_id}
    # list() copy so a concurrent page add/delete on a request thread can't break
    # iteration; page-byte objects are immutable and reassigned, so refs are safe.
    pages = list(session.get('pages') or [])
    if not pages:
        return {'purge': rec_id}
    all_dirty = dirty_ids is None
    dset = set() if all_dirty else set(dirty_ids)
    snap_pages = []
    for p in pages:
        snap_pages.append({
            'id':          p.get('id', ''),
            'filename':    p.get('filename', ''),
            'page_num':    p.get('page_num', 0),
            'total_pages': p.get('total_pages', 0),
            'rotation':    p.get('rotation', 0),
            'width_pts':   p.get('width_pts', 595.28),
            'height_pts':  p.get('height_pts', 841.89),
            'thumbnail':   p.get('thumbnail'),
            'form_fields': p.get('form_fields') or [],
            'bytes':       p.get('pdf_bytes'),
            'dirty':       all_dirty or (p.get('id') in dset),
        })
    # Not everything unsaved lives in the page bytes: filled form values, the
    # outline, and document properties are held on the session and only written
    # into the PDF at save time.  They are exactly as lost in a crash as an
    # applied edit is, so the snapshot has to carry them too.
    extra = {
        'comments':  session.get('comments'),
        'bookmarks': session.get('bookmarks'),
        'metadata':  session.get('metadata'),
    }
    for k, v in list(extra.items()):
        try:
            json.dumps(v)                   # keep only if JSON-serializable
        except (TypeError, ValueError):
            extra[k] = None
    return {
        'rec_id':      rec_id,
        'source_path': session.get('first_file_path'),
        'pages':       snap_pages,
        'comments':    extra['comments'],
        'bookmarks':   extra['bookmarks'],
        'metadata':    extra['metadata'],
    }


def _flush(snap):
    if 'purge' in snap:
        _purge(snap['purge'])
        return
    rec_id = snap['rec_id']
    d = _doc_dir(rec_id)
    if not d:
        return
    os.makedirs(d, exist_ok=True)

    valid = set()
    man_pages = []
    for p in snap['pages']:
        data = p['bytes']
        if not data:
            continue
        fname = _page_file(p['id'])
        path  = os.path.join(d, fname)
        # Write when the page changed or its file is missing (guarantees a
        # complete document even when only one page was flagged).
        if p['dirty'] or not os.path.isfile(path):
            _atomic_write(path, data)
        valid.add(fname)
        man_pages.append({
            'id':          p['id'],
            'file':        fname,
            'filename':    p['filename'],
            'page_num':    p['page_num'],
            'total_pages': p['total_pages'],
            'rotation':    p['rotation'],
            'width_pts':   p['width_pts'],
            'height_pts':  p['height_pts'],
            'thumbnail':   p['thumbnail'],
            'form_fields': p['form_fields'],
            'size':        len(data),
        })
    if not man_pages:
        _purge(rec_id)
        return

    # Prune orphaned page files (deleted/reordered-out pages), then commit the
    # manifest LAST so a half-written snapshot set is never treated as valid.
    for name in os.listdir(d):
        if name == _MANIFEST or name.endswith('.tmp'):
            continue
        if name not in valid:
            try:
                os.remove(os.path.join(d, name))
            except OSError:
                pass

    first = man_pages[0]
    manifest = {
        'rec_id':      rec_id,
        'source_path': snap['source_path'],
        'filename':    first.get('filename', ''),
        'total_pages': len(man_pages),
        'updatedAt':   int(time.time() * 1000),
        'comments':    snap['comments'],
        'bookmarks':   snap.get('bookmarks'),
        'metadata':    snap.get('metadata'),
        'pages':       man_pages,
    }
    _atomic_write(os.path.join(d, _MANIFEST), json.dumps(manifest))


# ── Read side: list / load (launch recovery) ─────────────────────────────────
def _read_manifest(rec_id):
    d = _doc_dir(rec_id)
    if not d:
        return None
    path = os.path.join(d, _MANIFEST)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def list_manifests():
    """Lightweight metadata for every stored snapshot — drives the launch prompt.
    No page bytes are read."""
    r = _root()
    out = []
    if not r or not os.path.isdir(r):
        return out
    for rec_id in os.listdir(r):
        man = _read_manifest(rec_id)
        if not man:
            continue
        pages = man.get('pages', [])
        src   = man.get('source_path')
        out.append({
            'docId':       rec_id,
            'filename':    man.get('filename', ''),
            'page_num':    (pages[0].get('page_num') if pages else 0),
            'total_pages': man.get('total_pages', 0),
            'updatedAt':   man.get('updatedAt', 0),
            'source_path': src,
            'source_exists': bool(src and os.path.isfile(src)),
            'size':        sum(p.get('size', 0) for p in pages),
        })
    out.sort(key=lambda r: r.get('updatedAt', 0), reverse=True)
    return out


def load(rec_id):
    """Read a full snapshot (manifest + every page's bytes) for restore, or None
    if it's missing/corrupt."""
    man = _read_manifest(rec_id)
    if not man:
        return None
    d = _doc_dir(rec_id)
    pages = []
    for p in man.get('pages', []):
        path = os.path.join(d, p.get('file', ''))
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except OSError:
            return None                     # incomplete snapshot — refuse it
        pages.append({
            'id':          p.get('id') or uuid.uuid4().hex,
            'filename':    p.get('filename', ''),
            'page_num':    p.get('page_num', 0),
            'total_pages': p.get('total_pages', 0),
            'rotation':    p.get('rotation', 0),
            'width_pts':   p.get('width_pts', 595.28),
            'height_pts':  p.get('height_pts', 841.89),
            'thumbnail':   p.get('thumbnail'),
            'form_fields': p.get('form_fields') or [],
            'pdf_bytes':   data,
        })
    if not pages:
        return None
    return {
        'rec_id':      rec_id,
        'source_path': man.get('source_path'),
        'comments':    man.get('comments'),
        'bookmarks':   man.get('bookmarks'),
        'metadata':    man.get('metadata'),
        'pages':       pages,
    }


# ── Housekeeping ─────────────────────────────────────────────────────────────
def _dir_size(d):
    total = 0
    for name in os.listdir(d):
        try:
            total += os.path.getsize(os.path.join(d, name))
        except OSError:
            pass
    return total


def _prune_old():
    r = _root()
    if not r or not os.path.isdir(r):
        return
    now = time.time() * 1000
    for rec_id in os.listdir(r):
        man = _read_manifest(rec_id)
        if not man or (now - man.get('updatedAt', 0)) > _MAX_AGE_SEC * 1000:
            _purge(rec_id)


def _enforce_cap():
    r = _root()
    if not r or not os.path.isdir(r):
        return
    entries = []
    total = 0
    for rec_id in os.listdir(r):
        d = os.path.join(r, rec_id)
        if not os.path.isdir(d):
            continue
        man = _read_manifest(rec_id)
        sz = _dir_size(d)
        total += sz
        entries.append((man.get('updatedAt', 0) if man else 0, sz, rec_id))
    if total <= _STORE_CAP_BYTES:
        return
    entries.sort()                          # oldest first
    for _ts, sz, rec_id in entries:
        if total <= _STORE_CAP_BYTES:
            break
        _purge(rec_id)
        total -= sz
