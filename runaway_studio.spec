# PyInstaller spec for Runaway Studio.
#
# Build locally:
#   pip install -r requirements.txt pyinstaller
#   pyinstaller runaway_studio.spec
#
# On Windows, drop ffmpeg.exe and ffprobe.exe into the repo root before
# building (same folder as this .spec) and they'll be bundled into the
# dist folder automatically -- that's exactly what the GitHub Actions
# release workflow (.github/workflows/release.yml) does on every tagged
# push, so a from-source build mirrors the official release.
#
# Output: dist/RunawayStudio/ (onedir build -- a folder you can zip and
# hand to anyone; RunawayStudio.exe inside it is the entry point).

import os

block_cipher = None
here = os.path.abspath(SPECPATH)

datas = [
    ("runaway_studio_icon_256.png", "."),
    ("runaway_studio_icon_64.png", "."),
    ("runaway_studio_icon_32.png", "."),
]

binaries = []
for exe_name in ("ffmpeg.exe", "ffprobe.exe"):
    p = os.path.join(here, exe_name)
    if os.path.exists(p):
        binaries.append((p, "."))

a = Analysis(
    ["runaway_studio.py"],
    pathex=[here],
    binaries=binaries,
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="RunawayStudio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=os.path.join(here, "assets", "icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="RunawayStudio",
)
