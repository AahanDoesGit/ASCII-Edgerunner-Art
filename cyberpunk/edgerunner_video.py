import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    import numpy as np
except ImportError:
    print("This script requires numpy.\nInstall it with:  pip install numpy")
    sys.exit(1)


# --- character ramps (dark -> bright) ---


CHARSETS = {
    # Long ASCII ramp, ordered by how much ink each glyph puts on the
    # cell. The fine gradations are what let faces and edges read at
    # small grid sizes - the default.
    "detailed": " .`^\\,:;Il!i><~+_-?][}{1)(|/tfjrxnuvczXYUJCLQ0OZmwqpdbkhao*#MW&8%B@$",

    # Classic chunky ASCII ramp. Fewer levels, bolder shapes.
    "classic": " .:-=+*#%@",

    # ASCII with a longer midtone section, for footage that lives in
    # the middle of the range.
    "mid": " .,:;i1tfLCG08@",

    # Solid block glyphs. Not really ASCII - reads as a neon display
    # panel, but large flat areas turn to mush.
    "blocks": " .:-=+*░▒▓█",

    # Half/quarter blocks only.
    "shade": " .:-░▒▓█",
}

DEFAULT_CHARSET = "detailed"
DEFAULT_FPS = 20
DEFAULT_MAX_WIDTH = 160
DEFAULT_QUANT = 4

# Single-color modes. Classic terminal ASCII art carries shape entirely
# in the glyphs, which stays legible where a full color grade does not.
MONO_COLORS = {
    "white":   (235, 240, 245),
    "green":   (120, 255, 150),
    "amber":   (255, 184,  72),
    "cyan":    (110, 235, 245),
    "magenta": (255,  90, 170),
}
DEFAULT_MAX_FRAME_SKIP = 5

CHAR_ASPECT = 0.5  # terminal cells are roughly twice as tall as wide


# --- cyberpunk color grade ---

# Luminance -> color ramp. Shadows fall to a deep violet-black,
# midtones push into hot magenta, highlights swing to cyan and then
# blow out near-white. This magenta/cyan split is what carries the
# Edgerunners look.

# The violet stop matters: interpolating straight from magenta to cyan
# in RGB passes through desaturated gray, which washes out the brightest
# midtones. Routing the transition through violet keeps it saturated.

GRADE_STOPS = np.array([0.00, 0.20, 0.42, 0.60, 0.75, 0.89, 1.00], dtype=np.float32)
GRADE_COLORS = np.array(
    [
        [6, 2, 18],        # near-black violet
        [58, 6, 62],       # deep purple
        [190, 18, 120],    # magenta
        [255, 62, 150],    # hot pink
        [148, 72, 240],    # violet (keeps the transition saturated)
        [70, 224, 238],    # cyan
        [228, 255, 255],   # blown-out white
    ],
    dtype=np.float32,
)


# --- color depth ---

# Not every terminal speaks 24-bit color. macOS Terminal.app in
# particular is 256-color only: sent 38;2;R;G;B it parses the
# parameters as separate SGR codes, so the picture comes out streaked
# with stray blue and green. Detect the capability and emit the
# sequence the terminal actually understands.

def detect_color_mode():
    """Return "truecolor" or "256" based on the environment."""
    if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        return "truecolor"
    term = os.environ.get("TERM", "")
    if "truecolor" in term or "24bit" in term:
        return "truecolor"
    # Terminal.app never sets COLORTERM; iTerm2, kitty, WezTerm,
    # Ghostty and Alacritty all do.
    if os.environ.get("TERM_PROGRAM") == "Apple_Terminal":
        return "256"
    return "256"


def fg(rgb, color_mode, bold=False):
    """Foreground SGR sequence for one color, in whichever depth the
    terminal supports. Used by the banner; the frame renderer builds
    its own sequences in bulk."""
    weight = "1;" if bold else ""
    if color_mode == "256":
        code = int(rgb_to_256(np.array([[list(rgb)]], dtype=np.float32))[0, 0])
        return f"\033[{weight}38;5;{code}m"
    r, g, b = rgb
    return f"\033[{weight}38;2;{r};{g};{b}m"


def rgb_to_256(arr):
    """Map an RGB array to xterm-256 indices.

    Each channel is matched against the 6-level color cube, and the
    result is compared with the 24-step gray ramp; whichever is closer
    to the original wins, which keeps near-neutral tones from picking
    up a color cast."""
    a = np.clip(arr, 0, 255)
    levels = np.array([0, 95, 135, 175, 215, 255], dtype=np.float32)

    idx = np.abs(a[..., None] - levels).argmin(axis=-1)      # (...,3)
    cube_rgb = levels[idx]
    cube_code = 16 + 36 * idx[..., 0] + 6 * idx[..., 1] + idx[..., 2]
    cube_err = ((cube_rgb - a) ** 2).sum(axis=-1)

    gray = a.mean(axis=-1)
    g_idx = np.clip(np.round((gray - 8.0) / 10.0), 0, 23).astype(np.int32)
    gray_val = (8.0 + 10.0 * g_idx)[..., None]
    gray_code = 232 + g_idx
    gray_err = ((gray_val - a) ** 2).sum(axis=-1)

    return np.where(gray_err < cube_err, gray_code, cube_code).astype(np.int32)


def luminance(arr):
    """Rec.601 luma of a float32 (rows, cols, 3) array, scaled 0..1."""
    return (
        0.299 * arr[..., 0] + 0.587 * arr[..., 1] + 0.114 * arr[..., 2]
    ) / 255.0


def auto_levels(lum, state, low_pct=2.0, high_pct=98.0, inertia=0.9):
    """Stretch a frame's luminance to use the full 0..1 range.

    This clip is strongly bimodal - crushed blacks and blown highlights
    with little in between - so without this the palette's midtone
    colors are barely reached. The black and white points are carried
    between frames with an exponential average, because recomputing
    them per frame makes the grade pump on every cut."""
    lo = float(np.percentile(lum, low_pct))
    hi = float(np.percentile(lum, high_pct))
    if hi - lo < 0.05:          # near-flat frame; leave it alone
        lo, hi = 0.0, 1.0

    if state["lo"] is None:
        state["lo"], state["hi"] = lo, hi
    else:
        state["lo"] += (lo - state["lo"]) * (1.0 - inertia)
        state["hi"] += (hi - state["hi"]) * (1.0 - inertia)

    span = max(state["hi"] - state["lo"], 1e-3)
    return np.clip((lum - state["lo"]) / span, 0.0, 1.0)


def apply_floor(lum, floor):
    """Crush the darkest values to true black.

    Lifting near-black regions turns the source's compression noise
    into visible colored speckle, which reads as dirt rather than
    style. Gating below the floor and rescaling what remains keeps
    shadows clean without flattening the rest of the range."""
    if floor <= 0.0:
        return lum
    return np.clip((lum - floor) / max(1.0 - floor, 1e-3), 0.0, 1.0)


def mono_shade(lum, color, depth=0.55):
    """Tint the whole frame one color, modulated by luminance.

    Classic ASCII art carries shape in the glyphs alone. Keeping a
    single hue and varying only its intensity removes the color noise
    that makes a graded frame hard to read, while `depth` keeps dark
    cells from collapsing to invisible."""
    base = np.array(color, dtype=np.float32)
    scale = ((1.0 - depth) + depth * np.clip(lum, 0.0, 1.0))[..., None]
    return base * scale


def neon_grade(arr, amount, lum=None):
    """Remap the frame onto the magenta/cyan ramp above.

    `amount` blends between the source color (0.0) and the full
    graded palette (1.0), so the grade can be dialed back if a shot
    loses too much of its original color. `lum` allows a pre-adjusted
    luminance (auto-levelled, floored) to drive the palette lookup."""
    if amount <= 0.0:
        return arr

    if lum is None:
        lum = luminance(arr)
    graded = np.empty_like(arr)
    for c in range(3):
        graded[..., c] = np.interp(lum, GRADE_STOPS, GRADE_COLORS[:, c])

    if amount >= 1.0:
        return graded
    return arr + (graded - arr) * amount


# --- chromatic aberration ---


def shift_columns(plane, offset):
    """Shift a 2D plane horizontally by `offset` columns, clamping at
    the edges (rather than wrapping, which would drag the far edge of
    the frame into view)."""
    if offset == 0:
        return plane
    n_cols = plane.shape[1]
    idx = np.clip(np.arange(n_cols) - offset, 0, n_cols - 1)
    return plane[:, idx]


def chromatic_aberration(arr, offset):
    """Pull the red and blue channels apart horizontally, leaving green
    in place. Edges pick up magenta on one side and cyan on the other,
    which reinforces the grade instead of fighting it."""
    if offset == 0:
        return arr
    out = arr.copy()
    out[..., 0] = shift_columns(arr[..., 0], offset)
    out[..., 2] = shift_columns(arr[..., 2], -offset)
    return out


# --- scanlines ---


def scanlines(arr, period, strength):
    """Darken every `period`-th character row to suggest a CRT raster."""
    if period <= 1 or strength <= 0.0:
        return arr
    out = arr.copy()
    out[period - 1 :: period] *= (1.0 - strength)
    return out


# --- glitch (off by default) ---


def glitch(arr, amount, rng):
    """Occasionally tear a few horizontal bands sideways. `amount` is
    the per-frame probability that any tearing happens at all."""
    if amount <= 0.0 or rng.random() > amount:
        return arr

    out = arr.copy()
    n_rows = arr.shape[0]
    for _ in range(rng.integers(1, 4)):
        height = int(rng.integers(1, max(2, n_rows // 8)))
        top = int(rng.integers(0, max(1, n_rows - height)))
        offset = int(rng.integers(-6, 7))
        band = out[top : top + height]
        for c in range(3):
            band[..., c] = shift_columns(band[..., c], offset)
    return out


# --- terminal / video info ---


def terminal_size(default=(120, 40)):
    """Columns and rows of the controlling terminal.

    COLUMNS/LINES win when set, so a caller can pin the render size
    without resizing the window. Otherwise ask the OS, and fall back to
    a sane default when stdout is a pipe rather than a terminal."""
    env_cols, env_rows = os.environ.get("COLUMNS"), os.environ.get("LINES")
    if env_cols and env_rows:
        try:
            return int(env_cols), int(env_rows)
        except ValueError:
            pass
    try:
        measured = os.get_terminal_size(sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        return default
    return measured.columns or default[0], measured.lines or default[1]


class PlaybackClock:
    """Decides, per frame, whether to wait, draw, or skip.

    Playback is paced against wall time from a fixed start, not against
    how long the previous frame took, so rendering cost cannot make the
    picture drift away from the audio. When the renderer falls behind,
    frames are abandoned rather than drawn late - but only up to
    `max_skip` in a row, so a slow passage degrades instead of blanking
    the screen."""

    DRAW, SKIP = "draw", "skip"

    def __init__(self, fps, max_skip):
        self.interval = 1.0 / fps
        self.max_skip = max_skip
        self.index = 0
        self.skipped = 0
        self.origin = None

    def start(self):
        self.origin = time.perf_counter()

    def next_action(self):
        """Advance one frame slot and say what to do with it."""
        due = self.origin + self.index * self.interval
        self.index += 1
        behind = time.perf_counter() - due

        if behind > self.interval and self.skipped < self.max_skip:
            self.skipped += 1
            return self.SKIP

        self.skipped = 0
        if behind < 0:
            time.sleep(-behind)
        return self.DRAW


def probe_dimensions(video):
    """Pixel size of the clip's first video stream, or None.

    Asks ffprobe for JSON rather than a delimited string: the parse is
    then a dictionary lookup instead of string splitting, and a clip
    with no video stream comes back as an empty list rather than
    something to distinguish from a malformed line."""
    if shutil.which("ffprobe") is None:
        return None

    command = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-print_format", "json",
        str(video),
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=10
        )
        streams = json.loads(completed.stdout).get("streams") or []
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return None

    if not streams:
        return None
    try:
        return int(streams[0]["width"]), int(streams[0]["height"])
    except (KeyError, TypeError, ValueError):
        return None


def detect_crop(video, probe_seconds=12):
    """Find the letterbox crop for a clip using ffmpeg's cropdetect.

    Phone-captured and re-encoded clips are usually a wide image padded
    into a tall canvas; rendering the padding wastes most of the
    terminal. Individual dark frames make cropdetect over-crop, so the
    most frequently reported box across the probe window wins rather
    than the last one."""
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-i", str(video),
                "-vf", "cropdetect=limit=24:round=2:reset=0",
                "-t", str(probe_seconds), "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:
        return None

    counts = {}
    for match in re.finditer(r"crop=(\d+):(\d+):(\d+):(\d+)", result.stderr):
        box = tuple(int(g) for g in match.groups())
        counts[box] = counts.get(box, 0) + 1

    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def parse_crop(value, video):
    """Resolve the --crop argument to a (w, h, x, y) box or None."""
    if value in (None, "none"):
        return None
    if value == "auto":
        return detect_crop(video)
    try:
        w, h, x, y = (int(part) for part in value.split(":"))
        return (w, h, x, y)
    except ValueError:
        print(f"\u26a0  Could not read --crop {value!r}; expected W:H:X:Y, auto, or none.")
        return None


def compute_render_size(term_cols, term_rows, video_dims, max_width,
                        margin=True, stretch=False):
    """Fit the render inside the terminal while preserving the source
    aspect ratio.

    Both dimensions are constraints. Clamping rows to the terminal
    height without also narrowing cols would stretch the frame
    horizontally - which is exactly what happens with vertical
    (9:16) footage, where the height limit always binds first."""
    # Fullscreen uses every cell; windowed leaves room for the shell
    # prompt and a little breathing space.
    if margin:
        max_cols = max(term_cols - 2, 10)
        max_rows = max(term_rows - 3, 5)
    else:
        max_cols = max(term_cols, 10)
        max_rows = max(term_rows, 5)

    if max_width:
        max_cols = min(max_cols, max_width)

    if stretch:
        # Fill the window completely, aspect ratio be damned.
        return max_cols, max_rows

    if video_dims:
        vid_w, vid_h = video_dims
        video_aspect = vid_h / vid_w
    else:
        video_aspect = 9 / 16

    # Rows per column, once the terminal cell shape is accounted for.
    rows_per_col = video_aspect * CHAR_ASPECT

    cols = max_cols
    rows = int(round(cols * rows_per_col))

    if rows > max_rows:
        # Height is the binding constraint: give up columns rather
        # than distort the picture.
        rows = max_rows
        cols = int(round(rows / rows_per_col))
        cols = max(10, min(cols, max_cols))

    rows = max(5, rows)
    return cols, rows


def build_filter(crop, fps, cols, rows):
    """Compose the ffmpeg filter chain: crop the letterbox first, then
    resample to the target rate and character grid."""
    parts = []
    if crop:
        w, h, x, y = crop
        parts.append(f"crop={w}:{h}:{x}:{y}")
    parts.append(f"fps={fps}")
    parts.append(f"scale={cols}:{rows}")
    return ",".join(parts)


class FrameReader:
    """Pulls fixed-size raw frames off a pipe.

    The buffer is allocated once and filled in place with readinto, so
    a frame costs no per-frame allocation - which matters at 20+ fps
    with grids in the tens of thousands of cells. `next_frame` returns
    a float array built from that buffer, or None once the pipe runs
    dry or ends mid-frame."""

    def __init__(self, stream, rows, cols, channels=3):
        self.stream = stream
        self.shape = (rows, cols, channels)
        self.nbytes = rows * cols * channels
        self._buffer = bytearray(self.nbytes)
        self._view = memoryview(self._buffer)

    def _fill(self):
        """Fill the whole buffer, looping because a pipe read can come
        back short. False means the stream ended before a full frame."""
        filled = 0
        while filled < self.nbytes:
            got = self.stream.readinto(self._view[filled:])
            if not got:
                return False
            filled += got
        return True

    def next_frame(self):
        if not self._fill():
            return None
        raw = np.frombuffer(self._buffer, dtype=np.uint8)
        return raw.reshape(self.shape).astype(np.float32)


# --- ansi rendering ---


def render_ascii(arr, shape_lum, quant_step, ramp, bloom_threshold,
                 color_mode="truecolor"):
    """Build the ANSI text for one frame.

    Glyph and color are driven by different signals on purpose.
    `shape_lum` - the tone-mapped luminance of the *source* - picks the
    character, so the ramp always tracks real brightness. Color comes
    from `arr`, the styled frame.

    Deriving the glyph from the styled frame instead looks equivalent
    but is not: the palette is not monotonic in luminance (violet is
    darker than the hot pink below it), so a brighter part of the
    picture could be assigned a darker character. That inverts edges
    and makes the image unreadable. Scanlines have the same problem -
    they must dim color without punching holes in the structure.

    Characters are emitted in runs: a run continues only while the
    glyph, the quantized color, and the bold (bloom) flag all stay the
    same, so one escape sequence covers many cells."""
    disp = np.clip(arr, 0, 255)
    lum = np.clip(shape_lum, 0.0, 1.0)

    char_idx = np.clip(
        (lum * (len(ramp) - 1)).astype(np.int32), 0, len(ramp) - 1
    )
    bold = (lum >= bloom_threshold) if bloom_threshold <= 1.0 else np.zeros_like(lum, dtype=bool)

    # One integer per cell describing everything that affects output:
    # colour, the bold flag and the glyph. Cells sharing a code can
    # share a single escape sequence.
    if color_mode == "256":
        colour_code = rgb_to_256(disp)
        palette = None
    else:
        step = max(quant_step, 1)
        quantized = (disp.astype(np.int32) // step) * step
        # Pack the channels so run detection is one integer comparison
        # rather than three.
        colour_code = (
            (quantized[..., 0] << 16) | (quantized[..., 1] << 8) | quantized[..., 2]
        )
        palette = quantized

    cell = (
        (colour_code.astype(np.int64) << 9)
        | (bold.astype(np.int64) << 8)
        | char_idx.astype(np.int64)
    )

    # Run starts for the whole grid at once: a cell opens a run when it
    # differs from its left neighbour, and column zero always does.
    opens_run = np.ones(cell.shape, dtype=bool)
    np.not_equal(cell[:, 1:], cell[:, :-1], out=opens_run[:, 1:])

    n_rows, n_cols = cell.shape
    lines = []

    for y in range(n_rows):
        starts = np.flatnonzero(opens_run[y])
        # Each run reaches the next start; the last reaches the edge.
        lengths = np.diff(np.append(starts, n_cols))

        pieces = []
        previous_sgr = None
        for start, length in zip(starts.tolist(), lengths.tolist()):
            glyph = ramp[char_idx[y, start]]
            weight = "1;" if bold[y, start] else "22;"
            if color_mode == "256":
                sgr = f"\033[{weight}38;5;{colour_code[y, start]}m"
            else:
                r, g, b = palette[y, start]
                sgr = f"\033[{weight}38;2;{r};{g};{b}m"

            # Adjacent runs often differ only in glyph; repeating an
            # identical escape sequence is pure overhead.
            if sgr != previous_sgr:
                pieces.append(sgr)
                previous_sgr = sgr
            pieces.append(glyph * length)

        pieces.append("\033[0m")
        lines.append("".join(pieces))

    return "\n".join(lines)


# --- main ---


def parse_args():
    p = argparse.ArgumentParser(
        description="Cyberpunk terminal ASCII video player: neon grade, "
                    "scanlines, chromatic aberration, synced audio."
    )
    p.add_argument("video", help="Clip to play")
    p.add_argument("--charset", choices=list(CHARSETS.keys()), default=DEFAULT_CHARSET,
                   help="Brightness->character ramp")
    p.add_argument("--fps", type=float, default=DEFAULT_FPS, help="Frames per second to aim for")
    p.add_argument("--width", type=int, default=None,
                   help="Max render width in columns. 0 removes the cap "
                        f"(default {DEFAULT_MAX_WIDTH}, uncapped with --fullscreen)")
    p.add_argument("--fullscreen", "-f", action="store_true",
                   help="Use the whole window: no width cap, centered, on the "
                        "alternate screen so your scrollback survives")
    p.add_argument("--stretch", action="store_true",
                   help="Fill the window completely, ignoring aspect ratio")
    p.add_argument("--crop", default="auto",
                   help="Letterbox crop: auto (detect), none, or explicit W:H:X:Y")

    p.add_argument("--levels", action="store_true", default=True,
                   help="Auto-stretch each frame's luminance (on by default)")
    p.add_argument("--no-levels", dest="levels", action="store_false",
                   help="Disable auto-levels and grade the raw luminance")
    p.add_argument("--floor", type=float, default=0.12,
                   help="Crush luminance below this to black, killing shadow speckle")
    p.add_argument("--gamma", type=float, default=0.75,
                   help="Midtone curve applied after levels. <1 lifts midtones into "
                        "the palette's colored band; 1.0 is no curve")
    p.add_argument("--mono", nargs="?", const="white", default=None,
                   choices=list(MONO_COLORS.keys()),
                   help="Single-color ASCII instead of the neon grade "
                        "(white, green, amber, cyan, magenta)")
    p.add_argument("--grade", type=float, default=0.55,
                   help="Neon grade strength, 0.0=source color .. 1.0=full magenta/cyan palette")
    p.add_argument("--aberration", type=int, default=1,
                   help="Chromatic aberration offset in columns (0 disables)")
    p.add_argument("--scanline-period", type=int, default=3,
                   help="Darken every Nth row (0 or 1 disables)")
    p.add_argument("--scanline-strength", type=float, default=0.30,
                   help="How much to darken scanline rows, 0.0..1.0")
    p.add_argument("--bloom", type=float, default=0.72,
                   help="Luminance above which cells render bold, 0.0..1.0 (>1.0 disables)")
    p.add_argument("--glitch", type=float, default=0.0,
                   help="Per-frame probability of horizontal tearing, 0.0..1.0")

    p.add_argument("--color", choices=["auto", "truecolor", "256"], default="auto",
                   help="Color depth. auto detects; macOS Terminal.app needs 256")
    p.add_argument("--quant", type=int, default=DEFAULT_QUANT,
                   help="Rounds colours before run detection, so runs merge and fewer escapes are sent")
    p.add_argument("--max-frame-skip", type=int, default=DEFAULT_MAX_FRAME_SKIP,
                   help="Ceiling on frames abandoned in a row while catching up")
    p.add_argument("--audio-delay", type=float, default=0.0,
                   help="Nudge the sound earlier or later, in seconds")
    p.add_argument("--no-audio", action="store_true", help="Render silently")
    return p.parse_args()


def end_process(process, grace=2.0):
    """Stop a child process, escalating if it ignores the polite ask.

    Called from a finally block during teardown, so it must not raise:
    the process may already be gone, or refuse to die, and neither is
    worth failing the exit over."""
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=grace)
        except (OSError, subprocess.SubprocessError):
            pass
    except (OSError, subprocess.SubprocessError):
        pass


def main():
    args = parse_args()

    if os.name == "nt":
        os.system("")  # enable ANSI escape processing on Windows consoles

    video = Path(os.path.expanduser(args.video)).resolve()
    if not video.is_file():
        print(f"❌ Video not found: {video}")
        return

    if shutil.which("ffmpeg") is None:
        print("❌ FFmpeg not found.")
        return
    if not args.no_audio and shutil.which("ffplay") is None:
        print("❌ FFplay not found. Re-run with --no-audio to render without sound.")
        return

    ramp = CHARSETS[args.charset]
    rng = np.random.default_rng()
    color_mode = detect_color_mode() if args.color == "auto" else args.color

    term_cols, term_rows = terminal_size()
    video_dims = probe_dimensions(video)

    crop = parse_crop(args.crop, video)
    if crop:
        # Size the render against the cropped picture, not the padded
        # canvas, or the aspect correction fits the black bars too.
        video_dims = (crop[0], crop[1])

    # --width unset means "the usual cap", except in fullscreen where
    # the point is to use everything.
    if args.width is None:
        max_width = 0 if args.fullscreen else DEFAULT_MAX_WIDTH
    else:
        max_width = args.width

    cols, rows = compute_render_size(
        term_cols, term_rows, video_dims, max_width,
        margin=not args.fullscreen,
        stretch=args.stretch,
    )

    # Centering offsets, so a frame that cannot fill the window sits in
    # the middle of it rather than in the top-left corner.
    if args.fullscreen:
        pad_left = max((term_cols - cols) // 2, 0)
        pad_top = max((term_rows - rows) // 2, 0)
    else:
        pad_left = pad_top = 0

    print()
    pink = fg((255, 62, 150), color_mode)
    cyan = fg((70, 224, 238), color_mode, bold=True)
    rule = pink + "+------------------------------------------+\033[0m"
    print(rule)
    print(pink + "|\033[0m   " + cyan + "E D G E R U N N E R   A S C I I\033[0m      "
          + pink + "|\033[0m")
    print(rule)
    print()
    print(f"Video:      {video.name}")
    print(f"Charset:    {args.charset} ({len(ramp)} levels)")
    if crop:
        print(f"Crop:       {crop[0]}x{crop[1]} at +{crop[2]}+{crop[3]}")
    print(f"Render:     {cols} x {rows} chars")
    print(f"Target FPS: {args.fps}")
    look = f"mono/{args.mono}" if args.mono else f"grade {args.grade}"
    print(f"Look:       {look}   Aberration: {args.aberration}   "
          f"Scanlines: {args.scanline_period}/{args.scanline_strength}")
    print(f"Levels:     {'auto' if args.levels else 'off'}   Floor: {args.floor}   "
          f"Gamma: {args.gamma}")
    print(f"Color:      {color_mode}"
          + ("   (Terminal.app is 256-only; iTerm2/Ghostty/kitty give true color)"
             if color_mode == "256" else ""))
    print()
    print("Starting... (Ctrl+C to stop)")
    time.sleep(0.6)

    clock = PlaybackClock(args.fps, args.max_frame_skip)

    # Audio starts first; the moment it starts is the sync clock.
    audio_process = None
    if not args.no_audio:
        audio_process = subprocess.Popen(
            ["ffplay", "-nodisp", "-vn", "-autoexit", "-loglevel", "quiet", str(video)]
        )

    if args.audio_delay > 0:
        time.sleep(args.audio_delay)

    # The sync origin is taken here, right after audio starts and
    # before ffmpeg spawns, so process startup does not push the video
    # behind the sound.
    clock.start()

    video_process = subprocess.Popen(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", str(video),
            "-vf", build_filter(crop, args.fps, cols, rows),
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=10 ** 8,
    )

    if args.fullscreen:
        # Alternate screen buffer: the render gets the whole window and
        # the user's scrollback comes back untouched on exit.
        sys.stdout.write("\033[?1049h")
    sys.stdout.write("\033[2J\033[H")   # clear screen, home cursor
    sys.stdout.write("\033[?25l")        # hide cursor
    sys.stdout.flush()

    reader = FrameReader(video_process.stdout, rows, cols)
    levels_state = {"lo": None, "hi": None}

    try:
        while True:
            arr = reader.next_frame()
            if arr is None:
                break

            if clock.next_action() is PlaybackClock.SKIP:
                continue

            # Luminance is prepared before the grade so the palette
            # lookup sees a full-range, de-speckled signal.
            lum = luminance(arr)
            if args.levels:
                lum = auto_levels(lum, levels_state)
            lum = apply_floor(lum, args.floor)
            if args.gamma != 1.0:
                # Push more of the picture into the 0.42-0.89 band where
                # the palette actually has color; without it a
                # high-contrast source skips straight from black to white.
                lum = np.power(lum, args.gamma)

            # Effects order matters: grade first so the aberration
            # splits already-neon colors, then scanlines darken on top.
            if args.mono:
                arr = mono_shade(lum, MONO_COLORS[args.mono])
            else:
                arr = neon_grade(arr, args.grade, lum)
            arr = chromatic_aberration(arr, args.aberration)
            arr = glitch(arr, args.glitch, rng)
            arr = scanlines(arr, args.scanline_period, args.scanline_strength)

            # `lum`, not the styled frame, chooses the glyphs - see the
            # note in render_ascii.
            frame_text = render_ascii(arr, lum, args.quant, ramp, args.bloom,
                                      color_mode)

            if pad_left:
                indent = " " * pad_left
                frame_text = "\n".join(
                    indent + line for line in frame_text.split("\n")
                )

            sys.stdout.write("\033[H")
            if pad_top:
                sys.stdout.write("\n" * pad_top)
            sys.stdout.write(frame_text)
            sys.stdout.flush()

    except KeyboardInterrupt:
        sys.stdout.write("\033[0m\n\n⏹ Stopped.\n")

    finally:
        sys.stdout.write("\033[0m")
        sys.stdout.write("\033[?25h")  # show cursor
        if args.fullscreen:
            sys.stdout.write("\033[?1049l")  # restore the normal screen
        sys.stdout.flush()

        for child in (video_process, audio_process):
            end_process(child)

    print("\n\n✅ Finished.")


if __name__ == "__main__":
    main()
