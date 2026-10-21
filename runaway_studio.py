"""
Battery Insight Studio -- Runaway Studio (standalone first, wired into
Safety Analysis later).

Trim, speed up, and shrink long test videos (MP4/MKV, no audio) without a
separate editor. Built for multi-hour files: nothing here ever decodes the
whole file. Previewing seeks within the file, thumbnails are a handful of
fast single-frame grabs, and export uses FFmpeg *input seeking* (-ss before
-i) so a clip starting 2 hours in begins encoding almost immediately, and
only the trimmed section is ever re-encoded.

Run it:   python runaway_studio.py [optional_video_file]
Needs:    PySide6, and ffmpeg + ffprobe on PATH (or next to this script).
          For the window/taskbar icon, keep runaway_studio_icon_*.png
          next to this script (optional -- falls back to Qt's default).

Wiring later (the page is self-contained): VideoToolPage exposes
`video_path`, `position_seconds()`, `seek(seconds)` and the
`positionChanged(float)` signal, so Safety Analysis can drive/follow it.
Internal class/module names (VideoToolPage, VideoToolWindow, video_tool.py)
are kept as-is even though the user-facing name is "Runaway Studio" --
only what's shown on screen changed, to avoid an unnecessary rename churn
through the rest of the codebase.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from concurrent.futures import ThreadPoolExecutor, as_completed

from PySide6.QtCore import (Qt, QEasingCurve, QPoint, QProcess, QPropertyAnimation, QRect,
                            QRectF, QSize, QThread, QTimer, QUrl, Signal)
from PySide6.QtGui import (QColor, QDesktopServices, QFont, QIcon, QImage, QKeySequence,
                           QPainter, QPen, QPixmap, QShortcut)
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QFileDialog, QFrame, QGraphicsDropShadowEffect,
    QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout,
    QWidget, QComboBox,
)

NAVY = "#0f2a52"
BORDER = "#e4e7ee"
SUB = "#667085"
TEXT = "#1a1d29"
GREEN = "#10b981"
RED = "#c0362c"

# On Windows, a console-less GUI app (how this is packaged) still gets a
# flashing console window for every child process it spawns unless told
# not to -- and thumbnails spawn one short-lived ffmpeg process per frame,
# so without this a video full of thumbnails flickers a terminal window
# open-and-closed dozens of times. No-op on other platforms.
_SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _base_dir():
    """Where bundled resources (ffmpeg.exe/ffprobe.exe, icons, fonts) live.
    When run as a plain script that's this file's own folder; when run as
    a PyInstaller build (onedir or onefile) that's sys._MEIPASS, the
    actual on-disk location PyInstaller extracted/placed everything --
    __file__ alone is not reliable once frozen, so this is the one helper
    every lookup below goes through instead of calling __file__ directly."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def _tool(name):
    """ffmpeg/ffprobe: next to this script first (bundled), then common
    subfolder names from how people actually unzip an FFmpeg release
    (the download's own bin/ folder, or one renamed to ffmpeg/), then
    PATH."""
    here = _base_dir()
    exe = name + (".exe" if os.name == "nt" else "")
    for cand in (os.path.join(here, exe),
                 os.path.join(here, "bin", exe),
                 os.path.join(here, "ffmpeg", exe),
                 os.path.join(here, "ffmpeg", "bin", exe)):
        if os.path.exists(cand):
            return cand
    return shutil.which(name)


FFMPEG = _tool("ffmpeg")
FFPROBE = _tool("ffprobe")

# --------------------------------------------------------------- helpers
def parse_timecode(text):
    """'1:02:03.5', '62:03', '3723.5', '1h2m3s' -> seconds, or None."""
    s = (text or "").strip().lower()
    if not s:
        return None
    m = re.fullmatch(r"(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+(?:\.\d+)?)s)?", s)
    if m and any(m.groups()):
        h, mi, sec = m.groups()
        return int(h or 0) * 3600 + int(mi or 0) * 60 + float(sec or 0)
    try:
        parts = [float(p) for p in s.split(":")]
    except ValueError:
        return None
    if not 1 <= len(parts) <= 3 or any(p < 0 for p in parts):
        return None
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def fmt_tc(sec, frac=True):
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:04.1f}" if frac else f"{h:02d}:{m:02d}:{int(s):02d}"


def fmt_len(sec):
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} h {m} min {s} s"
    if m:
        return f"{m} min {s} s"
    return f"{s} s"


def probe(path):
    """Duration/fps/size/codec via ffprobe -- reads headers only, so it is
    instant even on a multi-GB file."""
    if not FFPROBE:
        raise RuntimeError("ffprobe not found (install FFmpeg or place it next to this script).")
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json", "-show_format",
         "-show_streams", path],
        capture_output=True, text=True, timeout=60,
        creationflags=_SUBPROCESS_FLAGS)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "ffprobe failed")
    info = json.loads(out.stdout)
    vs = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not vs:
        raise RuntimeError("No video stream found in this file.")
    dur = info.get("format", {}).get("duration") or vs.get("duration")
    if dur is None:
        raise RuntimeError("Could not determine the video's duration.")
    num, _, den = (vs.get("avg_frame_rate") or "30/1").partition("/")
    try:
        fps = float(num) / float(den or 1) or 30.0
    except (ValueError, ZeroDivisionError):
        fps = 30.0
    return dict(duration=float(dur), fps=fps, width=int(vs.get("width", 0)),
                height=int(vs.get("height", 0)), codec=vs.get("codec_name", "?"),
                size=int(info.get("format", {}).get("size", 0) or 0))


QUALITY_CRF = {"High": 20, "Balanced": 24, "Small": 29}
SPEED_PRESETS = [0.5, 1, 1.5, 2, 2.5, 3, 3.5, 4]   # dropdown; any value 0.25-16 can be typed
SPEED_MIN, SPEED_MAX, SPEED_STEP = 0.25, 16.0, 0.5
RESOLUTIONS = {"Original": None, "1080p": 1080, "720p": 720, "480p": 480}
# Export can only ever produce one of these two containers (the Format
# toggle below). The wider set is just what we recognize as "someone
# clearly meant this to be a file extension" when they type one into the
# File name box -- anything in here but not in SUPPORTED_EXPORT_EXTS gets
# flagged as an error instead of silently exported as something broken.
SUPPORTED_EXPORT_EXTS = {"mp4", "mkv"}
KNOWN_VIDEO_EXTS = SUPPORTED_EXPORT_EXTS | {
    "mov", "avi", "webm", "m4v", "wmv", "flv", "mpg", "mpeg", "ts", "m2ts", "3gp"}
OVERLAY_SIZE_PRESETS = [12, 14, 18, 24, 32, 40, 48, 64, 68, 72]
OVERLAY_SIZE_DEFAULT = 40   # a legible starting point on real footage, not a tiny 12px caption
CAPTURE_FORMATS = ["PNG", "JPG"]
OVERLAY_FONTS = ["Arial", "Calibri", "Times New Roman", "Garamond"]
OVERLAY_COLORS = [("White", "#ffffff"), ("Black", "#000000"), ("Red", "#e5342a"),
                  ("Yellow", "#f5c518"), ("Dark Blue", "#0f2a52")]


# Windows ships these under C:\Windows\Fonts with predictable filenames;
# Garamond specifically often ISN'T there unless MS Office installed it,
# so that one can legitimately come back as "not found" on some machines.
_WIN_FONT_FILES = {
    "arial": ["arial.ttf", "Arial.ttf", "ARIAL.TTF"],
    "calibri": ["calibri.ttf", "Calibri.ttf", "CALIBRI.TTF"],
    "times new roman": ["times.ttf", "Times.ttf", "TIMES.TTF"],
    "garamond": ["GARA.TTF", "Garamond.ttf", "garamond.ttf"],
}
# Metrically-close equivalents actually installed on this Linux dev/test
# box, so the drawtext path can be exercised end-to-end here too -- not
# what ships on the user's Windows machine, just what lets us test.
_LINUX_FONT_FALLBACK = {
    "arial": "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "calibri": "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "times new roman": "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
    "garamond": None,
}


def _font_file(family="Arial"):
    """A real font FILE for ffmpeg's drawtext (fontfile=...), not just a
    family name -- many static Windows ffmpeg builds (the common gyan.dev
    "essentials" download) aren't built with libfontconfig, so font=Arial
    alone can silently fail to render. Returns None (caller should skip
    burning text in, not crash) if nothing usable is found."""
    key = (family or "Arial").strip().lower()
    candidates = []
    if os.name == "nt":
        windir = os.environ.get("WINDIR", r"C:\Windows")
        for name in _WIN_FONT_FILES.get(key, [f"{family}.ttf"]):
            candidates.append(os.path.join(windir, "Fonts", name))
    else:
        fb = _LINUX_FONT_FALLBACK.get(key)
        if fb:
            candidates.append(fb)
    here = _base_dir()
    candidates.append(os.path.join(here, "fonts", f"{key.replace(' ', '_')}.ttf"))
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def _escape_ffmpeg_path(path):
    """Escape a path for use inside an ffmpeg filtergraph option value
    (fontfile=/textfile=): backslashes to forward slashes, then escape
    characters the filtergraph parser treats as special."""
    p = path.replace("\\", "/")
    for ch in (":", "'", ","):
        p = p.replace(ch, "\\" + ch)
    return p


def _wrap_overlay_text(text, max_chars):
    """Wrap text to fit a box max_chars wide, keeping the user's own line
    breaks as paragraph boundaries."""
    max_chars = max(4, int(max_chars))
    lines = []
    for para in text.split("\n"):
        if not para.strip():
            lines.append("")
            continue
        lines.extend(textwrap.wrap(para, width=max_chars) or [""])
    return lines


def _is_dark_hex(hex6):
    try:
        r, g, b = int(hex6[0:2], 16), int(hex6[2:4], 16), int(hex6[4:6], 16)
    except (ValueError, IndexError):
        return False
    return (0.299 * r + 0.587 * g + 0.114 * b) < 128


def _scaled_dims(source_w, source_h, target_h):
    """Mirror ffmpeg's `scale=-2:min(h,ih)` so overlay pixel math lines up
    with what actually gets encoded."""
    if not target_h or source_h <= 0:
        return source_w, source_h
    h = min(target_h, source_h)
    w = round(source_w * h / source_h)
    if w % 2:
        w += 1
    return w, h


def build_export_cmd(src, dst, in_s, out_s, speed, height, quality, overlay=None, ffmpeg=None):
    """FFmpeg command for one trimmed/sped-up/downscaled, audio-free clip.

    -ss BEFORE -i = input seeking: jumps straight to the start point
    instead of decoding from 00:00, which is what makes a 3-hour source
    practical. -t placed BEFORE -i bounds the *source* span (as an output option it
    would count output time and, combined with a speed-up, read past the Out
    point); setpts then compresses that span by `speed`. scale never upscales (min(height, source height)).

    overlay (optional): {"text", "font_file", "color" (hex, e.g. "#ffffff"),
    "size_px" (relative to the SOURCE video's native height), "rel"
    (x,y,w,h fractions of the video frame), "source_w", "source_h"}.
    Burns the text in with drawtext, positioned/sized to match wherever
    the box was left in the UI. If a font file can't be resolved, the
    overlay is silently skipped (caller should warn the user) rather than
    failing the export. The temp text file it writes (drawtext reads text
    from a file so we never have to fight filtergraph string-escaping for
    arbitrary user text) is left at overlay["_tmp_path"] for the caller
    to delete once the export is done."""
    vf = []
    if speed != 1:
        vf.append(f"setpts=PTS/{speed:g}")
    if height:
        vf.append(f"scale=-2:min({height}\\,ih)")
    if overlay and overlay.get("text", "").strip() and overlay.get("font_file"):
        sw, sh = overlay["source_w"], overlay["source_h"]
        ow, oh = _scaled_dims(sw, sh, height) if height else (sw, sh)
        rx, ry, rw, rh = overlay["rel"]
        box_x, box_y = int(rx * ow), int(ry * oh)
        box_w, box_h = max(10, int(rw * ow)), max(10, int(rh * oh))
        size_px = max(6, round(overlay["size_px"] * oh / max(sh, 1)))
        lines = _wrap_overlay_text(overlay["text"], box_w / max(size_px * 0.58, 1))
        text_h = len(lines) * size_px * 1.3
        y = box_y + max(0, (box_h - text_h) / 2)
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
        tmp.write("\n".join(lines))
        tmp.close()
        overlay["_tmp_path"] = tmp.name
        color_hex = (overlay.get("color") or "#ffffff").lstrip("#")
        boxcolor = "white@0.45" if _is_dark_hex(color_hex) else "black@0.45"
        dt = ("drawtext="
              f"fontfile='{_escape_ffmpeg_path(overlay['font_file'])}':"
              f"textfile='{_escape_ffmpeg_path(tmp.name)}':"
              f"x={box_x}:y={int(y)}:fontsize={size_px}:fontcolor=0x{color_hex}:"
              f"box=1:boxcolor={boxcolor}:boxborderw=8:line_spacing=4")
        vf.append(dt)
    cmd = [ffmpeg or FFMPEG, "-y", "-hide_banner", "-nostdin",
           "-ss", f"{in_s:.3f}", "-t", f"{out_s - in_s:.3f}", "-i", src,
           "-an", "-sn", "-dn"]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    cmd += ["-c:v", "libx264", "-preset", "veryfast",
            "-crf", str(QUALITY_CRF[quality]), "-pix_fmt", "yuv420p"]
    if dst.lower().endswith(".mp4"):
        cmd += ["-movflags", "+faststart"]
    cmd += ["-progress", "pipe:1", "-nostats", dst]
    return cmd


def _contain_rect(container_w, container_h, content_w, content_h):
    """Centered rect of content_w x content_h scaled to fit inside the
    container, preserving aspect ratio -- the same placement QVideoWidget
    uses (KeepAspectRatio), so overlay math lines up with what's actually
    on screen (video may be letterboxed inside its widget)."""
    if content_w <= 0 or content_h <= 0 or container_w <= 0 or container_h <= 0:
        return QRect(0, 0, max(1, container_w), max(1, container_h))
    scale = min(container_w / content_w, container_h / content_h)
    w, h = max(1, int(content_w * scale)), max(1, int(content_h * scale))
    x, y = (container_w - w) // 2, (container_h - h) // 2
    return QRect(x, y, w, h)


# ------------------------------------------------------------ thumbnails
class ThumbWorker(QThread):
    """A few single-frame grabs spread across the file (input-seeking, so
    each is near-instant even deep into a long file). Grabs run a few at a
    time (ThreadPoolExecutor) rather than one after another -- each is its
    own ffmpeg process, and on Windows process start-up (antivirus scanning
    the exe, etc.) tends to dominate the wall-clock time for a short clip
    more than the actual seek+decode does, so overlapping them is what
    actually speeds up "video just loaded" perceived load time."""
    thumb = Signal(int, QImage)

    def __init__(self, path, duration, count, parent=None):
        super().__init__(parent)
        self.path, self.duration, self.count = path, duration, count
        self._stop = False

    def stop(self):
        self._stop = True

    def _grab(self, i):
        t = self.duration * (i + 0.5) / self.count
        try:
            r = subprocess.run(
                [FFMPEG, "-v", "error", "-ss", f"{t:.2f}", "-i", self.path,
                 "-frames:v", "1", "-vf", "scale=180:-2", "-f", "image2pipe",
                 "-vcodec", "png", "-"],
                capture_output=True, timeout=60,
                creationflags=_SUBPROCESS_FLAGS)
        except Exception:
            return i, None
        if self._stop or not r.stdout:
            return i, None
        img = QImage.fromData(r.stdout, "PNG")
        return i, (None if img.isNull() else img)

    def run(self):
        if not FFMPEG:
            return
        with ThreadPoolExecutor(max_workers=min(4, self.count)) as pool:
            futures = [pool.submit(self._grab, i) for i in range(self.count)]
            for fut in as_completed(futures):
                if self._stop:
                    break
                i, img = fut.result()
                if img is not None and not self._stop:
                    self.thumb.emit(i, img)


# -------------------------------------------------------------- timeline
class Timeline(QWidget):
    """Thumbnail strip + In/Out region + playhead. Click or drag inside the
    strip to seek; drag the green In/Out edges to adjust them; drag the red
    flag above the strip to move the playhead specifically -- it has its
    own dedicated grab zone up top so it never fights with an In/Out edge
    sitting at (or near) the same spot. Zoomable to a window around the
    playhead, or to an explicit range (e.g. the current clip), so a
    3-hour file can still be scrubbed precisely."""
    seekRequested = Signal(float)
    inChanged = Signal(float)
    outChanged = Signal(float)
    FLAG = 14   # header strip above the thumbnails, reserved for the playhead flag

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(74 + self.FLAG)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.duration = 0.0
        self.pos = 0.0
        self.t_in = None
        self.t_out = None
        self.thumbs = {}
        self.thumb_count = 10
        self.zoom = None          # None = full file; "range" = explicit view; else half-window seconds
        self._view = (0.0, 1.0)
        self._drag = None
        self.setMouseTracking(True)

    def set_duration(self, d):
        self.duration = d
        self.t_in = self.t_out = None
        self.thumbs = {}
        self.zoom = None
        self._recompute_view()
        self.update()

    def set_thumb(self, i, img):
        self.thumbs[i] = QPixmap.fromImage(img)
        self.update()

    def set_pos(self, s):
        self.pos = s
        if self.zoom and self.zoom != "range" and not (self._view[0] <= s <= self._view[1]):
            self._recompute_view()
        self.update()

    def set_zoom(self, half_window):
        self.zoom = half_window
        self._recompute_view()
        self.update()

    def set_view_range(self, a, b):
        """Zoom to an explicit [a, b] window (e.g. the current clip plus
        padding) instead of a half-window centered on the playhead."""
        if self.duration <= 0:
            return
        a = max(0.0, a)
        b = min(self.duration, max(b, a + 0.5))
        self.zoom = "range"
        self._view = (a, b)
        self.update()

    def _recompute_view(self):
        if self.zoom == "range":
            return  # explicit range set via set_view_range(); leave it alone
        if not self.zoom or self.duration <= 0:
            self._view = (0.0, max(self.duration, 1.0))
            return
        w = min(self.zoom * 2, self.duration)
        start = min(max(0.0, self.pos - w / 2), self.duration - w)
        self._view = (start, start + w)

    # coordinate mapping
    def _x(self, t):
        a, b = self._view
        return (t - a) / (b - a) * self.width()

    def _t(self, x):
        a, b = self._view
        return min(max(a + x / max(self.width(), 1) * (b - a), 0.0), self.duration)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        top = self.FLAG
        strip_h = self.height() - 20 - top
        p.fillRect(self.rect(), QColor("#ffffff"))
        strip = QRectF(0, top, self.width(), strip_h)
        p.fillRect(strip, QColor("#e4e7ee"))
        a, b = self._view
        # thumbnails only make sense in the full-file view
        if self.zoom is None and self.thumbs and self.duration > 0:
            w = self.width() / self.thumb_count
            for i, pm in self.thumbs.items():
                p.drawPixmap(QRectF(i * w, top, w, strip_h).toRect(), pm)
        if self.duration > 0:
            # dim outside In/Out
            if self.t_in is not None and self.t_out is not None:
                xi, xo = self._x(self.t_in), self._x(self.t_out)
                dim = QColor(15, 42, 82, 110)
                p.fillRect(QRectF(0, top, max(0, xi), strip_h), dim)
                p.fillRect(QRectF(min(self.width(), xo), top, self.width(), strip_h), dim)
                p.setPen(QPen(QColor(GREEN), 3))
                p.setBrush(Qt.NoBrush)
                p.drawRect(QRectF(xi, top + 1.5, xo - xi, strip_h - 3))
            elif self.t_in is not None:
                p.setPen(QPen(QColor(GREEN), 3))
                p.drawLine(int(self._x(self.t_in)), top, int(self._x(self.t_in)), int(top + strip_h))
            # ticks / labels
            span = b - a
            step = next((s for s in (1, 5, 10, 30, 60, 300, 600, 1800, 3600, 7200)
                         if span / s <= max(self.width() / 110, 1)), 7200)
            p.setPen(QColor(SUB))
            f = QFont(); f.setPointSize(8); f.setFamily("Consolas")
            p.setFont(f)
            t = (int(a // step) + 1) * step if a % step else a
            last_label_x = -999
            while t <= b:
                x = self._x(t)
                p.drawLine(int(x), int(top + strip_h), int(x), int(top + strip_h + 4))
                if x - last_label_x > 70:
                    p.drawText(int(x) + 3, self.height() - 3, fmt_tc(t, frac=False))
                    last_label_x = x
                t += step
            # playhead: a thin line through the strip, topped by a distinct
            # "flag" handle above the strip that's always clear of the
            # green In/Out edges, even when they land on the same spot.
            x = self._x(self.pos)
            p.setPen(QPen(QColor(RED), 2))
            p.drawLine(int(x), top, int(x), int(top + strip_h))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(RED))
            p.drawRoundedRect(QRectF(x - 5, 0, 10, top - 2), 2, 2)
            path_tip = [QRectF(x - 1.5, top - 3, 3, 4)]
            for r in path_tip:
                p.drawRect(r)
        p.end()

    def _near_edge(self, x):
        for name, t in (("in", self.t_in), ("out", self.t_out)):
            if t is not None and abs(self._x(t) - x) <= 6:
                return name
        return None

    def _near_playhead_flag(self, pos):
        return pos.y() <= self.FLAG + 2 and abs(pos.x() - self._x(self.pos)) <= 9

    def mousePressEvent(self, e):
        if self.duration <= 0:
            return
        pos = e.position()
        if self._near_playhead_flag(pos):
            self._drag = "playhead"
        else:
            self._drag = self._near_edge(pos.x()) or "seek"
        self.mouseMoveEvent(e)

    def mouseMoveEvent(self, e):
        pos = e.position()
        x = pos.x()
        if self._drag is None:
            if self._near_playhead_flag(pos):
                self.setCursor(Qt.SizeHorCursor)
            else:
                self.setCursor(Qt.SizeHorCursor if self._near_edge(x) else Qt.PointingHandCursor)
            return
        # Qt keeps delivering move events (with coordinates outside the
        # widget) once a drag starts, even after the mouse leaves it --
        # e.g. dragging the playhead flag past the right edge while
        # zoomed into a clip. Without clamping, the computed x maps to a
        # pixel outside the widget's own width, so the flag itself gets
        # drawn off the visible strip ("goes out of the screen"). Clamp
        # to the widget bounds so a drag past the edge just pins to
        # whatever time is at that edge of the current (possibly zoomed) view.
        x = min(max(x, 0.0), float(self.width()))
        t = self._t(x)
        if self._drag in ("seek", "playhead"):
            self.seekRequested.emit(t)
        elif self._drag == "in":
            self.inChanged.emit(t)
        else:
            self.outChanged.emit(t)

    def mouseReleaseEvent(self, _):
        self._drag = None


# ------------------------------------------------------------- text overlay
class OverlayBox(QWidget):
    """Draggable, resizable caption box drawn on top of the video preview.
    Position/size live as fractions (`rel` = x,y,w,h, 0..1) of the
    *displayed video rect* -- not the raw widget -- so they survive window
    resizes and map directly onto the real exported/captured pixel size,
    whatever that ends up being."""
    changed = Signal()
    HIT = 8
    MIN_W, MIN_H = 40, 24

    def __init__(self, parent=None):
        super().__init__(parent)
        self.text = ""
        self.font_family = "Arial"
        self.color = "#ffffff"
        self.size_px = OVERLAY_SIZE_DEFAULT  # relative to the SOURCE video's native height
        self.rel = [0.12, 0.68, 0.5, 0.18]
        self._display_rect = QRect(0, 0, 1, 1)
        self._source_h = 720
        self._drag = None
        self._drag_from = None
        self._geo_from = None
        self.setMouseTracking(True)
        self.setCursor(Qt.SizeAllCursor)
        self.hide()

    def set_source_height(self, h):
        self._source_h = max(1, h)
        self.update()

    def set_text(self, t):
        self.text = t
        self.update()

    def set_font(self, family, size_px):
        self.font_family, self.size_px = family, max(1, size_px)
        self.update()

    def set_color(self, hex_color):
        self.color = hex_color
        self.update()

    def relayout(self, display_rect):
        """display_rect: QRect of the visible video content (letterboxing
        excluded) in this widget's parent's coordinates."""
        self._display_rect = display_rect
        x, y, w, h = self.rel
        geo = QRect(display_rect.x() + int(x * display_rect.width()),
                    display_rect.y() + int(y * display_rect.height()),
                    max(self.MIN_W, int(w * display_rect.width())),
                    max(self.MIN_H, int(h * display_rect.height())))
        self.setGeometry(geo)

    def _sync_rel_from_geometry(self):
        dr = self._display_rect
        if dr.width() <= 0 or dr.height() <= 0:
            return
        g = self.geometry()
        self.rel = [(g.x() - dr.x()) / dr.width(), (g.y() - dr.y()) / dr.height(),
                    g.width() / dr.width(), g.height() / dr.height()]
        self.changed.emit()

    def _display_px(self):
        if self._source_h <= 0 or self._display_rect.height() <= 0:
            return max(8, self.size_px)
        return max(6, round(self.size_px * self._display_rect.height() / self._source_h))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # editor-only selection chrome (dashed outline + faint fill) -- never exported
        p.setPen(QPen(QColor("#ffd166"), 1.5, Qt.DashLine))
        p.setBrush(QColor(15, 42, 82, 60))
        p.drawRect(self.rect().adjusted(1, 1, -2, -2))
        f = QFont(self.font_family)
        f.setPixelSize(self._display_px())
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(self.color))
        p.drawText(self.rect().adjusted(6, 4, -6, -4), Qt.AlignCenter | Qt.TextWordWrap,
                   self.text or "Your text here")
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#ffffff"))
        for hx, hy in self._handles().values():
            p.drawRect(QRectF(hx - 4, hy - 4, 8, 8))
        p.end()

    def _handles(self):
        w, h = self.width(), self.height()
        return {"nw": (0, 0), "n": (w / 2, 0), "ne": (w, 0),
                "w": (0, h / 2), "e": (w, h / 2),
                "sw": (0, h), "s": (w / 2, h), "se": (w, h)}

    def _hit(self, pos):
        for name, (hx, hy) in self._handles().items():
            if abs(pos.x() - hx) <= self.HIT and abs(pos.y() - hy) <= self.HIT:
                return name
        inner = self.rect().adjusted(self.HIT, self.HIT, -self.HIT, -self.HIT)
        if inner.contains(int(pos.x()), int(pos.y())):
            return "move"
        return None

    def mousePressEvent(self, e):
        self._drag = self._hit(e.position()) or "move"
        self._drag_from = e.globalPosition().toPoint()
        self._geo_from = QRect(self.geometry())

    def mouseMoveEvent(self, e):
        if self._drag_from is None or e.buttons() == Qt.NoButton:
            h = self._hit(e.position())
            cursors = {"nw": Qt.SizeFDiagCursor, "se": Qt.SizeFDiagCursor,
                       "ne": Qt.SizeBDiagCursor, "sw": Qt.SizeBDiagCursor,
                       "n": Qt.SizeVerCursor, "s": Qt.SizeVerCursor,
                       "e": Qt.SizeHorCursor, "w": Qt.SizeHorCursor,
                       "move": Qt.SizeAllCursor}
            self.setCursor(cursors.get(h, Qt.SizeAllCursor))
            return
        d = e.globalPosition().toPoint() - self._drag_from
        g = QRect(self._geo_from)
        dr = self._display_rect
        if self._drag == "move":
            g.translate(d.x(), d.y())
        else:
            if "n" in self._drag:
                g.setTop(min(g.top() + d.y(), g.bottom() - self.MIN_H))
            if "s" in self._drag:
                g.setBottom(max(g.bottom() + d.y(), g.top() + self.MIN_H))
            if "w" in self._drag:
                g.setLeft(min(g.left() + d.x(), g.right() - self.MIN_W))
            if "e" in self._drag:
                g.setRight(max(g.right() + d.x(), g.left() + self.MIN_W))
        if dr.width() > 0 and dr.height() > 0:
            if g.width() > dr.width():
                g.setWidth(dr.width())
            if g.height() > dr.height():
                g.setHeight(dr.height())
            if g.left() < dr.left():
                g.moveLeft(dr.left())
            if g.top() < dr.top():
                g.moveTop(dr.top())
            if g.right() > dr.right():
                g.moveRight(dr.right())
            if g.bottom() > dr.bottom():
                g.moveBottom(dr.bottom())
        self.setGeometry(g)
        self._sync_rel_from_geometry()

    def mouseReleaseEvent(self, _):
        self._drag = None
        self._drag_from = None


class VideoSurface(QGraphicsView):
    """The video picture, rendered via QGraphicsVideoItem instead of
    QVideoWidget. QVideoWidget can use a native, OS-level video surface on
    some platforms/backends (notably common on Windows) that paints
    *outside* Qt's normal widget stacking -- it sits visually above every
    sibling widget regardless of raise()/z-order, which is exactly what
    made the Capture-frame button and the text-overlay box invisible (but
    still clickable, since Qt's own input routing doesn't know about that
    native surface). QGraphicsVideoItem is composited through the regular
    Qt scene graph instead, so ordinary sibling widgets placed on top of
    this view actually show up on top."""

    def __init__(self, parent=None):
        # Keep a Python-side reference to the scene (self._scene) -- without
        # it, nothing but QGraphicsView's internal C++ pointer keeps the
        # QGraphicsScene alive, and PySide6 was garbage-collecting the
        # Python wrapper once __init__ returned, which took the
        # QGraphicsVideoItem down with it ("already deleted" on first use).
        self._scene = QGraphicsScene()
        super().__init__(self._scene, parent)
        self.setStyleSheet("background:#000;border:none;")
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scene.setBackgroundBrush(QColor("#000000"))
        self.item = QGraphicsVideoItem()
        self._scene.addItem(self.item)
        self.item.nativeSizeChanged.connect(lambda *_: self._refit())

    def _refit(self):
        sz = self.item.nativeSize()
        if sz.width() > 0 and sz.height() > 0:
            self.item.setSize(sz)
            self.setSceneRect(self.item.boundingRect())
        self.fitInView(self.item, Qt.KeepAspectRatio)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._refit()


class VideoStage(QWidget):
    """Hosts the video surface plus layered children (the Capture-frame
    button and the text-overlay box) positioned by absolute geometry so
    they float on top of the video regardless of window size."""

    def __init__(self, video, capture_btn, overlay, parent=None):
        super().__init__(parent)
        self.video = video
        video.setParent(self)
        self.capture_btn = capture_btn
        capture_btn.setParent(self)
        self.overlay = overlay
        overlay.setParent(self)
        self.capture_btn.raise_()
        self.overlay.raise_()
        self.source_wh = (16, 9)

    def set_source_size(self, w, h):
        self.source_wh = (w, h) if w and h else (16, 9)
        self._relayout()

    def displayed_rect(self):
        w, h = self.source_wh
        return _contain_rect(self.width(), self.height(), w, h)

    def _relayout(self):
        self.overlay.relayout(self.displayed_rect())

    def resizeEvent(self, e):
        self.video.setGeometry(self.rect())
        bw = self.capture_btn.sizeHint().width() + 6
        bh = self.capture_btn.sizeHint().height()
        self.capture_btn.move(max(0, self.width() - bw - 10), 10)
        self._relayout()
        super().resizeEvent(e)


# ----------------------------------------------------- collapsible section
class CollapsibleSection(QFrame):
    """A card with a clickable header that shows/hides its body, and an
    optional one-line "peek" summary on the header so a collapsed section
    still tells you what it's set to."""

    def __init__(self, title, start_open=True, parent=None):
        super().__init__(parent)
        self.setObjectName("sect")
        self.setStyleSheet(f"QFrame#sect{{background:#fff;border:1px solid {BORDER};border-radius:10px;}}")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.head = QWidget()
        self.head.setCursor(Qt.PointingHandCursor)
        hl = QHBoxLayout(self.head)
        hl.setContentsMargins(14, 11, 14, 11)
        hl.setSpacing(8)
        t = QLabel(title.upper())
        t.setStyleSheet(f"color:{SUB};font-size:11.5px;font-weight:700;border:none;")
        self.peek = QLabel("")
        self.peek.setStyleSheet(f"color:{TEXT};font-size:11.5px;font-weight:600;border:none;")
        self.chev = QLabel("")
        self.chev.setStyleSheet(f"color:{SUB};font-size:11px;border:none;")
        hl.addWidget(t)
        hl.addStretch()
        hl.addWidget(self.peek)
        hl.addWidget(self.chev)

        self.body = QWidget()
        self.body_lay = QVBoxLayout(self.body)
        self.body_lay.setContentsMargins(14, 2, 14, 16)
        self.body_lay.setSpacing(8)

        outer.addWidget(self.head)
        outer.addWidget(self.body)
        self.head.mousePressEvent = lambda _e: self.toggle()

        self._open = start_open
        self.body.setVisible(start_open)
        self.chev.setText("▾" if start_open else "▸")

    def toggle(self):
        self._open = not self._open
        self.body.setVisible(self._open)
        self.chev.setText("▾" if self._open else "▸")

    def set_peek(self, text):
        self.peek.setText(text)


# ------------------------------------------------------------------ page
BTN = (f"QPushButton{{background:#fff;color:{NAVY};border:1.5px solid {NAVY};"
       f"border-radius:6px;padding:6px 12px;font-weight:600;}}"
       f"QPushButton:hover{{background:#eef3ff;}}"
       f"QPushButton:disabled{{color:#98a2b3;border-color:#d0d5dd;}}"
       f"QPushButton:checked{{background:{NAVY};color:#fff;}}")
PRIMARY = (f"QPushButton{{background:{NAVY};color:#fff;border:1.5px solid {NAVY};"
           f"border-radius:6px;padding:9px 12px;font-weight:700;}}"
           f"QPushButton:disabled{{background:#98a2b3;border-color:#98a2b3;}}")
INPUT = (f"QLineEdit{{background:#fff;color:{TEXT};border:1.5px solid #d0d5dd;"
         f"border-radius:6px;padding:6px 8px;font-family:Consolas,monospace;}}"
         f"QLineEdit:focus{{border-color:{NAVY};}}")
INPUT_MULTI = (f"QPlainTextEdit{{background:#fff;color:{TEXT};border:1.5px solid #d0d5dd;"
               f"border-radius:6px;padding:6px 8px;font-family:Segoe UI,Arial,sans-serif;}}"
               f"QPlainTextEdit:focus{{border-color:{NAVY};}}")
INPUT_READONLY = (f"QLineEdit{{background:#f6f7f9;color:{SUB};border:1.5px solid #d0d5dd;"
                  f"border-radius:6px;padding:6px 8px;font-family:Consolas,monospace;}}")
# A visibly-wrong state for the File name box -- a typed extension this
# app can't export to (e.g. ".mov") -- separate from INPUT's normal/focus
# styling, with a red border instead of the usual navy-on-focus one.
INPUT_ERROR = (f"QLineEdit{{background:#fff;color:{TEXT};border:1.5px solid {RED};"
               f"border-radius:6px;padding:6px 8px;font-family:Consolas,monospace;}}"
               f"QLineEdit:focus{{border-color:{RED};}}")


def _card(title=None):
    f = QFrame()
    f.setStyleSheet(f"QFrame#card{{background:#fff;border:1px solid {BORDER};border-radius:10px;}}")
    f.setObjectName("card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(14, 12, 14, 14)
    lay.setSpacing(6)
    if title:
        t = QLabel(title.upper())
        t.setStyleSheet(f"color:{SUB};font-size:11px;font-weight:700;border:none;")
        lay.addWidget(t)
    return f, lay


# ------------------------------------------------------------ about / help
APP_VERSION = "1.0.0"
AUTHOR_NAME = "Dhev Kannan"
AUTHOR_LINKEDIN_URL = "https://www.linkedin.com/in/dhevathirajan/"
AUTHOR_GITHUB_URL = "https://github.com/dhev-rk"
SIBLING_PROJECT_LABEL = "Battery Data Visualizer"
SIBLING_PROJECT_URL = "https://github.com/dhev-rk/battery-data-visualizer"
CONTACT_EMAIL = "dhev.rk@gmail.com"

ABOUT_BODY = (
    "Built for battery safety testing — pulling a usable clip or still "
    "frame out of long, heavy thermal-runaway and other abuse-test camera "
    "recordings without needing a full video editor. Trims by seeking "
    "straight to the span you want, can speed up a slow test to a "
    "watchable length, resizes on export, burns in a timestamp or note, "
    "and grabs single still frames.\n\n"
    "Not limited to battery testing — works for any long recording you "
    "just need a short, resized, labeled clip out of."
)
HELP_BODY = (
    "Have a question, found something broken, or want to suggest a "
    "feature? I'd like to hear about it."
)


def _rich_link(text, url, color=NAVY, size="12.5px"):
    """A clickable hyperlink rendered as a QLabel (opens in the system
    browser / default mail client for mailto: links)."""
    lbl = QLabel(f'<a href="{url}" style="color:{color};">{text}</a>')
    lbl.setOpenExternalLinks(True)
    lbl.setStyleSheet(f"font-size:{size};border:none;")
    lbl.setCursor(Qt.PointingHandCursor)
    return lbl


def _build_about_card():
    card, lay = _card()
    body = QLabel(ABOUT_BODY)
    body.setWordWrap(True)
    body.setStyleSheet(f"color:{TEXT};font-size:12px;border:none;")
    lay.addWidget(body)
    div = QFrame(); div.setFrameShape(QFrame.HLine); div.setStyleSheet(f"color:{BORDER};")
    lay.addWidget(div)
    ch = QLabel("CREATED BY")
    ch.setStyleSheet(f"color:{SUB};font-size:10.5px;font-weight:700;border:none;")
    lay.addWidget(ch)
    name = QLabel(AUTHOR_NAME)
    name.setStyleSheet(f"color:{TEXT};font-size:12.5px;font-weight:700;border:none;")
    lay.addWidget(name)
    links = QHBoxLayout(); links.setSpacing(14)
    links.addWidget(_rich_link("LinkedIn", AUTHOR_LINKEDIN_URL))
    links.addWidget(_rich_link("GitHub", AUTHOR_GITHUB_URL))
    links.addStretch()
    lay.addLayout(links)
    also = QLabel(f'Also see: <a href="{SIBLING_PROJECT_URL}" style="color:{NAVY};">{SIBLING_PROJECT_LABEL}</a>')
    also.setOpenExternalLinks(True)
    also.setWordWrap(True)
    also.setStyleSheet(f"color:{SUB};font-size:11px;border:none;")
    lay.addWidget(also)
    lic = QLabel(f"Version {APP_VERSION} · MIT License")
    lic.setStyleSheet(f"color:{SUB};font-size:10.5px;border:none;")
    lay.addWidget(lic)
    lay.addStretch()
    return card


def _build_help_card():
    card, lay = _card()
    body = QLabel(HELP_BODY)
    body.setWordWrap(True)
    body.setStyleSheet(f"color:{TEXT};font-size:12px;border:none;")
    lay.addWidget(body)
    eh = QLabel("EMAIL")
    eh.setStyleSheet(f"color:{SUB};font-size:10.5px;font-weight:700;border:none;margin-top:4px;")
    lay.addWidget(eh)
    er = QHBoxLayout(); er.setSpacing(8)
    er.addWidget(_rich_link(CONTACT_EMAIL, f"mailto:{CONTACT_EMAIL}"))
    copy_btn = QPushButton("Copy")
    copy_btn.setStyleSheet(BTN)
    copy_btn.setFixedWidth(55)
    copy_btn.setCursor(Qt.PointingHandCursor)
    copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(CONTACT_EMAIL))
    er.addWidget(copy_btn)
    er.addStretch()
    lay.addLayout(er)
    gh = QLabel("GITHUB")
    gh.setStyleSheet(f"color:{SUB};font-size:10.5px;font-weight:700;border:none;margin-top:4px;")
    lay.addWidget(gh)
    lay.addWidget(_rich_link(AUTHOR_GITHUB_URL, AUTHOR_GITHUB_URL))
    lay.addStretch()
    return card


def _seg(options, default, parent_lay, on_change=None, max_width=310):
    """A row of mutually-exclusive toggle buttons (Resolution/Quality/Format
    etc). Packs buttons into one row at their own natural (readable) width
    and starts a new row once they'd stop fitting, instead of compressing
    every option into a single fixed-width row -- which is what made these
    "squished and not visible to read at all" on the user's real machine,
    especially with a longer label like "Times New Roman" or at a
    different font/DPI. The right-hand panel has a fixed width, so this is
    computed once at build time rather than re-flowed on resize (a nested
    Qt height-for-width layout here turned out to be unreliable: it left
    the next field overlapping the wrapped row)."""
    grp = QButtonGroup(parent_lay)
    grp.setExclusive(True)
    btns = {}
    buttons = []
    for opt in options:
        b = QPushButton(str(opt))
        b.setCheckable(True)
        b.setStyleSheet(BTN)
        b.setCursor(Qt.PointingHandCursor)
        grp.addButton(b)
        btns[opt] = b
        buttons.append(b)
        if opt == default:
            b.setChecked(True)
    col = QVBoxLayout()
    col.setSpacing(6)
    row = QHBoxLayout(); row.setSpacing(6)
    used = 0
    for b in buttons:
        w = b.sizeHint().width()
        if used and used + 6 + w > max_width:
            row.addStretch()
            col.addLayout(row)
            row = QHBoxLayout(); row.setSpacing(6)
            used = 0
        row.addWidget(b)
        used += w + (6 if used else 0)
    row.addStretch()
    col.addLayout(row)
    parent_lay.addLayout(col)
    if on_change:
        grp.buttonClicked.connect(lambda _b: on_change())
    return grp, btns


def _pause_icon(color="#ffffff", bar_w=4, bar_h=13, gap=3):
    """Two small evenly-spaced bars, drawn directly -- rather than relying
    on a unicode glyph like "⏸" or "❚❚", whose bar count/spacing is
    inconsistent (or missing entirely) depending on the font actually
    installed, so Pause always looks the same everywhere."""
    pm = QPixmap(bar_w * 2 + gap, bar_h)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    p.drawRoundedRect(0, 0, bar_w, bar_h, 1, 1)
    p.drawRoundedRect(bar_w + gap, 0, bar_w, bar_h, 1, 1)
    p.end()
    return QIcon(pm), QSize(bar_w * 2 + gap, bar_h)


def _spinner(on_up, on_down, tip_up="", tip_down=""):
    """A compact vertical ▲/▼ pair (an up/down "spinner"), used for the
    Clip In/Out nudge controls so it's visually obvious they increment /
    decrement, rather than the old plain "-"/"+" row."""
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    up = QPushButton("▲")
    down = QPushButton("▼")
    for b, tip in ((up, tip_up), (down, tip_down)):
        # min/max-width in a Qt stylesheet sizes the CONTENT box; the 1px
        # border is then added on top of that, outside it. The container
        # below was previously the same fixed width as the buttons'
        # min/max-width, so the border's right-hand pixel had nowhere to
        # go and got clipped -- hence "cut off on the right side". Size
        # the content a couple of px narrower than the container instead.
        b.setStyleSheet(
            f"QPushButton{{background:#fff;color:{NAVY};border:1px solid {NAVY};"
            "padding:0px;font-size:8px;min-width:20px;max-width:20px;"
            "min-height:13px;max-height:13px;}"
            "QPushButton:hover{background:#eef3ff;}")
        b.setCursor(Qt.PointingHandCursor)
        if tip:
            b.setToolTip(tip)
        lay.addWidget(b)
    up.clicked.connect(lambda: on_up())
    down.clicked.connect(lambda: on_down())
    w.setFixedWidth(26)
    return w


class VideoToolPage(QWidget):
    positionChanged = Signal(float)      # seconds, for later plot sync

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("VideoToolPage")
        self.setStyleSheet(f"#VideoToolPage{{background:#f5f7fb;}} QLabel{{background:transparent;color:{TEXT};}}")
        self.video_path = None
        self.info = None
        self._pos = 0.0
        self.t_in = None
        self.t_out = None
        self._thumbs = None
        self._proc = None
        self._overlay_tmp = None
        self._out_len = 0.0
        self._log_tail = []
        self._preview_ok = True

        root = QHBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(14)

        # ----- left: preview + timeline + controls
        left = QVBoxLayout()
        left.setSpacing(10)

        topbar = QFrame()
        topbar.setObjectName("topbar")
        topbar.setStyleSheet(f"QFrame#topbar{{background:{NAVY};border-radius:10px;}}")
        tb = QHBoxLayout(topbar)
        tb.setContentsMargins(12, 9, 12, 9)
        tb.setSpacing(14)
        self.open_btn = QPushButton("Import Video")
        self._open_btn_style_empty = (
            f"QPushButton{{background:{GREEN};color:#fff;border:1.5px solid {GREEN};"
            "border-radius:6px;padding:7px 14px;font-weight:700;}"
            "QPushButton:hover{background:#0ea371;}")
        # Green before anything is loaded (an inviting "go" color), then
        # switches to red once a video is loaded -- a clear at-a-glance
        # state change either way, without having to read the (sometimes
        # long) filename next to it.
        self._open_btn_style_loaded = (
            f"QPushButton{{background:{RED};color:#fff;border:1.5px solid {RED};"
            "border-radius:6px;padding:7px 14px;font-weight:700;}"
            "QPushButton:hover{background:#a52d23;}")
        self.open_btn.setStyleSheet(self._open_btn_style_empty)
        self.open_btn.setCursor(Qt.PointingHandCursor)
        self.open_btn.clicked.connect(self.open_dialog)
        info_col = QVBoxLayout()
        info_col.setSpacing(3)
        self.file_label = QLabel("No video loaded.")
        self.file_label.setStyleSheet("color:#fff;font-size:13px;font-weight:600;border:none;")
        self.folder_label = QLabel("")
        self.folder_label.setStyleSheet("color:#fff;font-size:11.5px;border:none;")
        info_col.addWidget(self.file_label)
        info_col.addWidget(self.folder_label)
        tb.addWidget(self.open_btn)
        tb.addLayout(info_col, 1)

        # About / Help -- far right of the top bar, always visible. Each
        # opens the slide-in info panel (built below, after the right-hand
        # column exists) over the Clip/Export controls rather than a popup.
        def _topbar_link(text):
            lb = QLabel(f'<a href="#" style="color:#fff;text-decoration:none;">{text}</a>')
            lb.setStyleSheet("font-size:12.5px;font-weight:700;border:none;")
            lb.setCursor(Qt.PointingHandCursor)
            return lb

        self.about_link = _topbar_link("About")
        self.help_link = _topbar_link("Help")
        tb_sep = QLabel("|")
        tb_sep.setStyleSheet("color:rgba(255,255,255,0.4);border:none;")
        self.about_link.linkActivated.connect(lambda _: self._show_info("about"))
        self.help_link.linkActivated.connect(lambda _: self._show_info("help"))
        tb.addWidget(self.about_link)
        tb.addWidget(tb_sep)
        tb.addWidget(self.help_link)
        left.addWidget(topbar)

        card, cl = _card()
        self.video = VideoSurface()
        self.capture_btn = QPushButton("⏺ Capture frame")
        self.capture_btn.setStyleSheet(
            f"QPushButton{{background:rgba(15,42,82,0.85);color:#fff;border:1.5px solid #fff;"
            "border-radius:8px;padding:6px 11px;font-weight:700;font-size:11.5px;}"
            "QPushButton:hover{background:rgba(15,42,82,1.0);}"
            "QPushButton:disabled{background:rgba(120,120,120,0.6);border-color:rgba(255,255,255,0.4);}")
        self.capture_btn.setCursor(Qt.PointingHandCursor)
        self.capture_btn.clicked.connect(self.capture_frame)
        self.overlay_box = OverlayBox()
        self.stage = VideoStage(self.video, self.capture_btn, self.overlay_box)
        # A taller floor for the preview, and (below) one fewer row in the
        # card overall -- merging the Go-to boxes onto the Clip Start/Clip
        # End row -- so the video gets noticeably more of the card's height.
        self.stage.setMinimumHeight(430)
        cl.addWidget(self.stage, 1)
        self.overlay_msg = QLabel("")
        self.overlay_msg.setStyleSheet(f"color:{RED};border:none;")
        self.overlay_msg.setWordWrap(True)
        cl.addWidget(self.overlay_msg)

        # Clip Start / Clip End sit directly above the timeline, right over
        # the region they mark, instead of buried at the end of the Go-to
        # row -- previously called "Set In [" / "Set Out ]".
        clipbar = QHBoxLayout()
        clipbar.setSpacing(6)
        # Go-to lives on this same row now (no separate row below the
        # timeline for it, and no "CLIP" label -- what the two boxes and
        # Clip Start/Clip End buttons do is intuitive enough on its own).
        clipbar.addWidget(QLabel("Go to"))
        self.goto_tc_edit = QLineEdit()
        self.goto_tc_edit.setPlaceholderText("h:mm:ss")
        self.goto_tc_edit.setToolTip("Hours:minutes:seconds -- just type digits, the colons are added for you")
        self.goto_tc_edit.setStyleSheet(INPUT)
        self.goto_tc_edit.setFixedWidth(88)
        self.goto_tc_edit.returnPressed.connect(self.goto_timecode)
        self.goto_tc_edit.textEdited.connect(self._goto_tc_edited)
        self.goto_sec_edit = QLineEdit()
        self.goto_sec_edit.setPlaceholderText("seconds")
        self.goto_sec_edit.setToolTip("Plain seconds, e.g. 3723.5")
        self.goto_sec_edit.setStyleSheet(INPUT)
        self.goto_sec_edit.setFixedWidth(72)
        self.goto_sec_edit.returnPressed.connect(self.goto_timecode)
        self.goto_sec_edit.textEdited.connect(lambda _t: setattr(self, "_goto_active", "sec"))
        self._goto_active = "tc"  # which box Go/Enter reads from -- whichever was typed in last
        goto_spin = _spinner(lambda: self._nudge_goto(1), lambda: self._nudge_goto(-1),
                             "+1 second", "-1 second")
        go_btn = QPushButton("Go"); go_btn.setStyleSheet(BTN); go_btn.setCursor(Qt.PointingHandCursor)
        go_btn.clicked.connect(self.goto_timecode)
        clipbar.addWidget(self.goto_tc_edit); clipbar.addWidget(self.goto_sec_edit)
        clipbar.addWidget(goto_spin); clipbar.addWidget(go_btn)
        self.set_in_btn = QPushButton("Clip Start  [")
        self.set_out_btn = QPushButton("Clip End  ]")
        # Colored to match the green/red In-Out markers on the timeline
        # right below (and the mockup these were approved from) -- plain
        # navy BTN styling for both made them look identical and gave no
        # visual link to which marker each one sets.
        self.set_in_btn.setStyleSheet(
            f"QPushButton{{background:#fff;color:#06603f;border:1.5px solid {GREEN};"
            "border-radius:6px;padding:6px 12px;font-weight:600;}"
            "QPushButton:hover{background:#e9f9f1;}"
            "QPushButton:disabled{color:#98a2b3;border-color:#d0d5dd;}")
        self.set_out_btn.setStyleSheet(
            f"QPushButton{{background:#fff;color:{RED};border:1.5px solid {RED};"
            "border-radius:6px;padding:6px 12px;font-weight:600;}"
            "QPushButton:hover{background:#fdecea;}"
            "QPushButton:disabled{color:#98a2b3;border-color:#d0d5dd;}")
        for b in (self.set_in_btn, self.set_out_btn):
            b.setCursor(Qt.PointingHandCursor)
        self.set_in_btn.clicked.connect(lambda: self.set_in(self._pos))
        self.set_out_btn.clicked.connect(lambda: self.set_out(self._pos))
        clipbar.addStretch()
        clipbar.addWidget(self.set_in_btn); clipbar.addWidget(self.set_out_btn)
        cl.addLayout(clipbar)

        self.timeline = Timeline()
        self.timeline.seekRequested.connect(self.seek)
        self.timeline.inChanged.connect(self.set_in)
        self.timeline.outChanged.connect(self.set_out)
        cl.addWidget(self.timeline)
        cl.addSpacing(10)  # a bit of breathing room before the playback row, instead of it sitting flush under the timeline

        ctl = QHBoxLayout()
        ctl.setSpacing(6)
        # "(R)" / "(F)" spell out what the R/F step-unit selector's prefix
        # refers to -- Rewind and Forward -- right on the buttons it means.
        self.step_back = QPushButton("⏪ Rewind (R)")
        self.play_btn = QPushButton("▶ Play")
        self.step_fwd = QPushButton("Forward (F) ⏩")
        for b in (self.step_back, self.play_btn, self.step_fwd):
            b.setStyleSheet(BTN)
            b.setCursor(Qt.PointingHandCursor)
        # Noticeably bigger than its siblings -- it's the main playback
        # control, not just another button in the row. Green while showing
        # "Play", red while showing "Pause" -- same padding/font (and a
        # fixed width, set below) in both states so the button never
        # changes size as it toggles.
        self._play_style = (
            f"QPushButton{{background:{GREEN};color:#fff;border:1.5px solid {GREEN};"
            "border-radius:6px;padding:10px 22px;font-weight:700;font-size:14px;}"
            "QPushButton:hover{background:#0ea371;}"
            "QPushButton:disabled{background:#9fd9c3;border-color:#9fd9c3;color:#f0fbf7;}")
        self._pause_style = (
            f"QPushButton{{background:{RED};color:#fff;border:1.5px solid {RED};"
            "border-radius:6px;padding:10px 22px;font-weight:700;font-size:14px;}"
            "QPushButton:hover{background:#a52d23;}"
            "QPushButton:disabled{background:#e3a9a2;border-color:#e3a9a2;color:#fdf2f1;}")
        self._pause_icon, self._pause_icon_size = _pause_icon()
        self.play_btn.setStyleSheet(self._play_style)
        self.play_btn.setIconSize(self._pause_icon_size)
        # Fixed width sized to fit the larger of the two states, so Play
        # and Pause are exactly the same size rather than just similar.
        self.play_btn.setIcon(self._pause_icon)
        self.play_btn.setText(" Pause")
        pause_w = self.play_btn.sizeHint().width()
        self.play_btn.setIcon(QIcon())
        self.play_btn.setText("▶ Play")
        play_w = self.play_btn.sizeHint().width()
        self.play_btn.setFixedWidth(max(pause_w, play_w) + 4)
        # How far Rewind/Forward move -- one video frame, or a flat 1/10/30
        # seconds, same amount either direction for now (variable amounts
        # per direction is a possible future add, not needed yet). Labeled
        # "R/F: ..." -- on its own, "Frame"/"1s"/"10s" next to Rewind and
        # Forward didn't read as "how far Rewind/Forward move" to anyone
        # who hadn't been told.
        self.step_unit = QComboBox()
        for label, key in (("R/F: Frame", "Frame"), ("R/F: 1s", "1s"),
                           ("R/F: 10s", "10s"), ("R/F: 30s", "30s")):
            self.step_unit.addItem(label, key)
        self.step_unit.setToolTip("How far Rewind / Forward move each click")
        self.step_unit.currentIndexChanged.connect(lambda _i=None: self._update_step_labels())
        self.step_back.clicked.connect(lambda: self._step_by(-1))
        self.step_fwd.clicked.connect(lambda: self._step_by(1))
        self.play_btn.clicked.connect(self.toggle_play)
        self.preview_btn = QPushButton("▶ Preview clip")
        self.preview_btn.setToolTip("Plays Start → End at the export speed, then stops")
        self.preview_btn.setStyleSheet(BTN)
        self.preview_btn.setCursor(Qt.PointingHandCursor)
        self.preview_btn.clicked.connect(self.preview_clip)
        self._previewing = False
        self.zoom = QComboBox()
        self.zoom.addItems(["Timeline: Full", "Timeline: ±30 min", "Timeline: ±5 min",
                           "Timeline: ±1 min", "Timeline: Clip"])
        self.zoom.currentIndexChanged.connect(self._zoom_changed)
        self.zoom_clip_btn = QPushButton("🔍 Zoom to clip")
        self.zoom_clip_btn.setToolTip("Zoom the timeline in on the current Start → End span")
        self.zoom_clip_btn.setStyleSheet(BTN)
        self.zoom_clip_btn.setCursor(Qt.PointingHandCursor)
        self.zoom_clip_btn.clicked.connect(self.zoom_to_clip)
        self.pos_label = QLabel("00:00:00.0")
        self.pos_label.setStyleSheet("font-family:Consolas,monospace;font-weight:700;border:none;")
        ctl.addWidget(self.step_back); ctl.addWidget(self.play_btn); ctl.addWidget(self.step_fwd)
        ctl.addWidget(self.step_unit)
        ctl.addWidget(self.preview_btn); ctl.addWidget(self.zoom); ctl.addWidget(self.zoom_clip_btn)
        ctl.addStretch()
        ctl.addWidget(self.pos_label)
        cl.addLayout(ctl)

        # The clip's length (and what it becomes at the chosen export
        # speed) shown right under the timeline, always on screen -- the
        # same info used to live only in the Clip section's peek/length
        # line on the right, which scrolls out of view once Export
        # Settings is expanded.
        clip_len_row = QHBoxLayout()
        clip_len_row.addStretch()
        self.clip_len_readout = QLabel("")
        self.clip_len_readout.setStyleSheet(
            "font-family:Consolas,monospace;font-size:11.5px;color:#000000;font-weight:600;"
            "background:#f3f5f9;border-radius:4px;padding:2px 8px;border:none;")
        clip_len_row.addWidget(self.clip_len_readout)
        cl.addLayout(clip_len_row)

        left.addWidget(card, 1)
        root.addLayout(left, 1)

        # ----- right: clip + export
        wrap = QWidget(); wrap.setFixedWidth(350)
        self.right_wrap = wrap  # the About/Help overlay below covers exactly this column
        wrap_lay = QVBoxLayout(wrap)
        wrap_lay.setContentsMargins(0, 0, 0, 0)
        wrap_lay.setSpacing(12)
        root.addWidget(wrap)

        # Clip / Text overlay / Snapshot / Export settings all scroll
        # together in their own area, so opening several collapsible
        # sections at once doesn't squeeze -- or at narrow/short window
        # sizes, visually overlap -- the fixed Export box below. That
        # Export box (destination + Export clip + progress + Open Folder)
        # stays outside the scroll area so it's always visible without
        # scrolling down to find it.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # Explicit scrollbar styling -- left unstyled, the native/Fusion
        # scrollbar rendered a dark thumb on a dark track on the user's
        # machine (likely picking up a dark Windows theme), making it
        # effectively invisible against the white panel. This forces a
        # plain white track with a clearly visible light-grey thumb
        # regardless of OS theme.
        scroll.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}"
            "QScrollArea > QWidget > QWidget{background:transparent;}"
            "QScrollBar:vertical{background:#ffffff;width:12px;margin:0px;border-radius:6px;}"
            "QScrollBar::handle:vertical{background:#c3c9d6;min-height:30px;border-radius:6px;}"
            "QScrollBar::handle:vertical:hover{background:#9aa3b5;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0px;}"
            "QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:#ffffff;}")
        scroll_body = QWidget()
        right = QVBoxLayout(scroll_body)
        right.setSpacing(12)
        right.setContentsMargins(0, 0, 4, 4)
        scroll.setWidget(scroll_body)
        wrap_lay.addWidget(scroll, 1)

        self.sec_clip = CollapsibleSection("Clip", start_open=True)
        l1 = self.sec_clip.body_lay
        self.in_edit = QLineEdit(); self.out_edit = QLineEdit()
        for e in (self.in_edit, self.out_edit):
            e.setStyleSheet(INPUT)
            e.setPlaceholderText("not set")
        self.in_edit.editingFinished.connect(lambda: self._edit_marker("in"))
        self.out_edit.editingFinished.connect(lambda: self._edit_marker("out"))
        # Labeled "Start"/"End" here to match "Clip Start [" / "Clip End ]"
        # above the timeline -- "In"/"Out" (the internal names, still used
        # for variables/signals below) read as jargon to anyone who hasn't
        # edited video before.
        for lab, e, which in (("Start", self.in_edit, "in"), ("End", self.out_edit, "out")):
            r = QHBoxLayout(); r.setSpacing(4)
            lb = QLabel(lab); lb.setFixedWidth(34); lb.setStyleSheet("border:none;")
            spin = _spinner(lambda w=which: self._nudge_marker(w, 1.0, seconds=True),
                            lambda w=which: self._nudge_marker(w, -1.0, seconds=True),
                            "Nudge forward 1 second", "Nudge back 1 second")
            r.addWidget(lb); r.addWidget(e); r.addWidget(spin)
            l1.addLayout(r)
        self.len_label = QLabel("Set Start and End to define the clip.")
        self.len_label.setWordWrap(True)
        self.len_label.setStyleSheet("border:none;")
        l1.addWidget(self.len_label)
        right.addWidget(self.sec_clip)

        # ----- text overlay (optional, burned in at export + captures)
        self.sec_overlay = CollapsibleSection("Text overlay", start_open=False)
        lov = self.sec_overlay.body_lay
        self.ov_text_edit = QPlainTextEdit()
        self.ov_text_edit.setFixedHeight(56)
        self.ov_text_edit.setStyleSheet(INPUT_MULTI)
        self.ov_text_edit.setPlaceholderText("Type the caption to burn into the exported video…")
        self.ov_text_edit.textChanged.connect(self._overlay_text_changed)
        lov.addWidget(self.ov_text_edit)
        ov_row = QHBoxLayout(); ov_row.setSpacing(6)
        self.ov_font_combo = QComboBox()
        self.ov_font_combo.addItems(OVERLAY_FONTS)
        self.ov_font_combo.currentTextChanged.connect(self._overlay_style_changed)
        self.ov_size_combo = QComboBox()
        self.ov_size_combo.setEditable(True)
        self.ov_size_combo.addItems([str(s) for s in OVERLAY_SIZE_PRESETS])
        self.ov_size_combo.setCurrentText(str(OVERLAY_SIZE_DEFAULT))
        self.ov_size_combo.setFixedWidth(64)
        self.ov_size_combo.currentTextChanged.connect(self._overlay_style_changed)
        self.ov_size_combo.activated.connect(lambda _=None: self.ov_size_combo.lineEdit().clearFocus())
        ov_row.addWidget(self.ov_font_combo, 1)
        ov_row.addWidget(self.ov_size_combo)
        lov.addLayout(ov_row)
        ov_color_row = QHBoxLayout(); ov_color_row.setSpacing(6)
        color_lab = QLabel("Color"); color_lab.setStyleSheet("border:none;")
        self.ov_color_combo = QComboBox()
        for name, hexval in OVERLAY_COLORS:
            self.ov_color_combo.addItem(name, hexval)
        self.ov_color_combo.currentIndexChanged.connect(self._overlay_style_changed)
        ov_color_row.addWidget(color_lab)
        ov_color_row.addWidget(self.ov_color_combo, 1)
        lov.addLayout(ov_color_row)
        # A plain checkbox here was easy to miss, so a button is clearer
        # about there being a deliberate action -- but it used to also
        # disable the text box/font/color until clicked, which was
        # backwards: you had to click Insert before you could even type.
        # Now you can type and style the caption freely at any time; this
        # button only controls whether it shows on the video/export.
        self.ov_enable = QPushButton("➕  Insert in video")
        self.ov_enable.setCheckable(True)
        self.ov_enable.setStyleSheet(BTN)
        self.ov_enable.setCursor(Qt.PointingHandCursor)
        self.ov_enable.toggled.connect(self._overlay_toggled)
        lov.addWidget(self.ov_enable)
        ov_hint = QLabel("Type your caption, pick a font/size/color, then click Insert in video "
                         "to show it. Wraps automatically inside the box; drag the box on the "
                         "video to move it, drag an edge or corner to resize.")
        ov_hint.setWordWrap(True)
        ov_hint.setStyleSheet(f"color:{SUB};font-size:11px;border:none;")
        lov.addWidget(ov_hint)
        right.addWidget(self.sec_overlay)

        # ----- snapshot (frame capture)
        self.sec_snap = CollapsibleSection("Snapshot", start_open=False)
        lsn = self.sec_snap.body_lay
        sn_row = QHBoxLayout(); sn_row.setSpacing(8)
        sn_lab = QLabel("Save frame captures as"); sn_lab.setStyleSheet("border:none;")
        self.capture_fmt = QComboBox()
        self.capture_fmt.addItems(CAPTURE_FORMATS)
        self.capture_fmt.currentTextChanged.connect(lambda _t=None: self._update_section_peeks())
        sn_row.addWidget(sn_lab); sn_row.addWidget(self.capture_fmt); sn_row.addStretch()
        lsn.addLayout(sn_row)
        sn_hint = QLabel('Saved next to the source video as Capture1, Capture2, ... Click '
                         '"Capture frame" on the preview any time, playing or paused. Includes '
                         "the text overlay if it's turned on.")
        sn_hint.setWordWrap(True)
        sn_hint.setStyleSheet(f"color:{SUB};font-size:11px;border:none;")
        lsn.addWidget(sn_hint)
        right.addWidget(self.sec_snap)

        self.sec_exp = CollapsibleSection("Export settings", start_open=True)
        l2 = self.sec_exp.body_lay
        def sub(txt):
            lb = QLabel(txt.upper())
            lb.setStyleSheet(f"color:{SUB};font-size:10.5px;font-weight:600;border:none;margin-top:6px;")
            l2.addWidget(lb)
        sub("Speed")
        spd = QHBoxLayout(); spd.setSpacing(6)
        minus = QPushButton("−"); plus = QPushButton("+")
        for b in (minus, plus):
            b.setStyleSheet(BTN); b.setFixedWidth(34); b.setCursor(Qt.PointingHandCursor)
        self.speed_combo = QComboBox()
        self.speed_combo.setEditable(True)
        self.speed_combo.addItems([f"{p:g}" for p in SPEED_PRESETS])
        self.speed_combo.setCurrentText("1")
        self.speed_combo.setFixedWidth(64)
        self.speed_combo.setToolTip("Pick a preset or type any speed (0.25 - 16). "
                                    "Preview plays at this speed too.")
        self.speed_combo.currentTextChanged.connect(self._speed_changed)
        self.speed_combo.lineEdit().editingFinished.connect(self._speed_commit)
        # Hide the caret once a value is actually chosen/confirmed (dropdown
        # pick, or Enter after typing) -- it should only show while the user
        # is mid-edit, not just sitting there blinking.
        self.speed_combo.activated.connect(lambda _=None: self.speed_combo.lineEdit().deselect()
                                            or self.speed_combo.lineEdit().clearFocus())
        x_label = QLabel("x")
        x_label.setStyleSheet(f"color:{TEXT};font-weight:700;border:none;")
        minus.clicked.connect(lambda: self._nudge_speed(-SPEED_STEP))
        plus.clicked.connect(lambda: self._nudge_speed(SPEED_STEP))
        spd.addWidget(minus); spd.addWidget(self.speed_combo); spd.addWidget(x_label)
        spd.addWidget(plus); spd.addStretch()
        l2.addLayout(spd)
        hint = QLabel("Below 1x = slow motion. Preview plays at this speed.")
        hint.setStyleSheet(f"color:{SUB};font-size:11px;border:none;")
        l2.addWidget(hint)
        sub("Resolution")
        # Resolution also needs to refresh the default File name (its "_720p"
        # etc suffix), the same way Speed already does -- not just the Clip
        # length readout.
        self.res_grp, self.res_btns = _seg(list(RESOLUTIONS), "720p", l2, self._res_changed)
        sub("Quality / size")
        self.q_grp, self.q_btns = _seg(list(QUALITY_CRF), "Balanced", l2, self._update_section_peeks)
        sub("Format")
        # Changing Format only updates the File name box's extension chip
        # below (_fmt_changed), not the name itself -- unlike the clip
        # trim/speed changes that regenerate the whole default name, this
        # shouldn't stomp over anything you've already typed there.
        self.fmt_grp, self.fmt_btns = _seg(["MP4", "MKV"], "MP4", l2, self._fmt_changed)
        note = QLabel("Audio is always removed.")
        note.setStyleSheet(f"color:{SUB};border:none;margin-top:6px;")
        l2.addWidget(note)
        right.addWidget(self.sec_exp)
        right.addStretch()

        c3, l3 = _card("Export")

        def exp_sub(txt):
            lb = QLabel(txt)
            lb.setStyleSheet(f"color:{SUB};font-size:10.5px;font-weight:700;border:none;"
                             "letter-spacing:0.03em;margin-top:2px;")
            l3.addWidget(lb)

        exp_sub("DESTINATION FOLDER")
        self.out_folder_edit = QLineEdit()
        self.out_folder_edit.setReadOnly(True)
        self.out_folder_edit.setStyleSheet(INPUT_READONLY)
        self.out_folder_edit.setToolTip("Where the exported clip is saved -- use Browse to change it")
        browse = QPushButton("Browse…"); browse.setStyleSheet(BTN); browse.setCursor(Qt.PointingHandCursor)
        browse.clicked.connect(self.browse_out_folder)
        r = QHBoxLayout(); r.setSpacing(6); r.addWidget(self.out_folder_edit, 1); r.addWidget(browse)
        l3.addLayout(r)

        exp_sub("FILE NAME")
        self.out_name_edit = QLineEdit()
        self.out_name_edit.setStyleSheet(INPUT)
        self.out_name_edit.setPlaceholderText("file name")
        self.out_name_edit.textEdited.connect(self._out_name_edited)
        self.out_name_edit.editingFinished.connect(self._update_export_enabled)
        # The extension always matches whichever Format button (MP4/MKV)
        # is selected above -- a locked, read-only chip rather than
        # something typed, so a missing or unsupported extension simply
        # can't happen in what actually gets exported.
        self.out_ext_chip = QLabel(".mp4")
        self.out_ext_chip.setStyleSheet(
            f"color:{NAVY};background:#eef3ff;border:1.5px solid #c7d7f5;border-radius:6px;"
            "padding:6px 9px;font-family:Consolas,monospace;font-weight:700;font-size:12px;")
        self.out_ext_chip.setToolTip("Matches the Format choice above (MP4/MKV)")
        nr = QHBoxLayout(); nr.setSpacing(6)
        nr.addWidget(self.out_name_edit, 1); nr.addWidget(self.out_ext_chip)
        l3.addLayout(nr)
        self.out_name_error = QLabel("")
        self.out_name_error.setWordWrap(True)
        self.out_name_error.setStyleSheet(f"color:{RED};font-size:11px;border:none;margin-top:2px;")
        l3.addWidget(self.out_name_error)

        self.export_btn = QPushButton("Export Clip")
        self.export_btn.setStyleSheet(PRIMARY)
        self.export_btn.setMinimumHeight(36)
        self.export_btn.setCursor(Qt.PointingHandCursor)
        self.export_btn.clicked.connect(self.export_or_cancel)
        l3.addWidget(self.export_btn)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        self.progress.setStyleSheet(
            f"QProgressBar{{background:{BORDER};border:none;border-radius:4px;}}"
            f"QProgressBar::chunk{{background:{GREEN};border-radius:4px;}}")
        l3.addWidget(self.progress)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color:{SUB};border:none;")
        l3.addWidget(self.status)
        self.open_folder_btn = QPushButton("📁 Open Folder")
        self.open_folder_btn.setStyleSheet(BTN)
        self.open_folder_btn.setCursor(Qt.PointingHandCursor)
        self.open_folder_btn.clicked.connect(self.open_export_folder)
        l3.addWidget(self.open_folder_btn)
        wrap_lay.addWidget(c3)

        self._build_info_overlay()

        # ----- player (video only: no audio output is ever created)
        self.player = QMediaPlayer(self)
        self.player.setVideoOutput(self.video.item)
        self.player.positionChanged.connect(self._player_pos)
        self.player.playbackStateChanged.connect(self._state_changed)
        self.player.errorOccurred.connect(self._player_error)

        # shortcuts
        for key, fn in (("Space", self.toggle_play), ("I", lambda: self.set_in(self._pos)),
                        ("O", lambda: self.set_out(self._pos)),
                        ("Left", lambda: self.step(-1)), ("Right", lambda: self.step(1)),
                        ("Shift+Left", lambda: self.seek(self._pos - 5)),
                        ("Shift+Right", lambda: self.seek(self._pos + 5))):
            QShortcut(QKeySequence(key), self, activated=fn,
                      context=Qt.WidgetWithChildrenShortcut)

        self._set_enabled(False)
        self._update_section_peeks()
        self._update_step_labels()
        if not FFMPEG or not FFPROBE:
            self.status.setText("FFmpeg/ffprobe not found - install FFmpeg or place it "
                                "next to this app. Needed for thumbnails and export.")

    # ------------------------------------------------------ about / help
    def _build_info_overlay(self):
        """The About/Help panel: a plain child of this page, positioned to
        exactly cover self.right_wrap (the Clip/Export column) so it slides
        in ON TOP of those controls rather than replacing them in the
        layout -- they're untouched underneath, just visually covered
        while the panel is open."""
        self.info_overlay = QFrame(self)
        self.info_overlay.setStyleSheet(f"QFrame{{background:#fff;border-left:1px solid {BORDER};}}")
        shadow = QGraphicsDropShadowEffect(self.info_overlay)
        shadow.setBlurRadius(24)
        shadow.setXOffset(-4)
        shadow.setYOffset(0)
        shadow.setColor(QColor(0, 0, 0, 70))
        self.info_overlay.setGraphicsEffect(shadow)

        lay = QVBoxLayout(self.info_overlay)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        head_row = QHBoxLayout()
        self.info_title = QLabel("About Runaway Studio")
        self.info_title.setStyleSheet(f"color:{TEXT};font-size:13px;font-weight:700;border:none;")
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(26, 26)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setToolTip("Close")
        close_btn.setStyleSheet(
            f"QPushButton{{background:transparent;color:{SUB};border:none;font-size:13px;}}"
            f"QPushButton:hover{{color:{TEXT};}}")
        close_btn.clicked.connect(self._close_info)
        head_row.addWidget(self.info_title)
        head_row.addStretch()
        head_row.addWidget(close_btn)
        lay.addLayout(head_row)

        tabs_row = QHBoxLayout(); tabs_row.setSpacing(16)
        self.info_about_tab = QLabel("ABOUT")
        self.info_help_tab = QLabel("HELP")
        for lb, which in ((self.info_about_tab, "about"), (self.info_help_tab, "help")):
            lb.setCursor(Qt.PointingHandCursor)
            lb.mousePressEvent = lambda _e, w=which: self._show_info(w)
        tabs_row.addWidget(self.info_about_tab)
        tabs_row.addWidget(self.info_help_tab)
        tabs_row.addStretch()
        lay.addLayout(tabs_row)

        div = QFrame(); div.setFrameShape(QFrame.HLine); div.setStyleSheet(f"color:{BORDER};")
        lay.addWidget(div)

        self.about_card = _build_about_card()
        self.help_card = _build_help_card()
        self.help_card.hide()
        lay.addWidget(self.about_card, 1)
        lay.addWidget(self.help_card, 1)

        self.info_overlay.hide()
        self._info_anim = QPropertyAnimation(self.info_overlay, b"pos", self)
        self._info_anim.setDuration(220)
        self._info_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._position_info_overlay(visible=False)

    def _style_info_tab(self, label, active):
        if active:
            label.setStyleSheet(
                f"color:{NAVY};font-size:11px;font-weight:700;border:none;"
                f"border-bottom:2px solid {NAVY};padding-bottom:5px;")
        else:
            label.setStyleSheet(
                f"color:{SUB};font-size:11px;font-weight:700;border:none;padding-bottom:5px;")

    def _position_info_overlay(self, visible):
        """Keeps the overlay exactly over self.right_wrap -- called on
        open and on resize, since its geometry is absolute, not managed
        by a layout."""
        top_left = self.right_wrap.mapTo(self, QPoint(0, 0))
        w, h = self.right_wrap.width(), self.right_wrap.height()
        shown_x = top_left.x()
        hidden_x = top_left.x() + w  # just off the right edge
        self.info_overlay.setGeometry(shown_x if visible else hidden_x, top_left.y(), w, h)

    def _show_info(self, tab):
        """Open the panel (sliding in from the right edge of the Clip/
        Export column) to either the About or Help tab."""
        first_open = not self.info_overlay.isVisible()
        is_about = tab == "about"
        self.about_card.setVisible(is_about)
        self.help_card.setVisible(not is_about)
        self.info_title.setText("About Runaway Studio" if is_about else "Help")
        self._style_info_tab(self.info_about_tab, is_about)
        self._style_info_tab(self.info_help_tab, not is_about)

        top_left = self.right_wrap.mapTo(self, QPoint(0, 0))
        w, h = self.right_wrap.width(), self.right_wrap.height()
        shown_pos = QPoint(top_left.x(), top_left.y())
        self.info_overlay.setGeometry(top_left.x() + (w if first_open else 0), top_left.y(), w, h)
        self.info_overlay.show()
        self.info_overlay.raise_()
        self._info_anim.stop()
        self._info_anim.setStartValue(self.info_overlay.pos())
        self._info_anim.setEndValue(shown_pos)
        self._info_anim.start()

    def _close_info(self):
        if not self.info_overlay.isVisible():
            return
        top_left = self.right_wrap.mapTo(self, QPoint(0, 0))
        hidden_pos = QPoint(top_left.x() + self.right_wrap.width(), top_left.y())
        self._info_anim.stop()
        self._info_anim.setStartValue(self.info_overlay.pos())
        self._info_anim.setEndValue(hidden_pos)
        try:
            self._info_anim.finished.disconnect()
        except (RuntimeError, TypeError):
            pass
        self._info_anim.finished.connect(self.info_overlay.hide)
        self._info_anim.start()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        # The overlay's geometry is absolute (it isn't managed by a
        # layout), so it has to be re-synced to self.right_wrap's screen
        # position by hand whenever this page is resized.
        if hasattr(self, "info_overlay"):
            self._position_info_overlay(visible=self.info_overlay.isVisible())

    # ---------------------------------------------------------- state
    def _set_enabled(self, on):
        for w in (self.step_back, self.play_btn, self.step_fwd, self.step_unit, self.set_in_btn,
                  self.preview_btn, self.set_out_btn, self.goto_tc_edit,
                  self.goto_sec_edit, self.in_edit, self.out_edit, self.capture_btn, self.zoom_clip_btn,
                  self.out_name_edit):
            w.setEnabled(on)
        # export_btn's own enabled state additionally depends on whether
        # the File name box currently holds something exportable -- see
        # _update_export_enabled.
        if on:
            self._update_export_enabled()
        else:
            self.export_btn.setEnabled(False)

    def _update_section_peeks(self):
        self.sec_clip.set_peek(
            f"{fmt_tc(self.t_in, False)} → {fmt_tc(self.t_out, False)}"
            if self.t_in is not None and self.t_out is not None else "not set")
        self.sec_overlay.set_peek("On" if self.ov_enable.isChecked() else "Off")
        self.sec_snap.set_peek(self.capture_fmt.currentText())
        res_btn = self.res_grp.checkedButton()
        q_btn = self.q_grp.checkedButton()
        fmt_btn = self.fmt_grp.checkedButton()
        self.sec_exp.set_peek(
            f"{self._speed():g}x · {res_btn.text() if res_btn else '720p'} · "
            f"{q_btn.text() if q_btn else 'Balanced'} · {fmt_btn.text() if fmt_btn else 'MP4'}")

    # ------------------------------------------------------- text overlay
    def _overlay_toggled(self, on):
        self.overlay_box.setVisible(on and self.info is not None)
        self.ov_enable.setText("✕  Remove text" if on else "➕  Insert in video")
        self._update_section_peeks()

    def _overlay_text_changed(self):
        self.overlay_box.set_text(self.ov_text_edit.toPlainText())

    def _overlay_style_changed(self, _txt=None):
        try:
            sz = max(4, min(400, int(float(self.ov_size_combo.currentText()))))
        except ValueError:
            return
        self.overlay_box.set_font(self.ov_font_combo.currentText() or "Arial", sz)
        self.overlay_box.set_color(self.ov_color_combo.currentData() or "#ffffff")

    # ---------------------------------------------------------- snapshot
    def capture_frame(self):
        if not self.info:
            self.status.setText("Open a video first.")
            return
        if not FFMPEG:
            self.status.setText("FFmpeg not found - can't capture frames.")
            return
        try:
            r = subprocess.run(
                [FFMPEG, "-v", "error", "-ss", f"{self._pos:.3f}", "-i", self.video_path,
                 "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                capture_output=True, timeout=30,
                creationflags=_SUBPROCESS_FLAGS)
            img = QImage.fromData(r.stdout, "PNG") if r.stdout else QImage()
        except Exception as ex:
            self.status.setText(f"Capture failed: {ex}")
            return
        if img.isNull():
            self.status.setText("Capture failed - couldn't read a frame at this position.")
            return
        if self.ov_enable.isChecked() and self.ov_text_edit.toPlainText().strip():
            img = self._composite_overlay(img)
        ext = "png" if self.capture_fmt.currentText() == "PNG" else "jpg"
        folder = os.path.dirname(os.path.abspath(self.video_path))
        n = 1
        while any(os.path.exists(os.path.join(folder, f"Capture{n}.{e}")) for e in ("png", "jpg", "jpeg")):
            n += 1
        dst = os.path.join(folder, f"Capture{n}.{ext}")
        ok = img.save(dst, "PNG" if ext == "png" else "JPG", -1 if ext == "png" else 92)
        self.status.setText(f"Saved {os.path.basename(dst)}" if ok else "Couldn't save the capture.")

    def _composite_overlay(self, img):
        """Burn the current overlay text onto a captured frame, matching
        (not pixel-identical to, since Qt and ffmpeg render text
        differently, but visually equivalent) the export's drawtext box:
        same relative position/size, same semi-opaque backdrop."""
        img = img.convertToFormat(QImage.Format_ARGB32)
        w, h = img.width(), img.height()
        x, y, bw, bh = self.overlay_box.rel
        box = QRect(int(x * w), int(y * h), max(10, int(bw * w)), max(10, int(bh * h)))
        size_px = max(6, int(self.overlay_box.size_px))
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        f = QFont(self.overlay_box.font_family)
        f.setPixelSize(size_px)
        f.setBold(True)
        p.setFont(f)
        p.fillRect(box, QColor(0, 0, 0, 115))
        p.setPen(QColor("#ffffff"))
        p.drawText(box.adjusted(6, 4, -6, -4), Qt.AlignCenter | Qt.TextWordWrap,
                   self.ov_text_edit.toPlainText())
        p.end()
        return img

    def position_seconds(self):
        return self._pos

    def open_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open video", "", "Video (*.mp4 *.mkv *.mov *.avi *.m4v *.webm);;All files (*)")
        if path:
            self.load(path)

    def load(self, path):
        try:
            info = probe(path)
        except Exception as ex:
            QMessageBox.warning(self, "Open video", str(ex))
            return
        if self._thumbs:
            self._thumbs.stop(); self._thumbs.wait(2000)
        self.player.stop()
        self.video_path, self.info = path, info
        self.t_in = self.t_out = None
        self._pos = 0.0
        self._preview_ok = True
        self.overlay_msg.setText("")
        self.open_btn.setStyleSheet(self._open_btn_style_loaded)
        self.file_label.setText(
            f"{os.path.basename(path)}   ·   {info['width']}×{info['height']}   ·   "
            f"{info['fps']:.2f} fps   ·   {fmt_len(info['duration'])}   ·   "
            f"{info['size'] / 1e9:.2f} GB   ·   {info['codec']}")
        self.folder_label.setText(os.path.dirname(os.path.abspath(path)))
        self.stage.set_source_size(info["width"], info["height"])
        self.overlay_box.set_source_height(info["height"])
        self.overlay_box.setVisible(self.ov_enable.isChecked())
        self.timeline.set_duration(info["duration"])
        self._update_step_labels()
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.pause()
        self._set_enabled(True)
        # Reset the export destination to THIS video's own folder. Without
        # this, _refresh_out_name() (below) keeps whatever folder the
        # output path was last pointed at -- so opening a new video from a
        # different folder kept exporting into the previous video's
        # folder instead of following the newly opened one.
        self.out_folder_edit.setText("")
        self.out_name_error.setText("")
        self.out_name_edit.setStyleSheet(INPUT)
        # Default the export Format to match the loaded file's own
        # extension (when it's one we can export to) rather than always
        # defaulting to MP4, regardless of what was actually opened.
        loaded_ext = os.path.splitext(path)[1].lstrip(".").lower()
        if loaded_ext in SUPPORTED_EXPORT_EXTS:
            btn = self.fmt_btns.get(loaded_ext.upper())
            if btn:
                btn.setChecked(True)
        self._marker_text(); self._update_clip_info(); self._refresh_out_name()
        self.progress.setValue(0)
        self.status.setText("")
        if FFMPEG:
            self._thumbs = ThumbWorker(path, info["duration"], self.timeline.thumb_count, self)
            self._thumbs.thumb.connect(self.timeline.set_thumb)
            self._thumbs.start()
        self.seek(0)

    # ------------------------------------------------------- playback
    def seek(self, sec):
        if not self.info:
            return
        sec = min(max(float(sec), 0.0), self.info["duration"])
        self._pos = sec
        self.player.setPosition(int(sec * 1000))
        self._pos_changed()

    def _pos_changed(self):
        self.timeline.set_pos(self._pos)
        rate = self.player.playbackRate()
        suffix = f"  <span style='color:#e5342a;font-weight:700;'>({rate:g}x)</span>" if abs(rate - 1.0) > 1e-6 else ""
        self.pos_label.setText(fmt_tc(self._pos) + suffix)
        self.positionChanged.emit(self._pos)

    def _player_pos(self, ms):
        # ignore the player's own echo while the user is scrubbing
        if self.timeline._drag == "seek":
            return
        self._pos = ms / 1000.0
        self._pos_changed()
        if self._previewing and self.t_out is not None and self._pos >= self.t_out:
            self._previewing = False
            self.player.pause()
            self.seek(self.t_out)

    def step(self, n):
        if not self.info:
            return
        self.player.pause()
        self.seek(self._pos + n / max(self.info["fps"], 1.0))

    def _step_by(self, direction):
        """Rewind/Forward buttons: move by whatever unit is selected in
        step_unit (a video frame, or a flat 1/10/30 seconds)."""
        if not self.info:
            return
        unit = self.step_unit.currentData()
        if unit == "Frame":
            self.step(direction)
        else:
            self.player.pause()
            secs = {"1s": 1, "10s": 10, "30s": 30}.get(unit, 1)
            self.seek(self._pos + direction * secs)

    def _update_step_labels(self):
        """Shows what Rewind/Forward will actually move by, e.g. the
        real per-frame duration at this file's frame rate, right in the
        tooltip -- so "Frame" doesn't leave you guessing how far that is
        in seconds."""
        unit = self.step_unit.currentData()
        if unit == "Frame" and self.info:
            amt = f"1 frame ({1 / max(self.info['fps'], 1e-6):.3f}s)"
        elif unit == "Frame":
            amt = "1 frame"
        else:
            amt = unit
        self.step_back.setToolTip(f"Rewind {amt}")
        self.step_fwd.setToolTip(f"Forward {amt}")

    def _goto_box(self):
        """Which Go-to box Go/Enter/the spinner should read from -- whichever
        one was typed in most recently, falling back to the other if that
        one's empty."""
        active = self.goto_tc_edit if self._goto_active == "tc" else self.goto_sec_edit
        other = self.goto_sec_edit if self._goto_active == "tc" else self.goto_tc_edit
        return active if active.text().strip() else other

    def _goto_tc_edited(self, text):
        """Auto-formats the h:mm:ss box as you type -- just type digits and
        the colons are inserted for you (e.g. "13045" becomes "1:30:45")."""
        self._goto_active = "tc"
        digits = re.sub(r"\D", "", text)[:6]
        if len(digits) <= 2:
            out = digits
        elif len(digits) <= 4:
            out = f"{digits[:-2]}:{digits[-2:]}"
        else:
            out = f"{digits[:-4]}:{digits[-4:-2]}:{digits[-2:]}"
        if out != text:
            self.goto_tc_edit.setText(out)

    def _nudge_goto(self, delta):
        """+1s/-1s spinner next to the Go to boxes -- adjusts whatever
        timecode is already typed in the active box (or the current
        playback position if both are empty) without needing to press Go."""
        box = self._goto_box()
        cur = parse_timecode(box.text())
        if cur is None:
            cur = self._pos
        new_val = max(0.0, cur + delta)
        if box is self.goto_sec_edit:
            box.setText(f"{new_val:g}")
        else:
            box.setText(fmt_tc(new_val, frac=False))

    def toggle_play(self):
        if not self.info:
            return
        self._previewing = False
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _state_changed(self, st):
        playing = st == QMediaPlayer.PlayingState
        self.play_btn.setIcon(self._pause_icon if playing else QIcon())
        self.play_btn.setText(" Pause" if playing else "▶ Play")
        self.play_btn.setStyleSheet(self._pause_style if playing else self._play_style)

    def _speed_changed(self, _txt=None):
        """Preview speed always follows the export speed, so what you see
        is what gets exported."""
        sp = self._speed()
        self.player.setPlaybackRate(sp)
        self._update_clip_info()
        self._refresh_out_name()
        self._pos_changed()  # refresh the "(2x)" marker next to the time right away, not just on the next tick
        # Changing speed mid-preview left the status line's "at 1x..." text
        # stale (it was only set once, when Preview clip was first
        # clicked) even though the timeline's red (2x) marker updated
        # immediately -- keep this in sync too.
        if self._previewing and self.t_in is not None and self.t_out is not None:
            self.status.setText(f"Previewing {fmt_tc(self.t_in, False)} → "
                                f"{fmt_tc(self.t_out, False)} at {sp:g}x…")

    def _speed_commit(self):
        # tidy whatever was typed ("3x" or "3" -> "3", out-of-range -> clamped)
        self.speed_combo.setCurrentText(f"{self._speed():g}")
        self.speed_combo.lineEdit().deselect()
        self.speed_combo.lineEdit().clearFocus()

    def _nudge_speed(self, d):
        sp = min(max(round((self._speed() + d) / SPEED_STEP) * SPEED_STEP, SPEED_MIN), SPEED_MAX)
        self.speed_combo.setCurrentText(f"{sp:g}")

    def preview_clip(self):
        """Play Start -> End at the export speed and stop at End."""
        if not self.info:
            return
        if self.t_in is None or self.t_out is None or self.t_out <= self.t_in:
            self._flash("Set Start and End first, then Preview clip.")
            return
        self._previewing = True
        self.seek(self.t_in)
        self.player.setPlaybackRate(self._speed())
        self.player.play()
        self.status.setText(f"Previewing {fmt_tc(self.t_in, False)} → "
                            f"{fmt_tc(self.t_out, False)} at {self._speed():g}x…")

    def _flash(self, msg, ms=3000):
        """Show a message both in the status line and, briefly, right on
        top of the video -- the status line alone is easy to miss since
        it's at the bottom of the right-hand panel."""
        self.status.setText(msg)
        self.overlay_msg.setText(msg)

        def clear():
            if self.overlay_msg.text() == msg:
                self.overlay_msg.setText("")
        QTimer.singleShot(ms, clear)

    def _zoom_changed(self, idx):
        if idx == 4:   # "Timeline: clip"
            self.zoom_to_clip()
            return
        self.timeline.set_zoom([None, 1800, 300, 60][idx])

    def zoom_to_clip(self):
        """Zoom the timeline in to exactly the current Start -> End span (no
        padding) -- the view starts and stops right at Start / End.
        Keeps the "Timeline:" dropdown in sync so it doesn't sit there
        saying "full" while the view is actually zoomed in."""
        if self.t_in is None or self.t_out is None or self.t_out <= self.t_in or not self.info:
            self._flash("Set Start and End first, then Zoom to clip.")
            return
        self.timeline.set_view_range(self.t_in, self.t_out)
        self.zoom.blockSignals(True)
        self.zoom.setCurrentIndex(4)
        self.zoom.blockSignals(False)

    def open_export_folder(self):
        """Opens the folder the export will land in (or the source
        video's folder if no destination is set yet) in the OS file
        explorer. Always available, even before anything's been exported."""
        folder = self.out_folder_edit.text().strip()
        if not folder and self.video_path:
            folder = os.path.dirname(os.path.abspath(self.video_path))
        if not folder or not os.path.isdir(folder):
            self.status.setText("No export folder to open yet - open a video or choose an output path first.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def _player_error(self, _err, msg):
        self._preview_ok = False
        self.overlay_msg.setText(
            f"Preview can't play this file here ({msg}). Thumbnails, timecode entry and "
            "export still work - the preview just won't show frames.")

    def goto_timecode(self):
        s = parse_timecode(self._goto_box().text())
        if s is None:
            self.status.setText("Couldn't read that timecode - try 1:30:45 in the h:mm:ss box or 3723.5 in the seconds box")
            return
        self.seek(s)

    # --------------------------------------------------------- markers
    def set_in(self, s):
        s = min(max(s, 0.0), self.info["duration"])
        if self.t_out is not None and s >= self.t_out:
            self.t_out = None
        self.t_in = s
        self._markers_changed()

    def set_out(self, s):
        s = min(max(s, 0.0), self.info["duration"])
        if self.t_in is not None and s <= self.t_in:
            self.t_in = None
        self.t_out = s
        self._markers_changed()

    def _nudge_marker(self, which, frames, seconds=False):
        """Fine In/Out adjustment. By default `frames` is a frame count
        (used for the old precise per-frame nudging); pass seconds=True to
        treat `frames` as whole seconds instead -- used by the Clip
        section's up/down spinner, which steps by 1 second by default."""
        if not self.info:
            return
        step = frames if seconds else frames / max(self.info["fps"], 1e-6)
        cur = (self.t_in if which == "in" else self.t_out)
        if cur is None:
            cur = self._pos
        (self.set_in if which == "in" else self.set_out)(cur + step)

    def _edit_marker(self, which):
        edit = self.in_edit if which == "in" else self.out_edit
        txt = edit.text().strip()
        if not txt:
            if which == "in": self.t_in = None
            else: self.t_out = None
        else:
            s = parse_timecode(txt)
            if s is None:
                self._marker_text(); return
            (self.set_in if which == "in" else self.set_out)(s)
            return
        self._markers_changed()

    def _marker_text(self):
        self.in_edit.setText("" if self.t_in is None else fmt_tc(self.t_in))
        self.out_edit.setText("" if self.t_out is None else fmt_tc(self.t_out))

    def _markers_changed(self):
        completing_pair = (self.t_in is not None and self.t_out is not None
                           and self.timeline.zoom is None)
        self.timeline.t_in, self.timeline.t_out = self.t_in, self.t_out
        self.timeline.update()
        self._marker_text(); self._update_clip_info(); self._refresh_out_name()
        if completing_pair:
            # first time both In and Out are set (and the timeline hasn't
            # already been zoomed to something specific): zoom to the clip
            # automatically so a short span inside a long file isn't a
            # sliver a couple of pixels wide. Doesn't fight a zoom level
            # you've already picked on purpose.
            self.zoom_to_clip()

    def _speed(self):
        try:
            v = float(self.speed_combo.currentText().strip().lower().rstrip("x") or 1)
        except ValueError:
            v = 1.0
        return min(max(v, SPEED_MIN), SPEED_MAX)

    def _height(self):
        return RESOLUTIONS[self.res_grp.checkedButton().text()]

    def _update_clip_info(self):
        self._update_section_peeks()
        if self.t_in is None or self.t_out is None:
            self.len_label.setText("Set Start and End to define the clip.")
            self.clip_len_readout.setText("")
            return
        n = self.t_out - self.t_in
        sp = self._speed()
        self.len_label.setText(
            f"Length: <b>{fmt_len(n)}</b>" + (f" → <b style='color:{GREEN}'>{fmt_len(n / sp)}</b> at {sp:g}x"
                                              if sp != 1 else ""))
        # Same info as len_label above, but always visible right under the
        # timeline -- len_label lives in the scrollable Clip section on the
        # right, which scrolls out of view once Export Settings is open.
        # The speed is already shown right above as "(4x)" next to the
        # timecode, so it isn't repeated here.
        self.clip_len_readout.setText(
            f"Clip: {fmt_len(n)} → {fmt_len(n / sp)}" if sp != 1 else f"Clip: {fmt_len(n)}")

    def _res_changed(self):
        """Resolution (Original/1080p/720p/480p) toggle -- same idea as
        _speed_changed: refresh the Clip length readout AND the default
        File name's resolution suffix, which previously only followed
        whatever Resolution happened to be selected at export-name-build
        time rather than updating live as you change it."""
        self._update_clip_info()
        self._refresh_out_name()

    # ---------------------------------------------------------- export
    def _fmt_ext(self):
        return "mkv" if self.fmt_grp.checkedButton().text() == "MKV" else "mp4"

    def _fmt_changed(self):
        """Format (MP4/MKV) toggle -- only updates the extension chip next
        to the File name box, leaving whatever's typed there alone."""
        self.out_ext_chip.setText(f".{self._fmt_ext()}")
        self._update_section_peeks()
        self._update_export_enabled()

    def _refresh_out_name(self):
        """Regenerates the default File name (stem + clip span + speed +
        resolution) whenever the clip trim, speed, or resolution changes --
        same auto-naming as before, just writing into the name-only box
        now instead of a combined folder+name+extension path. Does NOT
        touch the extension (that's the Format toggle's job, via
        _fmt_changed) or the destination folder once one's been set."""
        self._update_section_peeks()
        if not self.video_path:
            return
        folder = self.out_folder_edit.text().strip() or os.path.dirname(self.video_path)
        # QFileDialog (and whatever the user typed) can hand back a folder
        # using "/" throughout, even on Windows -- os.path.join then only
        # adds a native "\" at the point it joins in the filename, leaving
        # a path with both separators mixed together. abspath+normpath
        # settles everything to one consistent, fully-qualified path
        # (relative to the app's own working directory otherwise, which
        # isn't what you'd want shown as "where this file is going").
        folder = os.path.normpath(os.path.abspath(folder)) if folder else folder
        self.out_folder_edit.setText(folder)
        stem = os.path.splitext(os.path.basename(self.video_path))[0]
        span = ""
        if self.t_in is not None and self.t_out is not None:
            span = f"_{fmt_tc(self.t_in, False).replace(':', '')}-{fmt_tc(self.t_out, False).replace(':', '')}"
        h = self._height()
        name = f"{stem}{span}_{self._speed():g}x{'_%dp' % h if h else ''}"
        self.out_name_edit.setText(name)
        self.out_name_error.setText("")
        self.out_name_edit.setStyleSheet(INPUT)
        self.out_ext_chip.setText(f".{self._fmt_ext()}")
        self._update_export_enabled()

    def _out_name_edited(self, text):
        """Auto-corrects the File name box as you type: a recognized but
        unsupported video extension (e.g. ".mov") is flagged as an error
        rather than silently exported as something broken; a supported one
        (".mp4"/".mkv") is stripped back out of the name and the Format
        toggle above switches to match, instead of producing a doubled-up
        "clip.mkv.mp4"."""
        stem, ext = os.path.splitext(text)
        ext = ext.lstrip(".").lower()
        if ext and ext in SUPPORTED_EXPORT_EXTS:
            btn = self.fmt_btns.get(ext.upper())
            if btn and not btn.isChecked():
                btn.setChecked(True)
                self._fmt_changed()
            self.out_name_edit.setText(stem)
            self.out_name_error.setText("")
            self.out_name_edit.setStyleSheet(INPUT)
        elif ext and ext in KNOWN_VIDEO_EXTS:
            self.out_name_error.setText(
                f'".{ext}" isn\'t a supported export format here -- pick MP4 or MKV above, '
                "or remove the extension from the name.")
            self.out_name_edit.setStyleSheet(INPUT_ERROR)
        else:
            self.out_name_error.setText("")
            self.out_name_edit.setStyleSheet(INPUT)
        self._update_export_enabled()

    def _update_export_enabled(self):
        """Export Clip stays disabled until there's a name to export to and
        it isn't sitting in the error state (an unsupported typed
        extension) -- while an export is actually running the button
        doubles as Cancel, so it always stays clickable then."""
        if self._proc is not None:
            self.export_btn.setEnabled(True)
            return
        ok = bool(self.out_name_edit.text().strip()) and not self.out_name_error.text()
        self.export_btn.setEnabled(ok)

    def browse_out_folder(self):
        start = self.out_folder_edit.text().strip() or (
            os.path.dirname(self.video_path) if self.video_path else "")
        folder = QFileDialog.getExistingDirectory(self, "Choose destination folder", start)
        if folder:
            self.out_folder_edit.setText(os.path.normpath(folder))

    def export_or_cancel(self):
        if self._proc is not None:
            self._proc.kill()
            return
        if not FFMPEG:
            QMessageBox.warning(self, "Export", "FFmpeg was not found.")
            return
        if self.t_in is None or self.t_out is None or self.t_out <= self.t_in:
            QMessageBox.information(self, "Export", "Set a Start point before an End point first.")
            return
        name = self.out_name_edit.text().strip()
        if not name:
            QMessageBox.information(self, "Export", "Type a file name first.")
            return
        stem, typed_ext = os.path.splitext(name)
        typed_ext = typed_ext.lstrip(".").lower()
        if typed_ext in SUPPORTED_EXPORT_EXTS:
            # Shouldn't normally happen (the name box auto-strips this as
            # you type -- see _out_name_edited), but guard against it
            # anyway rather than ever producing a doubled-up extension.
            name = stem
        elif typed_ext and typed_ext in KNOWN_VIDEO_EXTS:
            QMessageBox.warning(self, "Export",
                f'".{typed_ext}" isn\'t a supported export format -- this app can only export '
                "to MP4 or MKV. Remove the extension from the file name, or pick one of those "
                "two formats above.")
            return
        folder = self.out_folder_edit.text().strip() or (
            os.path.dirname(self.video_path) if self.video_path else "")
        if not folder:
            QMessageBox.information(self, "Export", "Choose a destination folder first.")
            return
        if not os.path.isdir(folder):
            QMessageBox.warning(self, "Export", "That destination folder doesn't exist anymore - choose another one.")
            return
        dst = os.path.normpath(os.path.join(folder, f"{name}.{self._fmt_ext()}"))
        if os.path.abspath(dst) == os.path.abspath(self.video_path):
            QMessageBox.warning(self, "Export", "Output can't overwrite the source video.")
            return
        if os.path.exists(dst) and QMessageBox.question(
                self, "Export", f"{os.path.basename(dst)} already exists. Replace it?") != QMessageBox.Yes:
            return
        overlay = None
        if self.ov_enable.isChecked() and self.ov_text_edit.toPlainText().strip():
            font_file = _font_file(self.overlay_box.font_family)
            if font_file is None:
                self.status.setText(
                    f"Text overlay needs a font file (couldn't find {self.overlay_box.font_family} "
                    "on this system) - exporting without it.")
            else:
                overlay = {
                    "text": self.ov_text_edit.toPlainText(),
                    "font_file": font_file,
                    "size_px": self.overlay_box.size_px,
                    "rel": tuple(self.overlay_box.rel),
                    "source_w": self.info["width"],
                    "source_h": self.info["height"],
                    "color": self.overlay_box.color,
                }
        cmd = build_export_cmd(self.video_path, dst, self.t_in, self.t_out, self._speed(),
                               self._height(), self.q_grp.checkedButton().text(), overlay=overlay)
        self._overlay_tmp = overlay.get("_tmp_path") if overlay else None
        self._out_len = (self.t_out - self.t_in) / self._speed()
        self._log_tail = []
        self._dst = dst
        self._proc = QProcess(self)
        if os.name == "nt":
            # Same reasoning as _SUBPROCESS_FLAGS above, but QProcess has
            # no creationflags= of its own -- this is Qt's equivalent hook
            # for it on Windows.
            self._proc.setCreateProcessArgumentsModifier(
                lambda args: setattr(args, "flags", args.flags | 0x08000000))  # CREATE_NO_WINDOW
        self._proc.setProcessChannelMode(QProcess.MergedChannels)
        self._proc.readyReadStandardOutput.connect(self._read_progress)
        self._proc.finished.connect(self._export_done)
        self._proc.errorOccurred.connect(lambda e: self._export_done(-1, QProcess.CrashExit)
                                         if e == QProcess.FailedToStart else None)
        self.export_btn.setText("Cancel export")
        self.progress.setValue(0)
        self.status.setText("Encoding…")
        self._proc.start(cmd[0], cmd[1:])

    def _read_progress(self):
        for line in bytes(self._proc.readAllStandardOutput()).decode("utf-8", "replace").splitlines():
            k, _, v = line.strip().partition("=")
            if k in ("out_time_us", "out_time_ms"):
                try:
                    done = int(v) / 1e6
                except ValueError:
                    continue
                pct = min(99, int(done / max(self._out_len, 0.001) * 100))
                self.progress.setValue(pct)
                self.status.setText(f"Encoding… {pct}%")
            elif "=" not in line and line.strip():
                self._log_tail = (self._log_tail + [line.strip()])[-4:]

    def _export_done(self, code, _status):
        proc, self._proc = self._proc, None
        self.export_btn.setText("Export Clip")
        self._update_export_enabled()
        if getattr(self, "_overlay_tmp", None):
            try: os.remove(self._overlay_tmp)
            except OSError: pass
            self._overlay_tmp = None
        if proc is not None and proc.exitStatus() == QProcess.CrashExit and code != 0:
            self.progress.setValue(0)
            self.status.setText("Export cancelled.")
            if os.path.exists(self._dst):
                try: os.remove(self._dst)
                except OSError: pass
            return
        if code != 0:
            self.progress.setValue(0)
            self.status.setText("Export failed: " + " | ".join(self._log_tail[-2:]))
            return
        self.progress.setValue(100)
        size = os.path.getsize(self._dst) / 1e6
        self.status.setText(f"Done - {os.path.basename(self._dst)}  ({size:.1f} MB)")


    def shutdown(self):
        """Stop background work cleanly (thumbnails, player, export)."""
        if self._thumbs:
            self._thumbs.stop(); self._thumbs.wait(3000)
        if self._proc is not None:
            self._proc.kill(); self._proc.waitForFinished(2000)
        self.player.stop()
        self.player.setVideoOutput(None)
        self.player.setSource(QUrl())

    def closeEvent(self, e):
        self.shutdown()
        super().closeEvent(e)


class VideoToolWindow(QMainWindow):
    def closeEvent(self, e):
        self.page.shutdown()
        super().closeEvent(e)

    def __init__(self, path=None):
        super().__init__()
        self.setWindowTitle("Battery Insight Studio - Runaway Studio")
        icon = _app_icon()
        if icon:
            self.setWindowIcon(icon)
        self.page = VideoToolPage()
        self.setCentralWidget(self.page)
        self.setMinimumSize(900, 600)
        self._fit_to_screen(1320, 800)
        self.destroyed.connect(lambda: None)
        if path:
            QTimer.singleShot(0, lambda: self.page.load(path))

    def _fit_to_screen(self, want_w, want_h):
        """Size and center the window on whichever screen it's opening on,
        so it's never bigger than the display and never lands with its
        title bar off-screen (can happen after a display-scaling change,
        a monitor swap, or moving the app between monitors with
        different scaling)."""
        screen = self.screen() or QApplication.primaryScreen()
        avail = screen.availableGeometry()  # excludes taskbar, in logical px
        w = min(want_w, avail.width() - 40)
        h = min(want_h, avail.height() - 40)
        w = max(w, self.minimumWidth())
        h = max(h, self.minimumHeight())
        x = avail.x() + (avail.width() - w) // 2
        y = avail.y() + (avail.height() - h) // 2
        self.setGeometry(x, y, w, h)


def _app_icon():
    """runaway_studio_icon_*.png next to this script, largest first -- None
    (never raises) if missing, so a missing icon just falls back to Qt's
    default rather than crashing the app."""
    from PySide6.QtGui import QIcon
    here = _base_dir()
    icon = QIcon()
    found = False
    for size in (256, 64, 32):
        p = os.path.join(here, f"runaway_studio_icon_{size}.png")
        if os.path.exists(p):
            icon.addFile(p)
            found = True
    return icon if found else None


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app_icon = _app_icon()
    if app_icon:
        app.setWindowIcon(app_icon)  # covers the taskbar icon on platforms that key off the app
    w = VideoToolWindow(sys.argv[1] if len(sys.argv) > 1 else None)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
