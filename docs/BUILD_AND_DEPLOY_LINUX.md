# Build & Deploy — Linux (Flatpak)

How to build Patchwork PDF as a Flatpak and self-host it on SourceForge (the same
way the Windows `.exe` and macOS `.dmg` are distributed). Flatpak is the chosen
Linux format because the GNOME runtime already provides WebKitGTK (the pywebview
`gtk` backend) and PyGObject, so dependency handling is largely automatic.
For the design behind the cross-platform work see
[CROSS_PLATFORM_PLAN.md](CROSS_PLATFORM_PLAN.md).

> **You do not need a domain or a Flathub account.** This guide self-hosts a
> single `.flatpak` file on SourceForge. Users install it with one command and
> the in-app update checker notifies them of new versions — just like on Windows
> and macOS. (Publishing to the Flathub *store* is a separate, optional path
> covered briefly in §6.)

Packaging files live in `packaging/flatpak/`:
`com.patchworkpdf.app.yaml` (manifest), `patchworkpdf` (launcher),
`…​.desktop`, `…​.metainfo.xml`.

---

## 1. Prerequisites

```bash
sudo apt install flatpak flatpak-builder            # or your distro's equivalent
flatpak remote-add --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo
flatpak install flathub org.gnome.Platform//50 org.gnome.Sdk//50
```
The runtime version here **must match `runtime-version` in the manifest** (currently
`50` — the newest supported GNOME runtime as of 2026-07). GNOME ships a new runtime
about every 6 months and each is supported for ~1 year, so an old one eventually
goes end-of-life: it still installs but gets no security updates and shows users an
"end-of-life" warning. When that happens, find the current default and bump both the
manifest and this command to match:
```bash
flatpak remote-info flathub org.gnome.Platform | grep -i version   # newest branch
```

> **Host distro is irrelevant.** A Flatpak runs against the GNOME runtime, not the
> host OS, so a bundle built on (say) Ubuntu 26.04 installs and runs unchanged on
> 22.04, Fedora, Arch, etc. Only the *runtime version* matters, not where it was built.

### Building from a Windows host (VM + shared folder)

Development happens on Windows, so the Linux release is built in a **VirtualBox
Ubuntu 26.04 guest** with a shared folder carrying the source in and the bundle
back out. The current setup: host `C:\Temp` is shared as `CTemp`, which the guest
mounts at `/media/sf_CTemp`.

> **Never run the build on the share.** VirtualBox mounts a `vboxsf` folder
> `root:vboxsf` with no real ownership or symlink semantics, and `flatpak-builder`
> cannot work there — it fails partway through with permission errors that look
> like a broken manifest. Sync the tree into `$HOME` first, build there, and have
> `build_flatpak.sh` copy the finished bundle back to the share (it copies to the
> directory passed as its first argument).

The per-release routine:

1. **Stage the source on the host** at `C:\Temp\patchworkPDF_source_v<X.YY>\` — a
   copy of the version folder **minus** build residue (`__pycache__`, `*.pyc`,
   `*.egg-info`, `.pytest_cache`, `.idea`) and **minus the Windows build output**
   (`build/` and `Output/`, which are gitignored and run to a few hundred MB after
   an installer build). Around 130 files.
2. **Copy the previous release's helper script** to
   `C:\Temp\pw_build_v<X.YY>.sh` and update the version strings in it. The script
   is deliberately outside the repo — it describes this machine's VM, not the
   project. Its shape is:
   - rsync `$SRC/` → `$HOME/pw_build_v<X.YY>/`, **excluding `.flatpak-builder`**;
   - seed that cache from the newest previous build tree if this one has none;
   - strip CRs from `packaging/flatpak/build_flatpak.sh` and
     `packaging/flatpak/patchworkpdf`, and `chmod +x` both — a Windows share
     reintroduces them, and a `\r` makes the shebang line fail with
     `$'\r': command not found`;
   - pre-flight the version gate, tessdata and the Python deps the test gate needs;
   - run `./packaging/flatpak/build_flatpak.sh /media/sf_CTemp`;
   - `exec > >(tee "$LOG") 2>&1` to a log on the share, so the Windows host can
     watch the build at `C:\Temp\pw_build_v<X.YY>.log` while it runs.
3. **Run it in the guest:**
   ```bash
   bash /media/sf_CTemp/pw_build_v<X.YY>.sh
   ```
4. The bundle lands at `C:\Temp\PatchworkPDF-<version>.flatpak` (~84 MB), ready to
   copy to the distribution folder.

> **Keep `.flatpak-builder`.** It holds the cached build stages and downloaded
> sources. A cold build recompiles Leptonica and Tesseract from source — roughly
> 40 minutes; a warm one finishes in a few. Deleting the build tree wholesale
> between releases throws that away, which is why the sync excludes it rather than
> using a plain `rm -rf`.

> **The test gate runs on the guest's interpreter**, not inside the sandbox, so the
> app's Python dependencies must be installed in the guest for
> `tools/run_tests.py` to run at all (§11). Install just the pinned set from
> `requirements.txt` — **not** `-r requirements.txt`, which pulls `pywebview`,
> whose GTK/WebKit bindings must come from the system rather than pip:
> ```bash
> python3 -m pip install --break-system-packages \
>     pikepdf==10.9.1 pypdf==6.14.2 pypdfium2==5.10.1 \
>     numpy==2.5.0 flask==3.1.3 python-docx==1.2.0 Pillow==12.2.0 \
>     reportlab==5.0.0 openpyxl==3.1.5 pdfplumber==0.11.10 pytest
> ```
> `pytest` is not optional here: `tools/run_tests.py` can run without it, but
> most suites import it for their fixtures and fail with `No module named
> 'pytest'` (a fresh WSL Ubuntu hit exactly that at v3.3).
> Node is optional: without it the front-end suites in `tests/js/` skip rather
> than fail.

---

## 2. How dependencies are satisfied

| Dependency | How it ships |
|------------|--------------|
| GTK, **WebKitGTK**, **PyGObject** | Provided by `org.gnome.Platform` — not built. |
| Python pip libraries (Flask, pypdf, pypdfium2, pikepdf, Pillow, numpy, reportlab, python-docx, openpyxl, pytesseract) | Installed into `/app` by the `python-deps` module. |
| pdfium, QPDF native libs | Come with the pip wheels inside the sandbox. |
| **Tesseract + Leptonica** (OCR) | **Built into the Flatpak from source** (the sandbox can't see a host Tesseract). This is the slowest part of the build; comment the `autoconf-archive`/`leptonica`/`tesseract` modules out to skip it (§4). |
| **LibreOffice** (Office import) | **Host** LibreOffice, reached via `flatpak-spawn --host` (needs `--talk-name=org.freedesktop.Flatpak`). Degrades gracefully to "convert manually" if absent. |
| tessdata language packs (eng/spa/fra/deu/ita) | **Fetched by the `tessdata` module** from tessdata_fast 4.1.0 into the app's `assets/tessdata`, overwriting whatever the checkout carried — so the bundle always gets the checksum-pinned set. Needs build-time network like `python-deps`. |

The user installs one `.flatpak`; everything above is resolved automatically.

---

## 3. Build & run locally

From the repository root:
```bash
flatpak-builder --user --install --force-clean build-dir \
  packaging/flatpak/com.patchworkpdf.app.yaml
flatpak run com.patchworkpdf.app
```
This builds the full app **including OCR**. The first build compiles leptonica +
tesseract, so it takes a few minutes; subsequent builds reuse the cache. The
`python-deps` module uses `pip install` with build-time network (`--share=network`),
which is fine for self-hosted builds. (Only Flathub forbids build-time network —
see §7 if you ever go that route.)

---

## 4. OCR (built in by default)

A Flatpak can't see a host `tesseract`, so the manifest builds it into the sandbox.
Because tesseract's GitHub archive has no `./configure`, it runs `autogen.sh`, which
needs the `AX_*` macros from `autoconf-archive` — so OCR is three build modules
(`autoconf-archive` → `leptonica` → `tesseract`) plus a `tessdata` module that
fetches the `eng/spa/fra/deu/ita` language packs into `assets/tessdata`. All are
pinned with verified checksums.

- **To skip OCR** (faster builds, smaller bundle if you don't need it): comment out
  those four modules in `packaging/flatpak/com.patchworkpdf.app.yaml`. The app then
  reports OCR "unavailable" and everything else works.
- **If the tesseract build fails**, it's almost always a missing `AX_*` macro —
  confirm the `autoconf-archive` module built first (its macros install to
  `/app/share/aclocal`, which the tesseract `autogen.sh` step points `ACLOCAL_PATH` at).
- **To add/remove languages**, edit the `tessdata` module's source list (checksums
  from `sha256sum <file>` against the tessdata_fast 4.1.0 tag) — and make the same
  change in `LANGS` in `tools/fetch_tessdata.py` (which pins the identical URLs and
  checksums for the Windows/macOS builds) and in `_UI_TO_TESS` in
  `src/backend/pdf/ocr_utils.py`. The Flatpak cannot call that script: a Flatpak
  build only gets network for declared module sources, hence the duplicated list.
- **To verify a bundle actually got the packs**, check inside the running sandbox:
  ```bash
  flatpak run --command=ls com.patchworkpdf.app /app/share/patchworkpdf/assets/tessdata
  ```
  Five `.traineddata` files should be listed.

---

## 5. Package the distributable `.flatpak`

`flatpak-builder --install` puts the app in your user installation, whose OSTree
repo is `~/.local/share/flatpak/repo`. Bundle straight from there into one file
named to match the update checker (`PatchworkPDF-<version>.flatpak`):
```bash
VERSION=$(python3 -c 'import json;print(json.load(open("version.json"))["version"])')

flatpak build-bundle \
  --runtime-repo=https://flathub.org/repo/flathub.flatpakrepo \
  ~/.local/share/flatpak/repo \
  "PatchworkPDF-${VERSION}.flatpak" \
  com.patchworkpdf.app
```
`--runtime-repo` embeds Flathub's URL so a user without the GNOME runtime is
pointed at where to fetch it (a single-file bundle carries only the app, not the
~300 MB runtime).

Smoke-test the bundle before uploading:
```bash
flatpak remove --user com.patchworkpdf.app          # drop the builder-installed copy
flatpak install --user "PatchworkPDF-${VERSION}.flatpak"
flatpak run com.patchworkpdf.app                     # confirm launch + OCR
```

The app-id stays `com.patchworkpdf.app` — for self-hosted bundles the id is not
verified by anyone, so no domain and no repo are required.

### Desktop menu entry (point-and-click launch)
The manifest already ships `com.patchworkpdf.app.desktop` + icons, which Flatpak
exports to the application menu on install — users launch it by clicking the icon,
not from a terminal. Verify the export:
```bash
ls ~/.local/share/flatpak/exports/share/applications/ | grep patchwork
```
**First-Flatpak gotcha:** on a machine where Flatpak was *just* installed, the
exports dir isn't in `XDG_DATA_DIRS` for the current login session, so the icon
won't appear until the user **logs out and back in once** (the same warning
flatpak prints on first run). Systems that already run Flatpak apps show it
immediately. Worth noting on the SourceForge download page.

**Stale-shell gotcha:** the desktop-file ID `com.patchworkpdf.app.desktop` is the
same in both scopes, and the user-scope export sits *earlier* in `XDG_DATA_DIRS`
than the system one. If you uninstall a user-scope copy while a system-scope copy
remains, GNOME Shell can keep the now-dead ID cached and show **no** icon at all,
even though the system `.desktop` is present and valid. Confirm the file is
genuinely fine before chasing it:
```bash
ls -l /var/lib/flatpak/exports/share/applications/com.patchworkpdf.app.desktop
python3 -c "from gi.repository import Gio; print([a.get_id() for a in Gio.AppInfo.get_all() if 'patchwork' in a.get_id()])"
```
If GIO lists it, the file is fine and only the shell is stale — **log out and back
in**. On Wayland (Ubuntu's default) `Alt`+`F2` → `r` does not work; that is X11-only.

---

## 6. Deploy / publish (SourceForge)

1. Build `PatchworkPDF-<version>.flatpak` (§5).
2. Upload it to the SourceForge **files** root, alongside the Windows installer,
   the macOS `.dmg`, and `changelog.docx`:
   `https://sourceforge.net/projects/patchworkpdf/files/`
3. Bump `version.json` **before** building so the artifact name and reported
   version agree.

The in-app update checker (`check_for_updates.py`) runs on Linux too: it reads the
SourceForge RSS feed, finds the newest `PatchworkPDF-<version>.flatpak`, and — if
it is newer than the running version — shows the user the release notes and the
download link. (The Flatpak grants `--share=network` for this; the app cannot
self-install the new bundle, so the user re-runs `flatpak install`.)

---

## 7. Optional: publish to Flathub (later)

Only worth it if you want store discoverability and automatic updates. It needs a
free public code-hosting account (no domain) to prove the app-id:
- `io.github.<you>.PatchworkPDF` (GitHub), `io.gitlab.<you>.PatchworkPDF` (GitLab),
  or `page.codeberg.<you>.PatchworkPDF` (Codeberg).

If you go this route you would: rename the four files in `packaging/flatpak/` and
the ids inside them to the chosen id; replace the `python-deps` module with
vendored wheels from `flatpak-pip-generator` (Flathub forbids build-time network);
add a real screenshot URL + license id to the metainfo and validate it
(`flatpak run org.freedesktop.appstream-glib validate …`); then open a PR against
`github.com/flathub/flathub`. On Flathub, disable the in-app update checker again
(Flathub self-updates). None of this is needed for the self-hosted path above.

---

## 8. Office import & OCR notes (sandbox specifics)

- **OCR** is built in by default (§4) and works offline — the tesseract binary is
  built into the Flatpak and the fetched `tessdata` packs are passed via
  `--tessdata-dir` (the app requests `<uilang>+eng`).
- **Office import** requires **LibreOffice on the host** and the
  `--talk-name=org.freedesktop.Flatpak` permission (present in the manifest). The
  app copies the source into a home-mounted temp dir and runs
  `flatpak-spawn --host soffice …` (see `pdf_core._office_to_pdf_via_libreoffice`).
  Remove that `finish-arg` if you prefer to disable Office import in the sandbox;
  the app then shows the graceful "convert manually" message.
- **The local server port is chosen at runtime — it is not 5000.** The frontend
  talks to a Flask server on loopback, and `bootstrap/single_instance.py` picks
  `49371`, then `49372`–`49375`, then an OS-assigned ephemeral port if all five
  are busy. Those come from the IANA dynamic range deliberately, to stay out of
  the crowded 5000/8000/8080 space that other dev tools occupy. Because the
  manifest grants `--share=network`, the sandbox shares the host network
  namespace, so that port is reachable from the host loopback — handy for
  inspecting the running app without entering the sandbox:
  ```bash
  ss -lntp | grep 127.0.0.1                       # find the live port
  curl -sI http://127.0.0.1:<port>/version.json   # e.g. check cache headers
  ```
  The live port is also published in `instance.json` under the sandboxed data dir
  (§9), and `/api/ping` identifies the server as `patchwork-pdf`. Set
  `PATCHWORK_PORT=<n>` to force a single port for a scripted or headless run.

---

## 9. Per-user data locations (Linux/Flatpak)

Runtime state uses `platform_paths.user_data_dir()` → `$XDG_DATA_HOME/PatchworkPDF`.
Inside the Flatpak sandbox this is transparently remapped to
`~/.var/app/com.patchworkpdf.app/data/PatchworkPDF/`
(`logfile.log`, `recent_files.json`, `license_accepted.json`, `lang.txt`,
`instance.lock`, `instance.json`). Non-Flatpak source runs use
`~/.local/share/PatchworkPDF/`.

---

## 10. Troubleshooting: "I installed the new version but it runs the old one"

Flatpak has **two independent installations**, and nothing warns you when the same
app is in both:

| Scope | Path | Installed by |
|-------|------|--------------|
| user | `~/.local/share/flatpak` | `flatpak install --user …`, and `build_flatpak.sh` |
| system | `/var/lib/flatpak` | `flatpak install …` with **no scope flag** (the default; authenticates via a polkit dialog, *not* sudo) |

Every release ships as the same ref `app/com.patchworkpdf.app/x86_64/master`, so
the two copies collide, and **`flatpak run` prefers the user one**. Install a new
build system-wide while an old user-scope copy exists and you will keep launching
the old one — while `flatpak install --or-update` correctly reports "already
installed", because it is only looking at the scope you named.

Diagnose:
```bash
flatpak list --app --columns=application,version,branch,installation
flatpak run --command=cat com.patchworkpdf.app /app/share/patchworkpdf/version.json
```
The second command is authoritative: it prints the `version.json` of whichever
copy actually runs. To read a specific scope instead, look at
`~/.local/share/flatpak/app/com.patchworkpdf.app/current/active/files/share/patchworkpdf/version.json`
or the same path under `/var/lib/flatpak`.

Fix — keep exactly one:
```bash
flatpak uninstall --user   com.patchworkpdf.app     # drop the user-scope copy
flatpak uninstall --system com.patchworkpdf.app     # ...or the system one
```
Neither touches `~/.var/app/com.patchworkpdf.app/` (recents, prefs, licence
acceptance) unless you add `--delete-data`. Removing the shadowing copy can leave
the menu icon missing until you log out and back in — see the stale-shell gotcha
in §5.

> The `version` column is populated from the AppStream metainfo, which is why the
> manifest installs it and why `build_flatpak.sh` refuses to build when the
> metainfo's newest `<release>` disagrees with `version.json`. Without that, the
> column is empty for every build and there is no way to tell two installs apart.

---

## 11. Release checklist

- [ ] `version.json` bumped; `<release>` added to `com.patchworkpdf.app.metainfo.xml`
      (`build_flatpak.sh` aborts if these disagree)
- [ ] `python3 tools/run_tests.py` passes — `build_flatpak.sh` runs this before
      the downloads and the Tesseract compile, and aborts if anything fails
- [ ] Runtime version current; OCR modules present (or intentionally skipped, §4)
- [ ] `flatpak-builder … --install` succeeds; `flatpak run` launches
- [ ] `flatpak list --app --columns=application,version,branch,installation` shows
      **one** entry, with the version you just built (§10)
- [ ] Open/edit/save a PDF anywhere (`--filesystem=host`); import a `.docx`/`.xlsx`
      (host LibreOffice); OCR a scan (if enabled); native title bar with OS
      min/max/close; tab tear-off opens a new window; update check reaches SourceForge
- [ ] `PatchworkPDF-<version>.flatpak` bundle built and installs on a clean machine
- [ ] Uploaded to SourceForge with the matching `changelog.docx`
