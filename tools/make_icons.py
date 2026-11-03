"""make_icons.py — generate the per-platform application icons from a master PNG.

Windows already uses assets/icon.ico.  This script produces the assets that the
macOS (.icns) and Linux (.png set, for the .desktop entry / Flatpak) builds need,
so they don't have to be checked in by hand.

    python tools/make_icons.py

Source: assets/icon_256.png (falls back to the largest frame in assets/icon.ico).
Outputs (into assets/):
    icon.icns
    icon-16.png icon-32.png icon-48.png icon-64.png icon-128.png icon-256.png
"""

import os
from PIL import Image

_HERE = os.path.dirname(os.path.abspath(__file__))
_ASSETS = os.path.join(os.path.dirname(_HERE), 'assets')

PNG_SIZES = [16, 32, 48, 64, 128, 256]


def _load_master():
    png = os.path.join(_ASSETS, 'icon_256.png')
    if os.path.isfile(png):
        return Image.open(png).convert('RGBA')
    ico = os.path.join(_ASSETS, 'icon.ico')
    im = Image.open(ico)
    # Pick the largest available frame.
    try:
        sizes = im.ico.sizes()
        im = im.ico.getimage(max(sizes))
    except Exception:
        pass
    return im.convert('RGBA')


def main():
    master = _load_master()
    # Square-pad if needed so all downscales stay centred and undistorted.
    if master.width != master.height:
        side = max(master.size)
        canvas = Image.new('RGBA', (side, side), (0, 0, 0, 0))
        canvas.paste(master, ((side - master.width) // 2, (side - master.height) // 2))
        master = canvas

    for s in PNG_SIZES:
        out = os.path.join(_ASSETS, f'icon-{s}.png')
        master.resize((s, s), Image.LANCZOS).save(out)
        print(f'[icons] wrote {out}')

    icns = os.path.join(_ASSETS, 'icon.icns')
    try:
        # Pillow's ICNS writer needs a reasonably large square source.
        master.resize((1024, 1024), Image.LANCZOS).save(icns, format='ICNS')
        print(f'[icons] wrote {icns}')
    except Exception as exc:
        print(f'[icons] WARNING: could not write {icns}: {exc}\n'
              f'         On macOS, generate it with iconutil instead (see docs).')


if __name__ == '__main__':
    main()
