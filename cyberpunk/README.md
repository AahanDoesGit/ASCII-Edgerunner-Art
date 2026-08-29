# Edgerunner ASCII

Plays video as ASCII art in the terminal, with a cyberpunk tone-mapping and
effects stack in front of the ANSI output and audio kept in sync.

## Run it

```
python3 cyberpunk/edgerunner_video.py clips/edgerunners.mp4
```

Audio plays through FFplay and acts as the sync clock; frames are dropped
rather than allowed to drift. Ctrl+C stops.

## Pipeline

```
ffmpeg -> crop letterbox -> scale to char grid
       -> luminance -> auto-levels -> floor -> gamma
       -> neon grade -> chromatic aberration -> glitch -> scanlines
       -> run-length ANSI
```

Tone mapping runs before the grade because the palette only has color in the
middle of its range; feeding it raw luminance from high-contrast footage skips
straight from black to white and the color never appears.

**Glyph and color come from different signals.** The character is picked from
the tone-mapped source luminance; only the color comes from the styled frame.
This matters more than it sounds: the palette is not monotonic in luminance
(violet is darker than the hot pink below it), so choosing the glyph from the
styled frame lets a brighter part of the picture get a darker character. Edges
invert and the image stops reading as anything. Scanlines have the same
failure mode - they dim color without punching holes in the structure.

## Options

### Framing
| Flag | Default | What it does |
|---|---|---|
| `--crop` | `auto` | Letterbox crop. `auto` detects it, `none` disables, or give `W:H:X:Y` |
| `--width` | 160 | Max render width in columns. `0` removes the cap |
| `--fullscreen` / `-f` | off | Use the whole window: no width cap, centered, on the alternate screen so scrollback survives |
| `--stretch` | off | Fill the window completely, ignoring aspect ratio |
| `--fps` | 20 | Target playback rate |
| `--charset` | `detailed` | `detailed`, `classic`, `mid` (ASCII) or `blocks`, `shade` (block glyphs) |

Both terminal dimensions are treated as constraints — if height binds first
(which it always does for vertical video) columns are given up rather than
stretching the picture.

### Tone mapping
| Flag | Default | What it does |
|---|---|---|
| `--levels` / `--no-levels` | on | Stretch each frame's luminance to full range, smoothed across frames so it doesn't pump on cuts |
| `--floor` | 0.12 | Crush luminance below this to black. Stops the grade turning compression noise into colored speckle |
| `--gamma` | 0.75 | Midtone curve. Below 1.0 lifts midtones into the palette's colored band |

### Look
| Flag | Default | What it does |
|---|---|---|
| `--mono` | off | Single-color ASCII: `white`, `green`, `amber`, `cyan`, `magenta`. Overrides the grade and is by far the most legible mode |
| `--grade` | 0.55 | Blend toward the magenta/cyan palette. 0.0 = source color |
| `--aberration` | 1 | Red/blue channel split in columns. 0 disables |
| `--scanline-period` | 3 | Darken every Nth row. 0 or 1 disables |
| `--scanline-strength` | 0.30 | How much those rows darken |
| `--bloom` | 0.72 | Luminance above which cells render bold. Above 1.0 disables |
| `--glitch` | 0.0 | Per-frame chance of horizontal tearing. Off by default |

### Color depth
| Flag | Default | What it does |
|---|---|---|
| `--color` | `auto` | `truecolor`, `256`, or `auto` to detect |

**macOS Terminal.app is 256-color only.** Sent a 24-bit sequence
(`38;2;R;G;B`) it parses the parameters as separate SGR codes, so the picture
comes out streaked with stray blue and green that has nothing to do with the
palette. `auto` detects this and emits `38;5;N` instead.

Terminals that do support true color: iTerm2, Ghostty, kitty, WezTerm,
Alacritty. They all set `COLORTERM`, which is what the detection looks for.

Playback also takes `--quant`, `--audio-delay`, `--no-audio`, `--max-frame-skip`.

## Tuning notes

- **Dark, high-contrast sources** (most night-time anime) need the tone mapping.
  With `--no-levels --gamma 1.0` the grade barely shows, because almost no
  pixels land where the palette has color.
- **Vertical clips** end up with few columns, since height binds first. A taller
  terminal window or a smaller font buys resolution directly.
- **`--fullscreen` matters most on big windows.** The default 160-column cap
  leaves a 400x100 terminal using a quarter of its cells; fullscreen roughly
  triples that. It stays short of 100% because aspect ratio is preserved -
  `--stretch` fills the rest at the cost of distorting the picture.
- Fullscreen renders cost more per frame, but there is headroom: about 19 ms
  at 267x100, against 50 ms available at 20 fps.
- **Stutter**: drop `--width` before `--fps`. Cost scales with character count.
- **Legibility beats styling on dark footage.** The color grade spreads hue
  across neighbouring cells, which competes with the glyphs for the eye. If a
  clip is hard to follow, `--mono` first, then add effects back one at a time.
- The color ramp is `GRADE_STOPS` / `GRADE_COLORS` at the top of the script.
  Editing those stops retargets the whole look; the violet stop is there
  deliberately, to stop the magenta-to-cyan transition passing through gray.
