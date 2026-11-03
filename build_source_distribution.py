#!/usr/bin/env python3

# ----------------------------------------------------------------------------------------------------------------------
# build_source_distribution.py

"""
This creates a source file distribution package based on the files and folders contained in the payload info.
"""

# Import python & wxpython modules
import os
import shutil
import wx
import threading
from fnmatch import fnmatch
from pathlib import Path

# Source files we report line counts for, mapped to the label shown in the grid.
COUNTED_SUFFIXES = {
    '.py': 'Python',
    '.js': 'JavaScript',
    '.html': 'HTML',
}

# Line/block comment markers per language. Blank lines are always ignored.
LINE_COMMENT = {'Python': ('#',), 'JavaScript': ('//',), 'HTML': ()}
BLOCK_COMMENT = {'Python': None, 'JavaScript': ('/*', '*/'), 'HTML': ('<!--', '-->')}

# Build residue that must never reach a source distribution.  A 'folder_all'
# entry copies a tree wholesale, so without this the working copy's ~46 stale
# .pyc files, its egg-info metadata and macOS resource forks all shipped —
# and a stale .pyc next to an edited .py is worse than noise, because Python
# will happily import it.
IGNORE_GLOBS = ('__pycache__', '*.pyc', '*.pyo', '*.egg-info', '.pytest_cache',
                '.DS_Store', '._*', '~$*', '*.log')
_ignore = shutil.ignore_patterns(*IGNORE_GLOBS)


def is_junk(path):
    """True for a file the distribution must not carry (matched on the name or
    on any directory above it, so everything under __pycache__/ is covered)."""
    parts = Path(path).parts
    return any(fnmatch(part, pat) for part in parts for pat in IGNORE_GLOBS)


class SourceDistributor(wx.Frame):
    def __init__(self, parent, title="Source Distribution Builder"):
        super().__init__(parent, title=title, size=wx.Size(800, 600))
        self.cwd = Path.cwd()
        self.payload = self.get_payload()
        self.panel = wx.Panel(self)

        # UI Elements
        self.start_button = wx.Button(self.panel, label="START")
        self.output = wx.TextCtrl(self.panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.BORDER_SUNKEN)
        self.output.SetBackgroundColour(wx.Colour(255, 255, 255))

        self.line_count_display = wx.ListCtrl(self.panel, style=wx.LC_REPORT)
        self.line_count_display.InsertColumn(0, "Source File", width=420)
        self.line_count_display.InsertColumn(1, "Type", width=100)
        self.line_count_display.InsertColumn(2, "Line Count", wx.LIST_FORMAT_RIGHT, width=100)

        # Collected as (relative path, language, line count) while copying.
        self.line_counts = []

        self.sizer = wx.BoxSizer(wx.VERTICAL)
        self.sizer.Add(self.start_button, 0, wx.ALL | wx.EXPAND, 5)
        self.sizer.Add(wx.StaticText(self.panel, label="Copy Log:"), 0, wx.LEFT | wx.TOP, 5)
        self.sizer.Add(self.output, 2, wx.EXPAND | wx.ALL, 5)
        self.sizer.Add(wx.StaticText(self.panel, label="Source Line Counts (Python / JavaScript / HTML):"),
                       0, wx.LEFT | wx.TOP, 5)
        self.sizer.Add(self.line_count_display, 1, wx.EXPAND | wx.ALL, 5)

        self.panel.SetSizer(self.sizer)

        self.start_button.Bind(wx.EVT_BUTTON, self.on_start)

        self.Show()

    def log(self, msg, error=False):
        if error:
            self.output.SetDefaultStyle(wx.TextAttr(wx.RED))
        else:
            self.output.SetDefaultStyle(wx.TextAttr(wx.BLACK))
        self.output.AppendText(msg + "\n")

    def get_payload(self):
        return [
           ['docs/changelog.docx', 'cwd', 'file'],
           # All application source now lives under src/ :
           #   src/backend/  — Python packages (bootstrap, platform_io, pdf,
           #                   routes, i18n, services) + entry.py
           #   src/frontend/ — viewer.html, css/, js/
           ['src', 'cwd', 'folder_all'],
           ['assets/icon.ico', 'cwd', 'file'],
           ['assets/icon_256.png', 'cwd', 'file'],
           ['assets/MicrosoftEdgeWebview2Setup.exe', 'cwd', 'file'],
           ['assets/fonts', 'cwd', 'folder_all'],
           ['requirements.txt', 'cwd', 'file'],
           ['setup.py', 'cwd', 'file'],
           # Windows installer files (installer.iss, build_installer.bat,
           # setup.bat) live under packaging/win/ and are copied by the
           # ['packaging', 'cwd', 'folder_all'] entry below.
           ['docs/PatchworkPDF_Manual.pdf', 'cwd', 'file'],
           ['version.json', 'cwd', 'file'],
           ['LICENSE.txt', 'cwd', 'file'],
           ['run_me.bat', 'cwd', 'file'],
           ['locales', 'cwd', 'folder_all'],
           # OCR language data (Tesseract .traineddata packs + README) and the
           # script that restores/verifies them — the packs are ~11 MB binaries a
           # copied tree can lose, and every platform's build needs them.
           ['assets/tessdata', 'cwd', 'folder_all'],
           # The whole tools/ tree, not a hand-picked pair.  tools/run_tests.py
           # is the pre-flight gate EVERY platform's build script runs before it
           # packages anything (build_installer.bat, make_dmg.sh,
           # build_flatpak.sh) — and a missing script exits 2, so a distribution
           # without it aborts the build at step 1 reporting "tests failed".
           # The i18n_*.py tools regenerate locales/, which is shipped.  Naming
           # the folder means a tool added later ships without editing this list.
           ['tools', 'cwd', 'folder_all'],
           ['assets/icon.icns', 'cwd', 'file'],
           ['assets/icon-16.png', 'cwd', 'file'],
           ['assets/icon-32.png', 'cwd', 'file'],
           ['assets/icon-48.png', 'cwd', 'file'],
           ['assets/icon-64.png', 'cwd', 'file'],
           ['assets/icon-128.png', 'cwd', 'file'],
           ['assets/icon-256.png', 'cwd', 'file'],
           ['packaging', 'cwd', 'folder_all'],
           ['tests', 'cwd', 'folder_all'],
           # The suite's own config, so a plain `pytest` in the distribution root
           # collects the same set the build gate runs.
           ['pytest.ini', 'cwd', 'file'],
           ['README_BUILD.md', 'cwd', 'file'],
           ['docs/BUILD_AND_DEPLOY_WINDOWS.md', 'cwd', 'file'],
           ['docs/BUILD_AND_DEPLOY_MACOS.md', 'cwd', 'file'],
           ['docs/BUILD_AND_DEPLOY_LINUX.md', 'cwd', 'file'],
               ]

    def on_start(self, _event):
        threading.Thread(target=self.build_distribution, daemon=True).start()

    def build_distribution(self):
        print(self.cwd)
        release_pos = str(self.cwd).find('_v')
        if release_pos == -1:
            wx.CallAfter(self.log, "Error: Could not find release version in path", True)
            return
        release_version = str(self.cwd)[release_pos + 1:]
        dist_folder = self.cwd / f'patchworkPDF_source_{release_version}'

        os.makedirs(dist_folder, exist_ok=True)
        wx.CallAfter(self.log, f"Created/verified distribution folder: {dist_folder}")

        copy_count = 0
        self.line_counts = []
        wx.CallAfter(self.line_count_display.DeleteAllItems)

        for item_name, source_dir, item_type in self.payload:
            dest_path = dist_folder / item_name
            source_path = self.cwd / item_name if source_dir == 'cwd' else Path(source_dir) / item_name

            try:
                if item_type == 'folder_all':
                    if not source_path.exists():
                        wx.CallAfter(self.log, f"Warning: Source folder '{source_path}' does not exist", True)
                        continue
                    shutil.copytree(source_path, dest_path, dirs_exist_ok=True,
                                    ignore=_ignore)
                    # Count what was actually copied, not what is on disk — the
                    # ignored residue is not part of the distribution.
                    copied_files = 0
                    for root, _, files in os.walk(source_path):
                        for name in sorted(files):
                            path = Path(root) / name
                            if is_junk(path.relative_to(self.cwd)):
                                continue
                            copied_files += 1
                            # Count every source file in the tree, not just top-level ones.
                            self.record_line_count(path)
                    copy_count += copied_files
                    wx.CallAfter(self.log, f"Copied folder tree: {source_path} -> {dest_path} ({copied_files} files)")

                elif item_type == 'folder':
                    os.makedirs(dest_path, exist_ok=True)
                    wx.CallAfter(self.log, f"Created folder: {dest_path}")

                elif item_type == 'file':
                    if not source_path.is_file():
                        wx.CallAfter(self.log, f"Warning: File '{source_path}' not found", True)
                        continue
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source_path, dest_path)
                    copy_count += 1
                    wx.CallAfter(self.log, f"Copied file: {source_path}")
                    self.record_line_count(source_path)
                else:
                    wx.CallAfter(self.log, f"Unknown item type: {item_type}", True)
            except Exception as e:
                wx.CallAfter(self.log, f"Error copying {item_name}: {e}", True)

        wx.CallAfter(self.log, "-" * 60)
        wx.CallAfter(self.log, f"Total files copied: {copy_count}")
        wx.CallAfter(self.show_line_counts)

    # ---------------------------------------------------------------- counting

    def record_line_count(self, path):
        """Add `path` to the line-count table if it is a source file we report on."""
        language = COUNTED_SUFFIXES.get(path.suffix.lower())
        if not language:
            return
        try:
            display = str(path.relative_to(self.cwd))
        except ValueError:
            display = str(path)
        self.line_counts.append((display, language, self.count_code_lines(path, language)))

    @staticmethod
    def count_code_lines(path, language):
        """Count non-blank, non-comment lines, honouring the language's comment syntax."""
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return 0

        block = BLOCK_COMMENT[language]
        line_markers = LINE_COMMENT[language]
        count = 0
        in_block = False

        for line in text.splitlines():
            stripped = line.strip()

            if block:
                open_tok, close_tok = block
                while stripped:
                    if in_block:
                        end = stripped.find(close_tok)
                        if end == -1:
                            stripped = ""
                        else:
                            stripped = stripped[end + len(close_tok):].strip()
                            in_block = False
                    else:
                        start = stripped.find(open_tok)
                        if start == -1:
                            break
                        before = stripped[:start].strip()
                        rest = stripped[start + len(open_tok):]
                        end = rest.find(close_tok)
                        if end == -1:
                            in_block = True
                            stripped = before
                            break
                        stripped = (before + " " + rest[end + len(close_tok):]).strip()

            if not stripped:
                continue
            if line_markers and stripped.startswith(line_markers):
                continue
            count += 1

        return count

    def show_line_counts(self):
        """Render the collected counts grouped by language, with per-group and grand totals."""
        self.line_count_display.DeleteAllItems()
        if not self.line_counts:
            return

        grand_files = grand_lines = 0

        for language in COUNTED_SUFFIXES.values():
            rows = sorted((r for r in self.line_counts if r[1] == language), key=lambda r: r[0].lower())
            if not rows:
                continue
            for display, _, count in rows:
                self.add_line_count(display, language, count)
            subtotal = sum(r[2] for r in rows)
            self.add_line_count(f"— {language} total ({len(rows)} files)", "", subtotal)
            grand_files += len(rows)
            grand_lines += subtotal

        self.add_line_count(f"== ALL SOURCE ({grand_files} files)", "", grand_lines)
        self.log("-" * 60)
        self.log(f"Source lines counted: {grand_lines} across {grand_files} files")

    def add_line_count(self, filename, language, count):
        index = self.line_count_display.InsertItem(self.line_count_display.GetItemCount(), filename)
        self.line_count_display.SetItem(index, 1, language)
        self.line_count_display.SetItem(index, 2, str(count))


if __name__ == '__main__':
    app = wx.App(False)
    SourceDistributor(None)
    app.MainLoop()
