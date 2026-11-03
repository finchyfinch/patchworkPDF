"""Regression tests for "Save downloads the PDF instead of saving it" (Linux).

The app's CSP deliberately omits 'unsafe-eval'.  WebKitGTK enforces that on
pywebview's own bridge plumbing, which compiled strings into code twice:

  * js/api.js built every js_api method with `new Function(...)`, so
    `window.pywebview.api` was never populated, isNativeApp() was false, and
    Save fell through to the browser download path ("PDF downloaded");
  * each return value came back through window.evaluate_js(), whose eval()
    wrapper is blocked too — a native dialog's promise would never resolve.

entry._csp_safe_js_bridge rewrites the first as a closure and routes the second
through run_js.  A real WebKitGTK is not reachable from a test, so the JS half
runs in a Node vm with string code generation DISABLED — the same restriction
the CSP imposes — and the tests first prove the unpatched api.js fails there,
so a pass means something.

Needs pywebview (for its api.js and js_bridge_call) and Node; skips without
either.  The Flatpak build VM has neither by design, so this runs on the dev box.
"""
import json
import os
import shutil
import subprocess
import time

import _bootstrap  # noqa: F401  (path setup)

import pytest

import entry

webview = pytest.importorskip('webview')
from webview import util as wv_util  # noqa: E402

NODE = shutil.which('node')
API_JS = os.path.join(os.path.dirname(webview.__file__), 'js', 'api.js')

_PARAMS = {'token': 't', 'platform': 'gtkwebkit2', 'uid': 'master',
           'js_api_endpoint': ''}

# Runs api.js in a context that cannot compile strings, creates the API, calls
# pick_save_path, then delivers REPLY (a run_js statement) and prints the result.
_HARNESS = r"""
const vm = require('vm');
const [apiJs, replyTemplate] = process.argv.slice(-2);  // node -e: args start at argv[1]
const posted = [];
const ctx = { setInterval, clearInterval, console, JSON,
              Node: function Node() {}, Window: function Window() {} };
ctx.window = ctx;
ctx.window.webkit = { messageHandlers: { jsBridge: {
  postMessage: (m) => posted.push(JSON.parse(m)) } } };
vm.createContext(ctx, { codeGeneration: { strings: false, wasm: false } });
try {
  vm.runInContext(apiJs, ctx);
  vm.runInContext('window.pywebview._createApi(' + JSON.stringify([
    { func: 'pick_save_path', params: ['session_id', 'default_name'] },
    { func: 'nested.ping', params: [] },
  ]) + ')', ctx);
} catch (e) { console.log(JSON.stringify({ error: e.name, message: e.message })); process.exit(0); }
const p = ctx.window.pywebview.api.pick_save_path('sid', 'x.pdf');
const msg = posted[0];
const replyResult = vm.runInContext(replyTemplate.split('__ID__').join(msg.id), ctx);
p.then((v) => console.log(JSON.stringify({
  msg, value: v, replyResult: typeof replyResult,
  nested: typeof ctx.window.pywebview.api.nested.ping })));
"""


def _api_js():
    with open(API_JS, 'r') as f:
        return f.read() % _PARAMS


def _patched_api_js():
    patched, n = entry._API_NEW_FUNCTION.subn(lambda _m: entry._API_CLOSURE, _api_js())
    assert n == 1, 'api.js no longer has the new Function() call this patch targets'
    return patched


def _run(api_js, reply):
    proc = subprocess.run([NODE, '-e', _HARNESS, api_js, reply],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_patch_matches_the_shipped_api_js():
    patched = _patched_api_js()
    assert 'new Function' not in patched
    assert 'eval(' not in patched


class _FakeWindow:
    """What js_bridge_call reads off a pywebview Window."""

    def __init__(self, api):
        self._functions = {}
        self._js_api = api
        self._callbacks = {}
        self.run = []
        self.evaluated = []

    def run_js(self, script):
        self.run.append(script)

    def evaluate_js(self, script, callback=None):
        self.evaluated.append(script)


def _bridge_reply(api, func_name, params):
    """Drive pywebview's real js_bridge_call through the proxy; return the
    statement it sent and the fake window."""
    w = _FakeWindow(api)
    wv_util.js_bridge_call(entry._RunJsReplies(w), func_name, params, '__ID__')
    for _ in range(200):                     # the call runs on its own thread
        if w.run or w.evaluated:
            break
        time.sleep(0.01)
    return w


def test_bridge_replies_go_through_run_js_not_eval():
    class Api:
        def pick_save_path(self, session_id=None, default_name=None):
            return '/home/u/' + default_name

    w = _bridge_reply(Api(), 'pick_save_path', ['sid', 'x.pdf'])
    assert w.evaluated == [], 'reply went through the eval() wrapper'
    assert len(w.run) == 1
    assert 'eval(' not in w.run[0]


@pytest.mark.skipif(NODE is None, reason='node not on PATH')
def test_unpatched_api_js_fails_without_eval():
    """Proves the harness models the CSP: the stock api.js cannot build its API."""
    assert _run(_api_js(), '')['error'] == 'EvalError'


@pytest.mark.skipif(NODE is None, reason='node not on PATH')
def test_patched_bridge_round_trips_without_eval():
    class Api:
        def pick_save_path(self, session_id=None, default_name=None):
            return '/home/u/' + default_name

    reply = _bridge_reply(Api(), 'pick_save_path', ['sid', 'x.pdf']).run[0]
    out = _run(_patched_api_js(), reply)
    assert out['msg']['funcName'] == 'pick_save_path'
    assert out['msg']['params'] == ['sid', 'x.pdf']
    assert out['value'] == '/home/u/x.pdf'
    # The reply script must evaluate to undefined: pywebview's gtk backend
    # converts an object result with JSC Value.to_object(), which the GNOME 50
    # runtime lacks — every reply logged an AttributeError traceback.
    assert out['replyResult'] == 'undefined'
    assert out['nested'] == 'function'


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
