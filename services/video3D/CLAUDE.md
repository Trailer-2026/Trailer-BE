# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A renderer that turns a GPS travel route into a vertical (1080x1920) H.264 MP4. A Mapbox GL JS WebGL 3D map is driven frame-by-frame inside headless Playwright Chromium, each frame is captured as PNG/JPEG, and frames are streamed into FFmpeg (`libx264`) via a pipe. Output is portrait, 30 FPS by default.

Two languages, one pipeline:
- `render_video.py` — orchestration: arg parsing, travel-data validation, timeline planning, Chromium driving, frame capture, photo-segment compositing (Pillow), FFmpeg piping. **2000+ lines, single file.**
- `map_animation.js` — runs in the browser: builds the route, computes camera/bearing per frame, draws Mapbox layers. Loaded by `map.html`. Python calls into it via `page.evaluate(...)`.

## Setup & commands

A Mapbox **public** access token is required. Create `.env` in the project root:
```
MAPBOX_ACCESS_TOKEN=pk.your_public_token_here
```
Without it the program prints a message and exits with code 1. `.env` is not committed. FFmpeg (and optionally `ffprobe`) must be on `PATH`.

All work happens in the `trailer3d` conda env:
```bash
conda create -n trailer3d python=3.11 -y
conda run -n trailer3d python -m pip install -r requirements.txt
conda run -n trailer3d playwright install chromium
```

Run:
```bash
conda run -n trailer3d python render_video.py            # full 1080x1920 / 30fps render
conda run -n trailer3d python render_video.py --quick    # 540x960 / 15fps fast test
conda run -n trailer3d python render_video.py --fast     # full res, cdp-jpeg + small queue, no warmup
conda run -n trailer3d python render_video.py --benchmark-frames 30   # render only first N frames, estimate full time
conda run -n trailer3d python render_video.py --travel-data assets/travel_data.json
conda run -n trailer3d python render_video.py --bearing-test-route    # short sharp-turn route for bearing tuning
```

Outputs auto-increment: `output/travel_3d_1.mp4`, `_2.mp4`, … (existing files are never overwritten — `next_output_path()`).

### BGM
`--bgm <path>` (or a top-level `"bgm": "..."` field in the travel-data JSON; `--bgm` wins) muxes a music track into the rendered video. The video is rendered silent as before, then `mux_bgm_into_video()` runs a second FFmpeg pass that stream-copies the video (`-c:v copy`, no re-encode), loops the audio to fill the duration (`-stream_loop -1 ... -shortest`), re-encodes audio to AAC 192k, and adds a 2s fade-out. BGM files live in `bgm/` (gitignored — Pixabay license restricts redistribution; see `bgm/CREDITS.md`). Skipped for `--benchmark-frames`.

### Builder server (`server.py` + `builder.html`)
A FastAPI app for building renders interactively instead of hand-editing JSON. `GET /` serves `builder.html` (Mapbox token injected server-side via `__MAPBOX_TOKEN__`); click the map to add GPS points, attach photos per point, pick a BGM track, and choose a **render engine**. `POST /api/render` saves uploaded photos to `assets/uploads/<job>/`, writes a `travel_data.json` (with the `bgm` field), then dispatches on the `engine` form field:
- `engine=local` → subprocess `render_video.py` (`sys.executable`, trailer3d env); output parsed from `"출력 예정 파일:"`.
- `engine=modal` → subprocess `modal run modal_render.py --travel-data <rel> --mode {quality|quality-fast}`; output parsed from the entrypoint's `"저장 위치:"` line. `modal_render.py`'s `render()`/`main()` take a `travel_data` arg and pass `--travel-data` into the container; the `assets/` (uploads) and `bgm/` dirs ride along via `add_local_dir`, and BGM is picked up from the JSON's `bgm` field inside the container.

`GET /api/bgm` lists tracks with cleaned display names (`bgm_display_name()` parses Pixabay attribution filenames). Run from this directory: `conda run -n trailer3d python -m uvicorn server:app --port 8200` (needs `fastapi`/`uvicorn`/`python-multipart`, added to `requirements.txt`; `modal` must be installed + logged in for the Modal engine).

There is **no test suite, linter, or build step.** Verification is done by rendering (use `--quick` or `--benchmark-frames`) and inspecting `output/` and `debug/` images. `--save-frames` dumps the individual frames to `frames/`.

## Architecture

### Data model (Python → browser)
Input is `trackPoints` + `mediaPoints` (see `assets/travel_data.json`):
- `trackPoints` — the full GPS path. Plain track points do **not** produce markers or stops.
- `mediaPoints` — named places that index into trackPoints via `trackIndex`. **Every media point is a stop**: with `photos` it holds `stop_seconds` then plays the photo sequence; without photos it holds `arrival_hold_seconds` (default 1.5s) so the place is still visible. Plain trackPoints (no media point) are flown through.

`load_raw_travel_data` → `validate_travel_data` (lenient: bad media points warn and are skipped, missing photos warn and are dropped, duplicate trackIndex merges photos) → `travel_data_for_browser` serializes it and injects it as `window.TRAVEL_DATA` via `page.add_init_script`. `map_animation.js` re-normalizes this defensively (`normalizeTrackPoints` / `normalizeMediaPoints`) and falls back to `defaultRoutePoints` (Seoul→Busan) if absent.

### Timeline (the core abstraction)
`build_timeline_segments` converts track + media points into an ordered list of `TimelineSegment`s of three types:
- `map_move` — camera flies between two track indices. **Pacing is distance-based**: total move time = `sum(distance_km) * move_seconds_per_km` (default 0.05 → consistent speed regardless of route length), then split proportionally to Haversine distance (`allocate_move_durations`, per-segment floor). **60s cap**: if `move_total + holds + photos > max_video_seconds` (default 60), only the moves are compressed (sped up) to fit; holds/photos are untouched. `stops_and_photos_seconds()` computes the non-move time used for the cap. `map_seconds` is now only the fallback for the no-media single move + the legacy `render_map_frames` path.
- `map_hold` — camera sits at a stop point (`stop_seconds` if it has photos, else `arrival_hold_seconds`).
- `photo` — a photo fade-in / hold / fade-out, composited in Python over the last held map frame.

`render_timeline_frames` (the active render path) walks the segments, calling the matching browser function per frame and capturing the result. **Note:** `render_map_frames` + the older single-`renderFrame(progress)` continuous-camera path still exist and are described in `README.md`'s "카메라 구간" section, but `run()` calls `render_timeline_frames`, not `render_map_frames`. Don't assume the README's progress-based camera bands are what executes.

### Browser entry points (called from Python via page.evaluate)
- `window.initializeMap()` — creates the Mapbox map, adds terrain/atmosphere/buildings/route layers, waits for idle.
- `window.renderRouteSegment(startIndex, endIndex, progress, waitMode)` — one `map_move` frame.
- `window.renderStopPoint(trackIndex, name, waitMode)` — one `map_hold` frame; its captured PNG is reused as the base for photo compositing.
- `window.renderFrame(progress, waitMode)` — the legacy continuous-camera entry (still used by `render_map_frames`).
- `window.isRenderReady()`, `window.getWebGLInfo()`, `window.warmUpRouteTiles()` — readiness / diagnostics / optional tile warmup.

Every frame uses `map.jumpTo()` (not animated `flyTo`) so rendering is deterministic and decoupled from wall-clock time.

### Bearing smoothing
Camera heading is the subtle part. `smoothBearingForFrame` applies: shortest-angle interpolation, an exponential smoothing factor, a max turn-rate cap (`BEARING_MAX_DEGREES_PER_SECOND` / fps), a look-ahead point (`BEARING_LOOK_AHEAD_POINTS`), and a circular-mean over recent targets (`BEARING_TARGET_HISTORY_SIZE`). State (`smoothedBearing`, `lastProgress`, `targetBearingHistory`) resets on backward or large progress jumps. With `window.DEBUG_BEARING` on, `[bearing] …` lines are logged and surfaced in Python output; debug frames at progress 0.45/0.50/0.55 are saved to `debug/bearing_*`. Use `--bearing-test-route` to exercise sharp turns.

### Capture & encoding
`FfmpegPipeWriter` runs FFmpeg in a subprocess with a bounded queue and a dedicated writer thread; frames are pushed as encoded bytes, so frame capture and encoding overlap. Capture goes through CDP `Page.captureScreenshot` (`cdp-png` default, `cdp-jpeg` for `--fast`) or Playwright's `page.screenshot` (`playwright-png`). `--render-wait-mode` controls how long to wait after `jumpTo` before capturing (`map-render` waits for a real render event; `raf`/`none` are faster but riskier). The first frame is checked for blankness and retried with `fromSurface=true` if needed.

### Performance
The dominant cost is reading the WebGL surface out of Chromium (`ReadPixels`), which is brutal under software rendering. Always check the startup `[webgl] renderer=...` log — if it shows SwiftShader / llvmpipe / "software" / "basic render", you're on CPU and it will be slow. `PerfStats` prints per-stage and per-frame timing (`[perf]` / `[perf:frame]` / `[benchmark]`).

## Conventions & gotchas

- **Directory layout is fixed** by module-level constants in `render_video.py` (`ROOT`, `ASSETS_DIR`, `FRAMES_DIR`, `OUTPUT_DIR`, `TEMP_DIR`, `DEBUG_DIR`). `frames/`, `output/`, `temp/`, `debug/` are working dirs; cleanup functions guard against deleting paths outside their own directory.
- **Fonts are Windows paths** (`C:/Windows/Fonts/malgun.ttf`, …) in `find_font()`. Korean labels in the auto-generated destination photo depend on `malgun`; on a non-Windows host it falls back to ASCII text and Pillow's default font.
- If `assets/destination_photo.jpg` is missing, `ensure_destination_photo()` generates a 1080x1920 placeholder.
- Config is split between two frozen `RenderConfig` presets (`DEFAULT_CONFIG`, `QUICK_CONFIG`); `--fast` mutates the parsed args, not the config. `build_config` layers CLI overrides on top of the chosen preset.
- The single render `.py` is large and dataclass-driven — match the existing functional style (pure helpers, explicit dataclasses, no globals beyond the path constants) when extending it.
- Mapbox attribution is intentionally **not** hidden (usage policy).
