# Runaway Studio

A fast, no-fuss video tool for trimming, speeding up, annotating, and
capturing frames from long battery thermal-runaway test recordings, built
so you never need a full video editor just to pull a 30-second clip out of
a multi-hour abuse-test camera file.

![Runaway Studio icon](runaway_studio_icon_256.png)

## Why

Thermal-runaway test cameras generate long, heavy recordings (multi-hour,
multi-gigabyte MP4/MKV files), but the moments worth sharing or archiving
are usually a few seconds long. Opening those files in a general-purpose
editor is slow and overkill. Runaway Studio is built specifically for this
workflow:

- **Trim** a clip by setting Start/End markers directly on a zoomable
  timeline, with frame-accurate nudging.
- **Speed up** a clip (useful for compressing a long slow onset into a
  watchable duration) without re-encoding anything outside the trimmed
  range.
- **Resize** on export (Original / 1080p / 720p / 480p) to keep shared
  clips small.
- **Burn in a text overlay** (timestamp, sample ID, test note) directly
  into the exported video, with a choice of font and color.
- **Capture a single frame** as a still image at any point in the
  timeline, without exporting a whole clip.
- Handles multi-hour files efficiently: nothing is ever fully decoded.
  Previewing seeks within the file, thumbnails are a handful of fast
  single-frame grabs, and export uses FFmpeg input-seeking so a clip
  starting two hours in starts encoding almost immediately — only the
  trimmed span is ever re-encoded.

## Download (Windows)

Grab the latest build from the [Releases page](../../releases) — look for
`RunawayStudio-<version>-windows.zip`. Unzip it anywhere and run
`RunawayStudio.exe`. FFmpeg is bundled inside, so there's nothing else to
install.

## Running from source

Needs Python 3.10+ and [FFmpeg](https://ffmpeg.org/download.html)/`ffprobe`
on your `PATH` (or placed next to the script).

```bash
pip install -r requirements.txt
python runaway_studio.py
```

You can also open a specific file directly: `python runaway_studio.py
my_test.mkv`.

## Building the Windows executable yourself

Every tagged push (`vX.Y.Z`) automatically builds a Windows `.exe` via
GitHub Actions (see `.github/workflows/release.yml`) and attaches it to a
new Release — that's the recommended way to cut a release. You can also
trigger the same workflow manually from the **Actions** tab for a one-off
test build (it uploads the `.exe` as a workflow artifact without creating
a Release).

To build locally on Windows instead:

```powershell
pip install -r requirements.txt
pip install pyinstaller
# place ffmpeg.exe and ffprobe.exe in this folder first if you want them bundled
pyinstaller runaway_studio.spec
```

The result is `dist/RunawayStudio/`, a self-contained folder you can zip
and share; `RunawayStudio.exe` inside it is the entry point.

## Status

Standalone today. A future version of this tool will also be embedded as
a page inside **Battery Insight Studio** (a separate internal app for
processing battery test data), where it'll optionally link to a test
sample's record for smarter file naming and, eventually, synced
video/data-plot playback. Those internal integrations live in a private
project and aren't part of this repo; this repo stays the standalone tool.

## License

[MIT](LICENSE).
