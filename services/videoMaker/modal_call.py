# -*- coding: utf-8 -*-
"""배포된 Modal 렌더 함수를 병렬로 호출하는 코디네이터 (서버 → 서브프로세스용).

video_service 가 engine=modal 렌더 시 이 스크립트를 실행한다:
    python modal_call.py --travel-data assets/uploads/<job>/travel_data.json \
        [--mode quality-fast] [--theme ...] [--light-preset ...] [--intro] [--outro]

동작:
  1. travel_data 를 정차 지점(사진 지점) 경계로 최대 MAX_CHUNKS 조각으로 나누고
     (경계 지점의 정차·사진은 다음 조각 시작에 배치 — 양쪽 다 같은 클로즈업
      프레임이라 이어붙여도 티가 안 난다),
  2. 배포된 render 함수(trailer-videomaker-render)를 조각별 컨테이너에서 동시에
     실행한다. 각 조각의 [perf:frame] 진행을 합산해
     "[perf:frame] <합계>/<전체>" 형태로 재출력 → 서버 진행률 파싱이 그대로 동작.
  3. 조각 mp4 들을 stream-copy concat 으로 합치고, BGM 먹싱·인트로/아웃트로를
     로컬에서 1회 처리한 뒤 (마커: "BGM 합성 완료" 등) output/ 에 저장하고
     "저장 위치: <경로>" 를 출력한다 (서버가 이 마커로 파일명을 파싱).

비고:
  - 조각 렌더는 BGM/인트로/아웃트로 없이 돌린다 (합친 뒤 한 번만 처리해야 함).
  - 병렬 수는 min(MAX_CHUNKS, 내부 정차 지점 수 + 1). 정차가 없으면 1조각.
  - 사전 준비: `modal deploy modal_render.py` 1회.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import modal

HERE = Path(__file__).resolve().parent
APP_NAME = "trailer-videomaker-render"
MAX_CHUNKS = 5


def load_travel_data_json(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def collect_job_files(base_dir: Path, travel_data_json: str) -> dict[str, bytes]:
    """travel_data 가 참조하는 업로드 사진들을 {상대경로: bytes} 로 모은다.

    bgm 은 배포 이미지에 이미 구워져 있으므로 보내지 않는다.
    """
    data = json.loads(travel_data_json)
    files: dict[str, bytes] = {}
    for media_point in data.get("mediaPoints", []):
        for rel in media_point.get("photos", []):
            source = (base_dir / rel).resolve()
            if source.is_file():
                files[Path(rel).as_posix()] = source.read_bytes()
            else:
                print(f"[warn] 사진 파일 없음(건너뜀): {rel}")
    return files


# --------------------------------------------------------------------------- #
# travel_data 분할
# --------------------------------------------------------------------------- #
def _haversine_km(a: dict, b: dict) -> float:
    lat1, lon1 = float(a["latitude"]), float(a["longitude"])
    lat2, lon2 = float(b["latitude"]), float(b["longitude"])
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    h = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
    )
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def split_travel_data(data: dict, max_chunks: int = MAX_CHUNKS) -> list[dict]:
    """travel_data 를 정차 지점 경계로 거리 균등에 가깝게 나눈다.

    경계 정차 지점의 정차+사진은 다음 조각의 시작(로컬 index 0)에 들어가고,
    이전 조각은 같은 지점에 사진 없는 도착 정차만 남긴다 — 양쪽 경계 프레임이
    같은 클로즈업이라 concat 이 자연스럽다. 반환: 조각 travel_data 목록 (bgm 제외).
    """
    track_points = data["trackPoints"]
    media_points = sorted(data.get("mediaPoints", []), key=lambda m: m["trackIndex"])
    last_index = len(track_points) - 1

    # 경계 후보: 경로 중간의 정차 지점
    candidates = [m["trackIndex"] for m in media_points if 0 < m["trackIndex"] < last_index]
    chunk_count = min(max_chunks, len(candidates) + 1)
    if chunk_count < 2:
        return [dict(data, mediaPoints=media_points)]

    # 누적 거리 기준으로 목표 지점(j/chunk_count)에 가장 가까운 후보를 경계로 선택
    cumulative = [0.0]
    for i in range(last_index):
        cumulative.append(cumulative[-1] + _haversine_km(track_points[i], track_points[i + 1]))
    total_km = cumulative[-1] or 1.0

    boundaries: list[int] = []
    remaining = list(candidates)
    for j in range(1, chunk_count):
        target = total_km * j / chunk_count
        usable = [c for c in remaining if not boundaries or c > boundaries[-1]]
        if not usable:
            break
        best = min(usable, key=lambda c: abs(cumulative[c] - target))
        boundaries.append(best)
        remaining.remove(best)
    if not boundaries:
        return [dict(data, mediaPoints=media_points)]

    edges = [0, *boundaries, last_index]
    chunks: list[dict] = []
    for chunk_index in range(len(edges) - 1):
        start, end = edges[chunk_index], edges[chunk_index + 1]
        chunk_track = track_points[start : end + 1]
        chunk_media: list[dict] = []
        for media in media_points:
            index = media["trackIndex"]
            if not start <= index <= end:
                continue
            local = index - start
            if index == start and chunk_index > 0:
                # 경계 지점의 정차·사진은 이 조각의 시작에 (직전 조각과 같은 프레임)
                chunk_media.append({**media, "trackIndex": local})
            elif index == end and end != last_index:
                # 경계 지점 도착 — 사진 없이 클로즈업 정차만 (사진은 다음 조각에서)
                chunk_media.append({"trackIndex": local, "name": media.get("name", ""), "photos": []})
            elif index != start or chunk_index == 0:
                chunk_media.append({**media, "trackIndex": local})
        chunks.append({"trackPoints": chunk_track, "mediaPoints": chunk_media})
    return chunks


# --------------------------------------------------------------------------- #
# 병렬 실행 + 진행률 합산
# --------------------------------------------------------------------------- #
class ChunkState:
    def __init__(self, count: int):
        self.lock = threading.Lock()
        self.frames = [0] * count
        self.totals = [0] * count
        self.results: list[dict | None] = [None] * count
        self.errors: list[str | None] = [None] * count


def _run_chunk(index: int, chunk: dict, files: dict[str, bytes], args, state: ChunkState) -> None:
    try:
        render = modal.Function.from_name(APP_NAME, "render")
        for event in render.remote_gen(
            travel_data_json=json.dumps(chunk, ensure_ascii=False),
            files=files,
            mode=args.mode,
            theme=args.theme,
            light_preset=args.light_preset,
            intro=False,
            outro=False,
        ):
            if not isinstance(event, dict):
                continue
            if "log" in event:
                match = re.search(r"\[perf:frame\]\s*(\d+)/(\d+)", event["log"])
                if match:
                    with state.lock:
                        state.frames[index] = int(match.group(1))
                        state.totals[index] = int(match.group(2))
            elif "result" in event:
                with state.lock:
                    state.results[index] = event["result"]
                    # 완료 조각은 프레임을 총량으로 확정
                    if state.totals[index]:
                        state.frames[index] = state.totals[index]
    except Exception as error:  # noqa: BLE001 — 조각 실패는 전체 실패로 보고
        with state.lock:
            state.errors[index] = str(error)


def render_chunks_parallel(chunks: list[dict], all_files: dict[str, bytes], args) -> list[dict]:
    """조각들을 동시에 렌더하고 결과 목록을 반환한다. 진행률을 합산 출력한다."""
    state = ChunkState(len(chunks))
    threads: list[threading.Thread] = []
    for i, chunk in enumerate(chunks):
        needed = {
            rel: all_files[rel]
            for media in chunk["mediaPoints"]
            for rel in media.get("photos", [])
            if rel in all_files
        }
        thread = threading.Thread(target=_run_chunk, args=(i, chunk, needed, args, state), daemon=True)
        thread.start()
        threads.append(thread)

    last_line = ""
    while any(t.is_alive() for t in threads):
        time.sleep(2)
        with state.lock:
            if any(state.errors):
                break
            # 모든 조각의 전체 프레임 수가 파악된 뒤부터 합산 출력 (진행률 역행 방지)
            if all(state.totals):
                line = f"[perf:frame] {sum(state.frames):06d}/{sum(state.totals):06d} (parallel x{len(chunks)})"
                if line != last_line:
                    print(line, flush=True)
                    last_line = line
    for t in threads:
        t.join(timeout=5)

    errors = [e for e in state.errors if e]
    if errors:
        raise RuntimeError(f"조각 렌더 실패 ({len(errors)}건):\n" + "\n".join(errors[:2]))
    results = [r for r in state.results if r]
    if len(results) != len(chunks):
        raise RuntimeError("일부 조각의 렌더 결과를 받지 못했습니다.")
    print(f"[perf:frame] {sum(state.totals):06d}/{sum(state.totals):06d} (parallel x{len(chunks)})", flush=True)
    return list(state.results)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 합치기 + 후처리 (concat → BGM → 인트로/아웃트로)
# --------------------------------------------------------------------------- #
def concat_and_postprocess(
    results: list[dict], bgm_rel: str | None, args, work_dir: Path
) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg 를 찾을 수 없습니다 (PATH 확인).")
    work_dir.mkdir(parents=True, exist_ok=True)

    chunk_paths: list[Path] = []
    for i, result in enumerate(results):
        path = work_dir / f"chunk_{i}.mp4"
        path.write_bytes(result["bytes"])
        chunk_paths.append(path)

    merged = work_dir / "merged.mp4"
    if len(chunk_paths) == 1:
        chunk_paths[0].replace(merged)
    else:
        # 조각들은 동일 파이프라인(libx264 동일 파라미터)이라 stream-copy concat 가능
        list_path = work_dir / "concat.txt"
        list_path.write_text(
            "".join(f"file '{p.as_posix()}'\n" for p in chunk_paths), encoding="utf-8"
        )
        proc = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", str(list_path), "-c", "copy", str(merged)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"조각 concat 실패:\n{(proc.stderr or '')[-1500:]}")
    print("렌더링 완료 (조각 합치기 끝)", flush=True)

    # 후처리는 render_video/intro_video 의 함수를 그대로 재사용 (마커도 동일하게 출력)
    sys.path.insert(0, str(HERE))
    from intro_video import append_outro, prepend_intro  # noqa: PLC0415
    from render_video import QUALITY_FAST_CONFIG, mux_bgm_into_video  # noqa: PLC0415

    config = QUALITY_FAST_CONFIG
    if bgm_rel:
        bgm_path = (HERE / bgm_rel).resolve()
        if bgm_path.is_file():
            mux_bgm_into_video(ffmpeg, merged, bgm_path)
            print(f"BGM 합성 완료: {bgm_path.name}", flush=True)
        else:
            print(f"[warn] BGM 파일 없음(무음 유지): {bgm_rel}", flush=True)
    if args.intro:
        prepend_intro(
            ffmpeg=ffmpeg, video_path=merged,
            width=config.width, height=config.height, fps=config.fps,
            preset="veryfast", crf=18, temp_dir=work_dir,
        )
        print("인트로 합성 완료: TRAILER 텍스트 마스크 줌", flush=True)
    if args.outro:
        append_outro(
            ffmpeg=ffmpeg, video_path=merged,
            width=config.width, height=config.height, fps=config.fps,
            preset="veryfast", crf=18, temp_dir=work_dir,
        )
        print("아웃트로 합성 완료: TRAILER 역방향 줌", flush=True)
    return merged


def main() -> int:
    parser = argparse.ArgumentParser(description="배포된 Modal 렌더 함수 병렬 호출")
    parser.add_argument("--travel-data", required=True, help="travel_data.json 경로 (videoMaker 기준 상대)")
    parser.add_argument("--mode", default="quality-fast", choices=["quality-fast", "quality"])
    parser.add_argument("--theme", default="")
    parser.add_argument("--light-preset", default="")
    parser.add_argument("--intro", action="store_true")
    parser.add_argument("--outro", action="store_true")
    args = parser.parse_args()

    travel_path = (HERE / args.travel_data).resolve()
    if not travel_path.is_file():
        print(f"travel_data 를 찾을 수 없습니다: {travel_path}")
        return 1
    data = json.loads(load_travel_data_json(travel_path))
    bgm_rel = data.pop("bgm", None)  # BGM 은 합친 뒤 1회 먹싱
    all_files = collect_job_files(HERE, json.dumps(data, ensure_ascii=False))

    try:
        modal.Function.from_name(APP_NAME, "render").hydrate()
    except Exception as error:
        print(f"배포된 Modal 앱({APP_NAME})을 찾을 수 없습니다: {error}")
        print("먼저 배포하세요: modal deploy modal_render.py")
        return 1

    chunks = split_travel_data(data)
    print(f"[parallel] {len(chunks)}개 조각으로 분할 렌더링 시작", flush=True)

    results = render_chunks_parallel(chunks, all_files, args)

    work_dir = HERE / "temp" / f"parallel_{uuid.uuid4().hex[:8]}"
    try:
        merged = concat_and_postprocess(results, bgm_rel, args, work_dir)
        out_dir = HERE / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = out_dir / f"modal_travel_3d_{stamp}.mp4"
        merged.replace(out_path)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    total_render = max((r["elapsed"] for r in results), default=0.0)
    print(f"렌더 시간(최장 조각): {total_render:.1f}초, 크기: {out_path.stat().st_size / 1e6:.1f} MB")
    print(f"저장 위치: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
