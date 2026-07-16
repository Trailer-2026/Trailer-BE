# -*- coding: utf-8 -*-
"""배포된 Modal 렌더 함수를 호출하는 러너 (서버 → 서브프로세스용).

video_service 가 engine=modal 렌더 시 이 스크립트를 실행한다:
    python modal_call.py --travel-data assets/uploads/<job>/travel_data.json \
        [--mode quality-fast] [--theme ...] [--light-preset ...] [--intro] [--outro]

동작:
  1. travel_data.json 과 거기에 참조된 업로드 사진들을 읽어
  2. 배포된 함수(trailer-videomaker-render / render)를 원격 호출하고
     (제너레이터가 yield 하는 진행 로그를 stdout 에 그대로 찍어
      서버의 진행률 파서([perf:frame])가 그대로 동작한다)
  3. 결과 mp4 를 output/ 에 저장한 뒤 "저장 위치: <경로>" 를 출력한다
     (서버가 이 마커로 파일명을 파싱한다).

사전 준비: `modal deploy modal_render.py` 1회 (modal_render.py 참고).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import modal

HERE = Path(__file__).resolve().parent
APP_NAME = "trailer-videomaker-render"


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


def main() -> int:
    parser = argparse.ArgumentParser(description="배포된 Modal 렌더 함수 호출")
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
    travel_data_json = load_travel_data_json(travel_path)
    files = collect_job_files(HERE, travel_data_json)

    try:
        render = modal.Function.from_name(APP_NAME, "render")
    except Exception as error:
        print(f"배포된 Modal 앱({APP_NAME})을 찾을 수 없습니다: {error}")
        print("먼저 배포하세요: modal deploy modal_render.py")
        return 1

    # render 는 제너레이터 함수 — 진행 로그({"log": ...})를 실시간으로 받아
    # 그대로 stdout 에 찍으면 서버의 [perf:frame] 진행률 파싱이 동작한다.
    result = None
    for event in render.remote_gen(
        travel_data_json=travel_data_json,
        files=files,
        mode=args.mode,
        theme=args.theme,
        light_preset=args.light_preset,
        intro=args.intro,
        outro=args.outro,
    ):
        if isinstance(event, dict) and "log" in event:
            print(event["log"], flush=True)
        elif isinstance(event, dict) and "result" in event:
            result = event["result"]
    if result is None:
        print("렌더 결과를 받지 못했습니다.")
        return 1

    out_dir = HERE / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"modal_travel_3d_{stamp}.mp4"
    out_path.write_bytes(result["bytes"])

    print(f"렌더 시간: {result['elapsed']:.1f}초, 크기: {result['size'] / 1e6:.1f} MB")
    print(f"저장 위치: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
