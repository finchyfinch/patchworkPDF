# Background Pre-load / Fast-Start — Upgrade Plan

**Status:** planning + Phase 0 complete. No feature code written yet.
**Author context:** Claude Code session, 2026-07-24.
**Goal:** make PatchworkPDF *feel* like it launches almost instantly (Adobe-style),
cross-platform, as an opt-in setting that can be switched off (restart required
after changing).

This document is the durable reference for the whole upgrade. It is broken into
phases; work stops at the end of each phase for review before the next begins.

> **Outcome (2026-07-24): Phase 1 shipped; project paused here by decision.**
> The Phase 0 measurement showed the real problem was a single-instance probe
> stall, not library/WebView weight. The Phase 1 fix cut steady-state cold start
> from ~8.7s to ~2.5s on real hardware (first-of-session launch ~15.8s → ~7.5s),
> which resolved the actual user complaint. Phases 2–4 (resident background
> process) remain fully specified below as **optional future work** — revisit
> only if the first-launch-of-session time becomes a priority.

---

## 0. Problem statement

Users on lower-spec, business-focused machines report that startup is too slow.
They are used to Adobe, which pre-loads libraries / runs a resident helper so the
window *appears* almost instantly. We want the same: keep a warm instance alive
so every user click is a fast "second launch" rather than a cold start.

The app **already** launches instantly when an instance is running — the
single-instance path hands off in milliseconds and the existing window comes
forward. So the feature is really: **keep a warm instance resident in the
background so the first click of the day is also fast.**

---

## 1. Phase 0 findings (COMPLETE) — measure & de-risk

Real boot timelines were pulled from `logfile.log` (project root, dev checkout)
and `%LOCALAPPDATA%\PatchworkPDF\logfile.log` (frozen/user runs). The
`[BOOT]` timeline (see `bootstrap/boot_timing.py`) makes the bottleneck
unambiguous.

### 1.1 Where the time actually goes

Representative cold start (no document), deltas between marks:

| Phase                                   | Delta        |
|-----------------------------------------|--------------|
| logging configured                      | ~0.05–1.0 s  |
| **single-instance check passed**        | **+6.1–7.0 s** ← dominant |
| heavy imports (flask, pypdf, …)         | +0.6–2.7 s   |
| flask server listening                  | +0.5 s       |
| **window shown (WebView2)**             | **+0.7–1.2 s** |
| page ready / DOMContentLoaded           | +0.1–0.5 s   |
| heavy deps (numpy/pdfium/PIL/OCR, bkgd) | +1.3–2.3 s (off the critical path) |

**WebView2 — the original suspect — is only ~1 second.** The dominant cost, in
*every* run, is the single-instance check at **+6–7 seconds**.

### 1.2 Root cause of the 6–7s stall

Two facts combine:

1. **`main()` exits via `os._exit(0)`**, which **skips `atexit` handlers**. The
   `_cleanup_rendezvous` cleanup registered in
   `single_instance.become_primary_or_handoff()` therefore **never runs on a
   normal window close**. `instance.json` is always left behind pointing at the
   previous session's (now-closed) port.

2. **On this class of machine, a TCP connect to a closed `127.0.0.1` port stalls
   for the full timeout instead of being refused instantly.** Measured: a raw
   `socket.connect()` to a closed loopback port took **1005 ms** (== the 1.0s
   timeout), not the sub-millisecond `ECONNREFUSED` a normal machine returns.
   SYN packets to closed loopback ports are being silently *dropped* — typical
   of managed-business-machine firewall / endpoint-security policy.
   (`urllib.getproxies()` was empty, so proxy autoconfig is **not** the cause.)

Result: on cold start, `find_running_instance()` pings the stale rendezvous port
(**1s stall**) and then scans **5 candidate ports** (**5 × 1s stall**) ≈ 6s of
pure dead time — before any real work begins.

### 1.3 Consequence for the plan

The headline user complaint ("slow startup on business machines") is mostly a
**single-instance probe bug**, not WebView2 or library-load weight. This is
fixable with a small, low-risk change (new **Phase 1** below) that should reclaim
~6 seconds on its own — likely the bulk of the felt pain. The resident-process
feature (Phases 2–3) is still worthwhile to erase the remaining ~2–3s
(interpreter + imports + WebView spin-up), but its cost/benefit is now secondary
to the probe fix.

### 1.4 pywebview backend facts (for later phases)

- Installed: **pywebview 5.4**, backends present: `edgechromium` (Windows),
  `cocoa` (macOS), `gtk` (Linux), plus others unused here.
- Hide/show + close-interception (needed for Phase 2) exist in this version but
  behave differently per backend; a per-backend spike is the first task of
  Phase 2, not an assumption.

---

## 2. Phase 1 — Fast single-instance probe (highest ROI, low risk)

**Objective:** eliminate the ~6s cold-start stall without any resident process or
new user-facing behavior. This ships independently of the rest of the feature.

**Design levers** (all in `bootstrap/single_instance.py`; module stays
stdlib-only):

1. **Lock-first, probe-only-if-needed.** The OS file lock (`instance.lock`) is
   already the authoritative single-instance signal (decoupled from the port in
   v2.11). Try to acquire it *before* any port probing:
   - Lock acquired ⇒ we are definitively the only instance ⇒ **skip all port
     probing entirely** (saves the full ~6s on the common cold-start path).
   - Lock refused ⇒ an instance really is running ⇒ *then* locate it to hand off
     (that peer is alive and answers a probe in a few ms).
   This inverts today's order (probe-then-lock) in both `handle_existing_instance`
   (pre-heavy-import fast path) and `become_primary_or_handoff`.

2. **Short, loopback-appropriate timeouts.** A live local service answers in
   <5 ms; 1000 ms is absurd for loopback. Drop the ping/connect timeout to
   ~200 ms. Even in the fallback scan, worst case becomes ~1s, not ~6s.

3. **Fast TCP pre-check before the HTTP ping.** Use a short-timeout
   `socket.create_connection` first; only issue the `/api/ping` identity check if
   the TCP connect succeeds. Avoids paying HTTP-stack cost on dropped probes.

4. **Parallelize the candidate scan** (only reached when the lock is held by a
   peer): probe the candidate ports concurrently so total cost is ~1× timeout,
   not 5×.

5. **Keep the rendezvous file fresh.** Either clean it on a real quit path, or
   (simpler) rely on lock-first so a stale file is never probed on cold start.

**Risks / watch-items:**
- The pre-heavy-import fast-exit path must still hand a double-clicked file to a
  running instance correctly. Lock-first preserves this: only a *failed* lock
  acquisition triggers the (now fast) locate-and-forward.
- Ensure the held lock handle from the fast path is carried into `main()` so we
  don't acquire twice / release early.
- Verify on a machine exhibiting the drop-timeout behavior that cold start drops
  to low single-digit seconds.

**Acceptance:** cold-start `single-instance check passed` delta falls from
~6–7s to well under 1s; second-instance file hand-off and "already running"
behavior unchanged.

---

## 3. Phase 2 — Background lifecycle (Option B: persist-after-first-launch)

**Objective:** after the user's first manual launch, closing the window **hides**
it instead of exiting, so every later launch that session is instant. No login
autostart yet. This builds the core mechanism reused by Phase 3.

**Step 0 — backend spike (de-risk before building):** confirm on all three
backends (`edgechromium`, `cocoa`, `gtk`) that we can (a) intercept the window
`closing` event and cancel it, and (b) `hide()` / `show()` + foreground the
window reliably. Document per-backend caveats and hidden-window throttling.

**Mechanism:**
- **Hide instead of exit on close.** When background mode is on, intercept the
  pywebview `closing` event, cancel it, and `window.hide()` instead of
  `destroy()` → `os._exit(0)`. Flask, the WebView runtime and loaded libraries
  stay resident.
- **Reveal instead of "already running" on relaunch.** Add a token-authenticated
  `/api/reveal` IPC. A no-file second launch POSTs it to the primary, which
  `show()`s + foregrounds the hidden window, then the second process exits. User
  sees the window appear instantly. (Gated by the setting; when off, today's
  "already running" message is retained.)
- **"Quit completely"** menu item (File menu) for a true exit that tears down the
  resident process.

**Lifecycle inversions to handle carefully** (these are load-bearing assumptions
in `entry.py` today):
- "The OS window is the liveness signal" + `os._exit(0)` on last close — the
  process must now survive with **zero** visible windows.
- `_purge_all_drafts()` + `_recovery.purge_all()` currently run on clean close.
  On a **hide** they must **not** run (documents are still open); they run only
  on a true quit. Getting this wrong corrupts crash-recovery semantics.
- Pop-out windows (`_LIVE_WINDOWS`) — hide applies to the main window; genuine
  pop-out closes must still close normally.

**Settings surface (new — none exists today):**
- There is no Settings dialog today; the only preference UI is the update-notify
  checkbox inside the update dialog, persisted via `_read/_write_update_prefs`
  to `update_prefs.json` in `platform_paths.user_data_dir()`.
- Add a small **Preferences dialog** reachable from the File menu, following the
  update-dialog markup pattern.
- Persist to a new `settings.json` in `user_data_dir()`. Setting has three
  levels: **Off** (today) / **Keep running in background** (Phase 2) / **…and
  start at login** (Phase 3).
- Read the pref **early** in `entry.py`, before the window-close wiring, so it
  governs hide-vs-exit.
- **Default: Off.** Copy must state the RAM trade-off plainly (see §6).
- Add `data-i18n` keys across all five locales (`en/es/fr/de/it`).

**Acceptance:** with the setting on, closing the window hides it and leaves a
resident process; relaunching reveals it in <0.5s; recovery/draft data is
preserved across a hide and only cleared on true quit; with the setting off,
behavior is identical to today.

---

## 4. Phase 3 — Autostart / prewarm at login (Option A: most Adobe-like)

**Objective:** the opt-in "…and start at login" level. A hidden instance starts
at OS login so even the first click of the day is instant. This is Phase 2 plus
autostart registration + a `--background` launch flag.

**Mechanism:**
- **`--background` launch flag:** start with the window created but hidden
  (never shown until a reveal), heavy deps warmed lazily/idle so login-time
  contention is minimized. Consider a **delayed/idle warm** rather than warming
  immediately at login (a business machine's login is already contended).
- **Register/deregister autostart on toggle:**
  - **Windows:** `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` value (no
    admin) launching the app with `--background`.
  - **macOS:** a `~/Library/LaunchAgents/*.plist` (or `SMAppService`) pointing at
    the `.app` with the flag.
  - **Linux:** an XDG `~/.config/autostart/*.desktop`. **Flatpak is the sharp
    edge** — sandboxed autostart needs the Background portal; ship as
    "unsupported under Flatpak" initially and document it.
- All autostart writes live behind `platform_io` so the rest of the app never
  special-cases the OS inline (matches existing `platform_paths` convention).

**Risks / watch-items:**
- First login still pays the cold cost (now shortened by Phase 1) — just at login
  instead of on click. Idle/delayed warm mitigates contention.
- Toggling the setting off must deregister autostart **and** quit any running
  background instance.

**Acceptance:** enabling the top level registers autostart; after a reboot a
hidden instance is resident and the first click reveals it instantly; disabling
removes the autostart entry and quits the background instance.

---

## 5. Phase 4 — Polish

- **Tray icon** (`pystray`; uses Pillow, already bundled). Gives the resident
  process a visible presence and a "Quit" affordance. Linux needs
  AppIndicator/libayatana — the reason this is deferred to polish, not Phase 2.
  Until this ships, an invisible resident process is a support-call risk;
  mitigated by the "Quit completely" menu item from Phase 2.
- **Updater coordination:** a resident process holds the exe/DLLs open; the
  SourceForge installer could fail to replace locked files. The update flow
  (`services/check_for_updates.py` + `update_check`) must be able to signal the
  background instance to quit before an update installs.
- **Delayed / idle warm** tuning at login (if not already done in Phase 3).
- **Opportunistic cold-path trimming** (former Option C): trim the cx_Freeze
  module set, lazy-import Flask pieces, WebView pre-warm env — helps the genuine
  first launch that even autostart can't avoid. Low ceiling; do only if cheap.

---

## 6. Cross-cutting concerns

- **Memory is the central tension.** The users who benefit (slow CPU/disk)
  overlap with those who can least afford a resident ~200–400 MB process + WebView
  runtime (low-RAM business boxes also running Outlook/Teams/Chrome). The feature
  **must** be opt-in, **off by default**, with the RAM trade stated plainly in
  the settings copy. On a RAM-starved machine it can make the whole system slower.
- **Security:** Flask is already bound to `127.0.0.1` with per-launch token auth
  + rate limiting. A longer-lived resident server widens the window the port is
  open, but it stays localhost-only + token-gated. `/api/reveal` must require the
  token. Low delta risk.
- **Platform scope for v1:** Windows first (primary user base), then macOS/Linux.

---

## 7. Decision log (confirmed with user 2026-07-24)

- Strategy: **layered B → A** (build Phase 2 lifecycle once, expose Phase 3
  autostart as an opt-in sub-toggle). ✅
- Tray icon: **Phase 4** (polish), not a Phase 2 must-have. ✅
- Default: **off by default**, with an explanatory RAM note. ✅
- Platform order: **Windows first**, then all three. ✅
- Phased approach with a **stop-and-review gate at the end of each phase**. ✅
- Phase 0 measurement surfaced the single-instance probe stall; a new **Phase 1**
  (probe fix) was inserted ahead of the resident-process work as the highest-ROI,
  lowest-risk win. ✅ (documented here; awaiting go-ahead to implement)

---

## 8. Progress tracker

- [x] **Phase 0 — Measure & de-risk.** Complete. Bottleneck identified: single-
      instance probe stall (~6–7s), not WebView2. pywebview 5.4 backend facts
      captured.
- [x] **Phase 1 — Fast single-instance probe.** Complete (2026-07-24). All
      changes in `bootstrap/single_instance.py`: lock-first arbitration
      (idempotent `_try_acquire_lock`; primary skips probing entirely),
      `_PROBE_TIMEOUT` 0.25s, two-stage `_ping` (bounded TCP pre-connect +
      proxy-bypassed HTTP), parallel candidate fallback scan, shorter
      `is_listening`. **Measured (headless, dev):** `single-instance check
      passed` delta **6,300 ms → 33 ms**; second-instance detection of a running
      primary in ~95 ms. Estimated windowed cold-start-to-interactive now
      ~3.5–4s (was ~9–10s). Awaiting real-machine confirmation by user before
      deciding whether Phases 2–4 are still needed.
- [ ] **Phase 2 — Background lifecycle (hide-on-close + reveal + settings).**
      *Optional / not scheduled — Phase 1 resolved the complaint (see Outcome).*
- [ ] **Phase 3 — Autostart / prewarm at login.** *Optional / not scheduled.*
- [ ] **Phase 4 — Polish (tray, updater coordination, warm tuning, cold-path).**
      *Optional / not scheduled.*
