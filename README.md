# ⚡ ASCII Edgerunner

A high-performance, feature-rich Cyberpunk-themed ASCII video player that renders video directly in your terminal with synced audio, custom neon color grading, CRT scanline emulation, chromatic aberration, and digital glitch tearing.

Designed to capture the aesthetic of *Cyberpunk: Edgerunners*, this script leverages modern terminal capabilities (Truecolor / xterm-256) and highly optimized NumPy arrays to render video in real-time.

---

## ✨ Features

* **🎨 Cyberpunk Neon Grade**: Custom luminance-to-color mapping that splits shadows into deep violet-black, midtones into hot pink/magenta, and highlights into neon cyan and blown-out white.
* **🔊 Synced Audio Playback**: Spawns `ffplay` for audio and syncs video frame rates against wall-clock time, dynamically dropping frames if the terminal render queue falls behind to prevent drift.
* **📺 CRT & Analog Emulation**:
  * **Chromatic Aberration**: Offsets the red and blue channels horizontally to create classic retro color fringing.
  * **Scanlines**: Emulates raster beam lines by periodically darkening character rows.
  * **Glitch Tearing**: Optional random horizontal band displacement simulating video signal instability.
* **⚖️ Adaptive Auto-Levels**: Dynamic contrast optimization with an exponential moving average (inertia) to keep scenes vibrant while preventing flickering on cuts.
* **🚀 Run-Length ANSI Optimization**: Combines RGB color, bold weight, and character glyph indices into single 64-bit integers to identify identical runs of cells, minimizing terminal escape sequence overhead.

---

## 📋 Prerequisites

Before running the player, make sure you have the following system and Python dependencies installed:

### 1. System Dependencies (FFmpeg & FFplay)

The player uses `ffmpeg` to decode video frames, `ffprobe` to determine video dimensions, and `ffplay` to output synchronized audio.

* **macOS** (via Homebrew):
  ```bash
  brew install ffmpeg
  ```
* **Linux** (Debian/Ubuntu):
  ```bash
  sudo apt update
  sudo apt install ffmpeg
  ```
* **Windows** (via winget):
  ```cmd
  winget install Gyan.FFmpeg
  ```

### 2. Python Dependencies

* **Python 3.8+**
* **NumPy**

Install NumPy using pip:
```bash
pip install numpy
```

---

## 🚀 Quick Start

1. Clone or download this repository.
2. Put a video file (e.g., `edgerunners.mp4`) into the `clips/` directory.
3. Run the player:
   ```bash
   python cyberpunk/edgerunner_video.py clips/edgerunners.mp4
   ```

---

## ⚙️ Configuration & Options

The script offers a wide range of command-line arguments to customize your playback experience:

| Option | Default | Description |
| :--- | :--- | :--- |
| `video` | *Required* | Path to the video file to play. |
| `--charset` | `detailed` | The character ramp to map brightness: `detailed`, `classic`, `mid`, `blocks`, `shade`. |
| `--fps` | `20` | Target frames per second. |
| `--width` | `160` | Maximum rendering width in character columns. |
| `--fullscreen`, `-f` | `False` | Render across the entire terminal, centering the video. |
| `--stretch` | `False` | Fill the window completely, ignoring aspect ratio. |
| `--crop` | `auto` | Detect and crop black bars (`auto`, `none`, or explicit `W:H:X:Y`). |
| `--no-levels` | `False` | Disable real-time auto-level contrast stretching. |
| `--floor` | `0.12` | Luminance threshold below which pixels are crushed to pure black. |
| `--gamma` | `0.75` | Midtone correction. Values `< 1.0` shift dark midtones into colors. |
| `--mono` | `None` | Render in a single color tint: `white`, `green`, `amber`, `cyan`, `magenta`. |
| `--grade` | `0.55` | Strength of the magenta/cyan neon color grade (0.0 to 1.0). |
| `--aberration` | `1` | Horizontal column offset for chromatic aberration (0 to disable). |
| `--scanline-period`| `3` | Periodicity of scanlines (darkens every Nth line; 0 to disable). |
| `--scanline-strength`| `0.30` | Intensity of the scanlines (0.0 to 1.0). |
| `--bloom` | `0.72` | Luminance threshold above which characters are rendered bold (0.0 to 1.0). |
| `--glitch` | `0.0` | Probability (0.0 to 1.0) of horizontal tearing glitches per frame. |
| `--color` | `auto` | Color depth: `auto`, `truecolor` (24-bit), or `256` (xterm-256). |
| `--no-audio` | `False` | Play the video silently (without spawning `ffplay`). |

### Example Command

To play in fullscreen mode with 24-bit Truecolor, some glitch effects, and higher FPS:
```bash
python cyberpunk/edgerunner_video.py clips/edgerunners.mp4 --fullscreen --fps 24 --glitch 0.05
```

---

## 🛠️ How It Works (Technical Details)

### 1. High-Performance Frame Pipeline
Instead of loading frames into memory as image objects, the player opens `ffmpeg` as a subprocess and streams raw RGB24 bytes via `stdout`.
A custom `FrameReader` reads from the process stream directly into a single pre-allocated `bytearray` using `readinto()`, avoiding garbage collection overhead.

### 2. NumPy Array Vectorization
All image processing operations (auto-levels, gamma adjustments, color grading, chromatic aberration, scanlines, and glitches) are fully vectorized using NumPy:
* **Auto-Levels**: Calculates the 2nd and 98th percentiles of luminance to establish the white and black points, updating them with an exponential smoothing factor to prevent sudden flickering.
* **Neon Grade**: Colorizes grayscale luminance using a custom 1D lookup table interpolation for R, G, and B values.
* **Chromatic Aberration**: Shifts the red and blue color planes horizontally using array indexing.

### 3. Escape Sequence Optimization
Writing ANSI escape sequences cell-by-cell is extremely slow and can saturate the terminal's input buffer. To combat this:
1. Every cell's color, bold flag, and character index are packed into a single 64-bit integer.
2. The code computes the difference between adjacent cells.
3. Only when a cell's packed configuration differs from its predecessor is a new ANSI escape sequence generated, rendering identical spans of characters in a single run.

---

## ⚠️ Notes

* **macOS Terminal.app**: Terminal.app does not support 24-bit Truecolor and will corrupt color displays when Truecolor sequences are sent. The script automatically detects Terminal.app and falls back to quantized xterm-256 colors. For the full Truecolor experience, use modern terminal emulators like **iTerm2**, **Ghostty**, **kitty**, **WezTerm**, or **Alacritty**.
* **Audio Drift**: Audio sync is maintained by comparing current playback time to the monotonic clock start. If the system is slow, the script will skip rendering intermediate video frames to keep up with the audio.
