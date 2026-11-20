"""Manual check: does Ctrl+P open the print dialog wherever keyboard focus is?

Windows only.  Launches the REAL app window (port 5011) on the manual, sends
real keypresses and reads logfile.log for the [SHORTCUT] lines:
  A focus in the page   B SetFocus on the window   C focus nowhere
  D after the native Open dialog (Ctrl+O, Esc)   E after the print dialog
Every case must end in "print via ...: run".  Takes over the keyboard for
~40s; do not type while it runs.  Run from the project root:
    python tools/verify_ctrl_p.py
"""
import ctypes, ctypes.wintypes as W, subprocess, time, os, sys
u=ctypes.windll.user32; k=ctypes.windll.kernel32
LOG='logfile.log'
start=os.path.getsize(LOG) if os.path.exists(LOG) else 0
env=dict(os.environ, PATCHWORK_PORT='5011')
p=subprocess.Popen([sys.executable,'src/backend/entry.py','docs/PatchworkPDF_Manual.pdf'], env=env)
def logtail():
    with open(LOG,'rb') as f: f.seek(start); return f.read().decode('utf-8','replace')
t0=time.time()
while time.time()-t0<60 and 'heavy deps: loaded' not in logtail(): time.sleep(0.5)
time.sleep(4)
hw=[]
@ctypes.WINFUNCTYPE(W.BOOL,W.HWND,W.LPARAM)
def cb(h,l):
    pid=W.DWORD(); u.GetWindowThreadProcessId(h,ctypes.byref(pid))
    if pid.value==p.pid and u.IsWindowVisible(h): hw.append(h)
    return True
u.EnumWindows(cb,0)
print('windows',hw)
H=hw[0]
KEYUP=2
u.GetFocus.restype=W.HWND; u.SetFocus.argtypes=[W.HWND]; u.SetFocus.restype=W.HWND; u.GetClassNameW.argtypes=[W.HWND,W.LPWSTR,ctypes.c_int]
def key(vk,up=False): u.keybd_event(vk,0,KEYUP if up else 0,0)
def fg():
    key(0x12); key(0x12,True)          # ALT tap unlocks SetForegroundWindow
    u.ShowWindow(H,9); u.SetForegroundWindow(H); time.sleep(0.6)
def ctrl_p():
    key(0x11); key(0x50); key(0x50,True); key(0x11,True); time.sleep(1.5)
def esc(): key(0x1B); key(0x1B,True); time.sleep(0.8)
def cls(h):
    b=ctypes.create_unicode_buffer(256); u.GetClassNameW(h,b,256); return b.value
def focus_host(target=None):
    cur=k.GetCurrentThreadId(); wt=u.GetWindowThreadProcessId(H,None)
    u.AttachThreadInput(cur,wt,True)
    before=u.GetFocus(); u.SetFocus(target if target is not None else H); f=u.GetFocus()
    u.AttachThreadInput(cur,wt,False); return (cls(before) if before else None, cls(f) if f else None)
def click_page():
    r=W.RECT(); u.GetWindowRect(H,ctypes.byref(r)); x=(r.left+r.right)//2; y=(r.top+r.bottom)//2
    u.SetCursorPos(x,y); u.mouse_event(2,0,0,0,0); u.mouse_event(4,0,0,0,0); time.sleep(0.8)
fg(); print('foreground', u.GetForegroundWindow()==H)
mark=len(logtail())
click_page(); ctrl_p(); a=logtail()[mark:]; esc()
mark=len(logtail())
print('host focus set:', focus_host()); ctrl_p(); b=logtail()[mark:]; esc()
mark=len(logtail())
print('focus cleared:', focus_host(0)); ctrl_p(); c=logtail()[mark:]
esc()
def combo(vk):
    key(0x11); key(vk); key(vk,True); key(0x11,True)
# D: Ctrl+O (native Open dialog) -> Esc -> Ctrl+P
mark=len(logtail()); combo(0x4F); time.sleep(3); esc(); time.sleep(1.5)
fgw=u.GetForegroundWindow(); print('after open dialog, our window foreground:', fgw==H, 'fg class', cls(fgw))
cur=k.GetCurrentThreadId(); wt=u.GetWindowThreadProcessId(fgw,None); u.AttachThreadInput(cur,wt,True); ff=u.GetFocus(); u.AttachThreadInput(cur,wt,False)
print('focus after dialog:', ff, cls(ff) if ff else None, 'log since:', [l for l in logtail()[mark:].splitlines() if 'dialog' in l.lower() or 'focus' in l.lower() or 'pick' in l.lower()][-5:])
ctrl_p(); d=logtail()[mark:]; esc()
# E: Ctrl+P -> Enter (Print = system print dialog) -> Esc x2 -> Ctrl+P
mark=len(logtail()); ctrl_p(); key(0x0D); key(0x0D,True); time.sleep(5); esc(); time.sleep(1); esc(); time.sleep(3)
mark2=len(logtail()); ctrl_p(); e=logtail()[mark2:]
import re
for name,t in (('A focus in page',a),('B focus on window',b),('C focus nowhere',c),('D after Open dialog',d),('E after print dialog',e)):
    print(name, '->', re.findall(r'\[SHORTCUT\][^\r\n]*',t))
print('installed:', re.findall(r'\[SHORTCUT\] window shortcuts[^\r\n]*', logtail()))
p.terminate()
