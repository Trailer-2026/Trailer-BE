# -*- coding: utf-8 -*-
"""TRAILER 텍스트 마스크 줌 인트로/아웃트로.

렌더가 끝난 본편 MP4 앞뒤에 타이틀 클립을 붙인다 (in place):
- `prepend_intro`: 검정→TRAILER→'I' 획 속으로 줌인→검정→본편 첫 프레임
- `append_outro`: 인트로의 정반대 — 본편 마지막 프레임→검정→'I' 획에서
  줌아웃→TRAILER→검정 (배경 영상은 정방향 유지)
브라우저/GPU 없이 Pillow + FFmpeg 만 사용하므로 본편 렌더 대비 비용이
사실상 고정(클립당 수 초)이다.

구성:
1. 검정 화면에서 "TRAILER" 페이드인 — 글자 안쪽에만 본편 영상
   (BG_START_SECONDS 지점부터)이 비친다 (마스크 합성)
2. 글자를 매 프레임 다시 그리며(스케일 아님 → 끝까지 선명) 가운데 글자
   'I'의 획 속으로 줌인 → 검정 페이드아웃
3. 검정 → 본편 첫 프레임 페이드인까지 인트로에 굽는다. 인트로 마지막
   프레임 == 본편 첫 프레임이라 하드컷이 보이지 않는다.
4. 본편과 동일한 코덱 파라미터로 인코딩 후 concat demuxer + stream copy
   → 본편 재인코딩 없음. 추가 시간은 영상 길이와 무관하게 ~5초.

본편에 오디오(BGM)가 있으면 같은 스펙의 무음 AAC 트랙을 인트로에 넣어
stream copy concat 이 유지된다.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

INTRO_TEXT = "TRAILER"

# 타이밍 (초). 인트로 총 길이 = HOLD + ZOOM + FADE_TO_MAIN.
HOLD_SECONDS = 0.6          # 처음 글자 크기 유지 (타이틀 읽는 시간)
ZOOM_SECONDS = 2.4          # 줌인 구간
FADE_IN_SECONDS = 0.3       # 시작 시 검정에서 글자 페이드인
FADE_OUT_SECONDS = 0.45     # 줌 끝에서 검정으로 페이드아웃 (줌과 겹침)
FADE_TO_MAIN_SECONDS = 0.6  # 검정 → 본편 첫 프레임 페이드인
BG_START_SECONDS = 6.0      # 글자 안에 비출 본편 구간 시작 지점 (가능하면)

# 두꺼운 올캡스 폰트 후보 (위에서부터). 획 폭 = 글자 창으로 보이는 영상
# 면적이므로 웨이트가 무거울수록 효과가 산다. Linux(Modal 컨테이너)에는
# Impact 가 없어 폴백 사용 — 이미지에 폰트를 추가하면 그 폰트가 잡힌다.
FONT_CANDIDATES = [
    Path("C:/Windows/Fonts/impact.ttf"),
    Path("C:/Windows/Fonts/ariblk.ttf"),
    Path("C:/Windows/Fonts/seguibl.ttf"),
    Path("C:/Windows/Fonts/arialbd.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
    Path("/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf"),
]


def find_intro_font() -> Path | None:
    for candidate in FONT_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def ease_in_out(t: float) -> float:
    return t * t * (3.0 - 2.0 * t)


def probe_video(
    video_path: Path,
) -> tuple[float | None, tuple[int, int] | None, int | None]:
    """(길이 초, 오디오 (sample_rate, channels) 또는 None, 비디오 타임스케일).

    ffprobe 없으면 전부 None. 타임스케일은 본편 비디오 트랙의 time_base 분모 —
    인트로를 같은 값으로 인코딩해야 stream copy concat 에서 본편 타임스탬프가
    늘어지지 않는다 (다르면 재생 속도가 틀어진다).
    """
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None, None, None
    duration: float | None = None
    audio_spec: tuple[int, int] | None = None
    timescale: int | None = None
    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(video_path),
            ],
            capture_output=True, text=True, check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            duration = float(result.stdout.strip())
    except (OSError, ValueError):
        pass
    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=time_base",
                "-of", "default=noprint_wrappers=1:nokey=1", str(video_path),
            ],
            capture_output=True, text=True, check=False,
        )
        text = result.stdout.strip()
        if result.returncode == 0 and "/" in text:
            timescale = int(text.split("/")[1])
    except (OSError, ValueError, IndexError):
        pass
    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=sample_rate,channels",
                "-of", "csv=p=0", str(video_path),
            ],
            capture_output=True, text=True, check=False,
        )
        line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        if result.returncode == 0 and line:
            rate_text, channels_text = line.split(",")[:2]
            audio_spec = (int(rate_text), int(channels_text))
    except (OSError, ValueError, IndexError):
        pass
    return duration, audio_spec, timescale


def measure_font_sizes(font_path: Path, width: int) -> tuple[float, float]:
    """시작/끝 폰트 크기.

    시작: 문구 전체 폭이 화면 폭의 78%. 끝: 문자열 중앙 글자('I', 속이 꽉 찬
    획)의 획 폭이 화면 폭을 여유 있게 덮어 줌 종착점에서 화면 전체가 배경이
    된다.
    """
    probe = ImageFont.truetype(str(font_path), 100)
    text_bbox = probe.getbbox(INTRO_TEXT)
    text_width = max(1, text_bbox[2] - text_bbox[0])
    middle_char = INTRO_TEXT[len(INTRO_TEXT) // 2]
    char_bbox = probe.getbbox(middle_char)
    char_stroke = max(1, char_bbox[2] - char_bbox[0])
    size_start = 100.0 * (width * 0.78) / text_width
    size_end = 100.0 * (width * 1.35) / char_stroke
    return size_start, size_end


def read_raw_frames(
    ffmpeg: str,
    video_path: Path,
    width: int,
    height: int,
    fps: int,
    start_seconds: float,
    max_frames: int,
) -> list[Image.Image]:
    cmd = [
        ffmpeg, "-v", "error",
        "-ss", f"{start_seconds:.3f}",
        "-t", f"{(max_frames + 2) / fps:.3f}",
        "-i", str(video_path),
        "-vf", f"scale={width}:{height},fps={fps}",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    frame_bytes = width * height * 3
    frames: list[Image.Image] = []
    assert proc.stdout is not None
    while len(frames) < max_frames:
        chunk = proc.stdout.read(frame_bytes)
        if not chunk or len(chunk) < frame_bytes:
            break
        frames.append(Image.frombytes("RGB", (width, height), chunk))
    proc.stdout.close()
    proc.wait()
    return frames


def build_zoom_frames(
    bg_frames: list[Image.Image],
    font_path: Path,
    width: int,
    height: int,
    fps: int,
    reverse: bool = False,
):
    """텍스트 마스크 줌 시퀀스: 페이드인 → hold → 'I' 획 속으로 줌인 → 페이드아웃.

    reverse=True 면 마스크 진행만 거꾸로(획 확대 상태에서 시작해 줌아웃 →
    hold → 페이드아웃, 아웃트로용). 배경(글자 안에 비치는 영상)은 어느 쪽이든
    항상 정방향으로 재생된다.
    """
    size_start, size_end = measure_font_sizes(font_path, width)
    zoom_frames = round((HOLD_SECONDS + ZOOM_SECONDS) * fps)
    hold_frames = round(HOLD_SECONDS * fps)
    fade_in_frames = max(1, round(FADE_IN_SECONDS * fps))
    fade_out_frames = max(1, round(FADE_OUT_SECONDS * fps))
    black = Image.new("RGB", (width, height), (0, 0, 0))

    for i in range(zoom_frames):
        # reverse 는 마스크 크기·페이드 타임라인만 뒤집는다 (j 기준).
        j = zoom_frames - 1 - i if reverse else i
        # 줌 진행도: hold 구간은 0, 이후 easeInOut. 크기는 지수 보간(등속 체감).
        zoom_t = max(0.0, (j - hold_frames) / max(1, zoom_frames - 1 - hold_frames))
        eased = ease_in_out(zoom_t)
        font_size = size_start * (size_end / size_start) ** eased

        font = ImageFont.truetype(str(font_path), round(font_size))
        mask = Image.new("L", (width, height), 0)
        ImageDraw.Draw(mask).text(
            (width / 2, height / 2), INTRO_TEXT, font=font, fill=255, anchor="mm"
        )
        bg = bg_frames[min(i, len(bg_frames) - 1)] if bg_frames else black
        frame = Image.composite(bg, black, mask)

        if j < fade_in_frames:
            frame = Image.blend(black, frame, (j + 1) / fade_in_frames)
        remaining = zoom_frames - 1 - j
        if remaining < fade_out_frames:
            frame = Image.blend(black, frame, remaining / fade_out_frames)

        yield frame


def build_intro_frames(
    bg_frames: list[Image.Image],
    main_first_frame: Image.Image,
    font_path: Path,
    width: int,
    height: int,
    fps: int,
):
    yield from build_zoom_frames(bg_frames, font_path, width, height, fps)

    # 검정 → 본편 첫 프레임. 마지막 프레임이 본편 첫 프레임과 동일해
    # stream copy 하드컷이 보이지 않는다.
    black = Image.new("RGB", (width, height), (0, 0, 0))
    fade_to_main_frames = max(1, round(FADE_TO_MAIN_SECONDS * fps))
    for i in range(fade_to_main_frames):
        yield Image.blend(black, main_first_frame, (i + 1) / fade_to_main_frames)


def build_outro_frames(
    bg_frames: list[Image.Image],
    main_last_frame: Image.Image,
    font_path: Path,
    width: int,
    height: int,
    fps: int,
):
    """아웃트로: 본편 마지막 프레임 → 검정 → 'I' 획에서 줌아웃 → TRAILER → 검정."""
    # 첫 프레임이 본편 마지막 프레임과 동일(알파 0)해 하드컷이 보이지 않는다.
    black = Image.new("RGB", (width, height), (0, 0, 0))
    bridge_frames = max(1, round(FADE_TO_MAIN_SECONDS * fps))
    for i in range(bridge_frames):
        yield Image.blend(main_last_frame, black, i / bridge_frames)

    yield from build_zoom_frames(bg_frames, font_path, width, height, fps, reverse=True)


def title_clip_total_frames(fps: int) -> int:
    """인트로/아웃트로 공통: 줌 시퀀스 + 본편 브리지(0.6초) 프레임 수."""
    return round((HOLD_SECONDS + ZOOM_SECONDS) * fps) + max(
        1, round(FADE_TO_MAIN_SECONDS * fps)
    )


def encode_clip(
    ffmpeg: str,
    clip_path: Path,
    frames,
    clip_seconds: float,
    width: int,
    height: int,
    fps: int,
    preset: str,
    crf: int,
    audio_spec: tuple[int, int] | None,
    timescale: int | None,
    label: str,
) -> None:
    """프레임 시퀀스를 본편과 동일 코덱 파라미터의 MP4 클립으로 인코딩한다."""
    cmd = [
        ffmpeg, "-y", "-v", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{width}x{height}", "-r", str(fps), "-i", "pipe:0",
    ]
    if audio_spec is not None:
        sample_rate, channels = audio_spec
        layout = "stereo" if channels >= 2 else "mono"
        cmd += [
            "-f", "lavfi", "-t", f"{clip_seconds:.3f}",
            "-i", f"anullsrc=r={sample_rate}:cl={layout}",
            "-map", "0:v", "-map", "1:a",
            "-c:a", "aac", "-b:a", "128k", "-ar", str(sample_rate), "-ac", str(channels),
            "-shortest",
        ]
    # stream copy concat 을 위해 본편(FfmpegPipeWriter/BGM mux 결과)과 코덱
    # 파라미터를 맞춘다. 타임스케일은 본편에서 probe 한 값을 그대로 사용 —
    # 다르면 concat 시 본편 재생 속도가 틀어진다. probe 실패 시 mp4 muxer
    # 기본값에 맡긴다.
    cmd += [
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-profile:v", "high",
        "-pix_fmt", "yuv420p",
    ]
    if timescale:
        cmd += ["-video_track_timescale", str(timescale)]
    cmd += [
        "-movflags", "+faststart",
        str(clip_path),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert proc.stdin is not None
    for frame in frames:
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError(f"{label} 클립 인코딩 실패")


def encode_intro_clip(
    ffmpeg: str,
    video_path: Path,
    intro_path: Path,
    width: int,
    height: int,
    fps: int,
    preset: str,
    crf: int,
    duration_seconds: float | None,
    audio_spec: tuple[int, int] | None,
    timescale: int | None,
    font_path: Path,
) -> None:
    zoom_seconds = HOLD_SECONDS + ZOOM_SECONDS

    # 글자 안에 비출 구간: 기본 6초 지점부터, 본편이 짧으면 앞으로 당긴다.
    bg_start = BG_START_SECONDS
    if duration_seconds is not None:
        bg_start = min(BG_START_SECONDS, max(0.0, duration_seconds - zoom_seconds - 0.5))
    bg_frames = read_raw_frames(
        ffmpeg, video_path, width, height, fps,
        start_seconds=bg_start, max_frames=round(zoom_seconds * fps),
    )
    first_frames = read_raw_frames(
        ffmpeg, video_path, width, height, fps, start_seconds=0.0, max_frames=1
    )
    if not first_frames:
        raise RuntimeError("본편 첫 프레임 추출 실패")

    encode_clip(
        ffmpeg, intro_path,
        build_intro_frames(bg_frames, first_frames[0], font_path, width, height, fps),
        title_clip_total_frames(fps) / fps,
        width, height, fps, preset, crf, audio_spec, timescale, "인트로",
    )


def encode_outro_clip(
    ffmpeg: str,
    video_path: Path,
    outro_path: Path,
    width: int,
    height: int,
    fps: int,
    preset: str,
    crf: int,
    duration_seconds: float | None,
    audio_spec: tuple[int, int] | None,
    timescale: int | None,
    font_path: Path,
) -> None:
    zoom_seconds = HOLD_SECONDS + ZOOM_SECONDS

    # 글자 안에 비출 구간: 본편 끝쪽. duration 을 모르면 처음부터.
    bg_start = 0.0
    if duration_seconds is not None:
        bg_start = max(0.0, duration_seconds - zoom_seconds - 0.6)
    bg_frames = read_raw_frames(
        ffmpeg, video_path, width, height, fps,
        start_seconds=bg_start, max_frames=round(zoom_seconds * fps),
    )
    # 본편 마지막 프레임: 끝 0.5초 구간을 읽어 마지막 것을 쓴다.
    tail_start = max(0.0, (duration_seconds or 0.5) - 0.5)
    tail_frames = read_raw_frames(
        ffmpeg, video_path, width, height, fps,
        start_seconds=tail_start, max_frames=round(0.5 * fps) + 2,
    )
    if not tail_frames:
        raise RuntimeError("본편 마지막 프레임 추출 실패")

    encode_clip(
        ffmpeg, outro_path,
        build_outro_frames(bg_frames, tail_frames[-1], font_path, width, height, fps),
        title_clip_total_frames(fps) / fps,
        width, height, fps, preset, crf, audio_spec, timescale, "아웃트로",
    )


def concat_replace(
    ffmpeg: str,
    part_paths: list[Path],
    video_path: Path,
    temp_dir: Path,
    label: str,
) -> None:
    """part_paths 순서대로 stream copy concat 해 video_path 를 교체한다."""
    list_path = temp_dir / f"{label}_concat.txt"
    joined_path = temp_dir / f"{label}_joined.mp4"
    list_path.write_text(
        "".join(f"file '{path.as_posix()}'\n" for path in part_paths),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            ffmpeg, "-y", "-v", "error",
            "-f", "concat", "-safe", "0", "-i", str(list_path),
            "-c", "copy", "-movflags", "+faststart",
            str(joined_path),
        ],
        capture_output=True, text=True, check=False,
    )
    list_path.unlink(missing_ok=True)
    if result.returncode != 0:
        joined_path.unlink(missing_ok=True)
        stderr = result.stderr[-3000:] if result.stderr else ""
        raise RuntimeError(f"{label} concat 실패:\n{stderr}")
    joined_path.replace(video_path)


def prepend_intro(
    ffmpeg: str,
    video_path: Path,
    width: int,
    height: int,
    fps: int,
    preset: str,
    crf: int,
    temp_dir: Path,
) -> None:
    """본편 MP4 앞에 TRAILER 인트로를 붙인다 (in place, 본편 재인코딩 없음)."""
    font_path = find_intro_font()
    if font_path is None:
        print("[intro] 사용할 폰트가 없어 인트로를 건너뜁니다.")
        return

    duration_seconds, audio_spec, timescale = probe_video(video_path)
    temp_dir.mkdir(parents=True, exist_ok=True)
    intro_path = temp_dir / "intro_clip.mp4"

    encode_intro_clip(
        ffmpeg, video_path, intro_path,
        width, height, fps, preset, crf,
        duration_seconds, audio_spec, timescale, font_path,
    )
    try:
        concat_replace(ffmpeg, [intro_path, video_path], video_path, temp_dir, "intro")
    finally:
        intro_path.unlink(missing_ok=True)


def append_outro(
    ffmpeg: str,
    video_path: Path,
    width: int,
    height: int,
    fps: int,
    preset: str,
    crf: int,
    temp_dir: Path,
) -> None:
    """본편 MP4 뒤에 TRAILER 아웃트로를 붙인다 (in place, 본편 재인코딩 없음)."""
    font_path = find_intro_font()
    if font_path is None:
        print("[outro] 사용할 폰트가 없어 아웃트로를 건너뜁니다.")
        return

    duration_seconds, audio_spec, timescale = probe_video(video_path)
    temp_dir.mkdir(parents=True, exist_ok=True)
    outro_path = temp_dir / "outro_clip.mp4"

    encode_outro_clip(
        ffmpeg, video_path, outro_path,
        width, height, fps, preset, crf,
        duration_seconds, audio_spec, timescale, font_path,
    )
    try:
        concat_replace(ffmpeg, [video_path, outro_path], video_path, temp_dir, "outro")
    finally:
        outro_path.unlink(missing_ok=True)
