"""Modal T4 GPU 렌더 워커 (배포형).

`modal deploy modal_render.py` 로 한 번 배포해 두면, 이미지(playwright/ffmpeg)와
정적 자산(기차 GLB·bgm·지도 소스)이 미리 구워져서 렌더 호출마다 다시 올리지
않는다. 서버(video_service)는 modal_call.py 를 통해 배포된 render 함수를 원격
호출하며, 매 호출에는 travel_data JSON 과 업로드 사진 바이트만 전달된다.
→ `modal run` 방식 대비 호출당 준비 시간이 수 분 → 컨테이너 부팅 수십 초로 준다.

사전 준비:
  - config/properties_dev.ini 의 [mapbox] access_token 에 Mapbox 토큰
    (배포 시 Modal Secret 으로 구워진다 — 토큰 변경 시 재배포 필요).
  - 배포/재배포 (trailer3d 환경, 이 디렉터리에서):
        modal deploy modal_render.py

수동 테스트 (배포 없이 1회 실행):
    modal run modal_render.py --travel-data assets/travel_data.json
"""

import os
import sys
from pathlib import Path

import modal

HERE = Path(__file__).parent

APP_NAME = "trailer-videomaker-render"
# 렌더 호출 기본 모드 — quality-fast(JPEG q95, raf 대기)가 기본.
# 무손실 PNG 가 꼭 필요할 때만 mode="quality" 로 호출한다.
DEFAULT_MODE = "quality-fast"


def _mapbox_token() -> str:
    """백엔드 공통 설정([mapbox] access_token)에서 토큰을 읽는다.

    렌더러 나머지(render_video.load_token)와 동일한 1순위 소스. 설정이 없으면
    환경변수 MAPBOX_ACCESS_TOKEN 폴백.
    """
    backend_root = HERE.resolve().parent.parent
    if (backend_root / "config" / "__init__.py").exists():
        if str(backend_root) not in sys.path:
            sys.path.insert(0, str(backend_root))
        try:
            from config import Config

            token = Config.read("mapbox", "access_token")
            if token and token.strip():
                return token.strip()
        except Exception:
            pass
    return os.environ.get("MAPBOX_ACCESS_TOKEN", "").strip()

# --- 검증된 GPU 컨테이너 이미지 -------------------------------------------------
image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("playwright==1.49.0", "python-dotenv", "Pillow")
    .run_commands(
        # 헤드리스 크롬 + OS 의존 라이브러리
        "playwright install --with-deps chromium",
        # GPU 백엔드 로더 + ffmpeg(libx264) + 한글 CJK 폰트
        "apt-get update && apt-get install -y --no-install-recommends "
        "libvulkan1 libglvnd0 libgl1 libegl1 libgles2 "
        "ffmpeg fonts-noto-cjk fonts-nanum",
        # NVIDIA Vulkan ICD 등록 (그래픽 lib 은 런타임 주입; json 은 이름만 참조)
        "mkdir -p /usr/share/vulkan/icd.d",
        "printf '%s' '{\"file_format_version\":\"1.0.0\",\"ICD\":"
        "{\"library_path\":\"libGLX_nvidia.so.0\",\"api_version\":\"1.3.242\"}}' "
        "> /usr/share/vulkan/icd.d/nvidia_icd.json",
        # NVIDIA EGL vendor 등록 (glvnd)
        "printf '%s' '{\"file_format_version\":\"1.0.0\",\"ICD\":"
        "{\"library_path\":\"libEGL_nvidia.so.0\"}}' "
        "> /usr/share/glvnd/egl_vendor.d/10_nvidia.json",
    )
    .env({
        "NVIDIA_DRIVER_CAPABILITIES": "all",
        "NVIDIA_VISIBLE_DEVICES": "all",
        "VK_ICD_FILENAMES": "/usr/share/vulkan/icd.d/nvidia_icd.json",
        "__EGL_VENDOR_LIBRARY_FILENAMES": "/usr/share/glvnd/egl_vendor.d/10_nvidia.json",
        "LD_LIBRARY_PATH": "/usr/lib/x86_64-linux-gnu",
    })
    # 렌더러 소스 + 정적 자산을 /app 에 복사 — 배포 시 1회.
    # 업로드 사진(assets/uploads)은 렌더 호출 때 바이트로 받는다.
    .add_local_dir(
        str(HERE),
        remote_path="/app",
        ignore=[
            "output/**", "frames/**", "temp/**", "debug/**",
            "*.mp4", "**/*.mp4", ".env", "__pycache__/**",
            "modal_*.py", "*.md",
            "assets/uploads/**",
            # 3D 기차 디자인 소스(~80MB)는 로컬 변환용 — 렌더는 train_color.glb 만 읽음
            "assets/Little train/**", "assets/lokomotiv/**",
            # 구버전 원본 GLB(11MB, 법선 깨짐) — train_fixed.glb 가 폴백으로 충분
            "assets/train.glb",
        ],
    )
)

app = modal.App(APP_NAME, image=image)


@app.function(
    gpu="T4",
    timeout=3600,
    # 공통 설정의 Mapbox 토큰을 컨테이너 환경변수로 주입 (MAPBOX_ACCESS_TOKEN)
    secrets=[modal.Secret.from_dict({"MAPBOX_ACCESS_TOKEN": _mapbox_token()})],
)
def render(
    travel_data_json: str = "",
    files: dict | None = None,
    mode: str = DEFAULT_MODE,
    theme: str = "",
    light_preset: str = "",
    intro: bool = False,
    outro: bool = False,
):
    """travel_data JSON + 사진 바이트({상대경로: bytes})를 받아 렌더링한다.

    제너레이터: 진행 로그를 {"log": line} 으로 실시간 yield 하고 (호출측이
    진행률 파싱에 사용), 마지막에 {"result": {filename, bytes, ...}} 를 yield 한다.
    Modal 의 로그 스트리밍은 파이프 환경에서 실시간성이 보장되지 않아
    제너레이터로 직접 흘려보낸다.

    files 의 경로는 /app 기준 상대경로(예: assets/uploads/<job>/photo_0.jpg)로,
    travel_data 의 photos 항목과 일치해야 한다.
    """
    import os
    import subprocess
    import sys
    import time
    from pathlib import Path as ContainerPath

    # NVIDIA 그래픽 lib 은 컨테이너 시작 시 주입되므로 링커 캐시 갱신
    subprocess.run("ldconfig", shell=True)
    os.chdir("/app")
    app_root = ContainerPath("/app").resolve()

    # 호출자가 보낸 파일(업로드 사진 등)을 /app 아래에 기록 (경로 탈출 방지)
    for rel_path, content in (files or {}).items():
        target = (app_root / rel_path).resolve()
        if not str(target).startswith(str(app_root)):
            raise ValueError(f"허용되지 않는 파일 경로: {rel_path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    cmd = [sys.executable, "render_video.py"]
    if mode == "quality-fast":
        cmd.append("--quality-fast")
    # mode == "quality": 플래그 없음 = 최고 품질(무손실 cdp-png, map-render 대기)
    if travel_data_json:
        travel_path = app_root / "travel_data_job.json"
        travel_path.write_text(travel_data_json, encoding="utf-8")
        cmd += ["--travel-data", str(travel_path)]
    # 지도 테마 (spring/summer/autumn/winter). 빈 값이면 기본 스타일.
    if theme and theme != "default":
        cmd += ["--theme", theme]
    # 시간대 조명 (dawn/day/dusk/night). 빈 값이면 테마 기본.
    if light_preset:
        cmd += ["--light-preset", light_preset]
    # TRAILER 텍스트 마스크 줌 인트로/아웃트로 (렌더 후처리).
    if intro:
        cmd.append("--intro")
    if outro:
        cmd.append("--outro")

    print(
        f"=== 렌더 시작 (mode={mode}, files={len(files or {})}, "
        f"theme={theme or 'default'}, light={light_preset or 'auto'}, "
        f"intro={'yes' if intro else 'no'}, outro={'yes' if outro else 'no'}) ==="
    )
    t0 = time.time()
    # 렌더 로그([perf:frame] 등)를 실시간으로 yield — capture 후 일괄 출력하면
    # 호출측(서버) 진행률 표시가 완료 순간까지 0% 로 멈춰 보인다.
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    # 진행률 파싱에 쓰이는 라인만 yield (전 라인 yield 는 네트워크 왕복 낭비).
    progress_markers = (
        "[perf:frame]", "렌더링 완료", "합성 완료", "출력 예정 파일", "[timeline]", "[webgl]",
    )
    for line in proc.stdout:
        line = line.rstrip("\n")
        print(line, flush=True)  # Modal 로그(대시보드)용
        if any(marker in line for marker in progress_markers):
            yield {"log": line}
    returncode = proc.wait()
    elapsed = time.time() - t0

    if returncode != 0:
        raise RuntimeError(f"render_video.py 실패 (exit {returncode})")

    # 가장 최근 생성된 mp4 회수
    outputs = sorted(
        ContainerPath("/app/output").glob("travel_3d_*.mp4"),
        key=lambda p: p.stat().st_mtime,
    )
    if not outputs:
        raise RuntimeError("mp4 산출물을 찾지 못했습니다.")
    mp4 = outputs[-1]
    data = mp4.read_bytes()
    print(f"=== 렌더 완료: {mp4.name}, {elapsed:.1f}s, {len(data)/1e6:.1f} MB ===")
    yield {
        "result": {
            "filename": mp4.name,
            "bytes": data,
            "elapsed": elapsed,
            "size": len(data),
        }
    }


@app.local_entrypoint()
def main(
    mode: str = DEFAULT_MODE,
    travel_data: str = "",
    theme: str = "",
    light_preset: str = "",
    intro: bool = False,
    outro: bool = False,
):
    """수동 테스트용 (modal run). 서버 경유는 modal_call.py 를 쓴다."""
    from datetime import datetime

    from modal_call import collect_job_files, load_travel_data_json

    travel_data_json, files = "", {}
    if travel_data:
        travel_data_json = load_travel_data_json(HERE / travel_data)
        files = collect_job_files(HERE, travel_data_json)

    result = None
    for event in render.remote_gen(
        travel_data_json=travel_data_json,
        files=files,
        mode=mode,
        theme=theme,
        light_preset=light_preset,
        intro=intro,
        outro=outro,
    ):
        if isinstance(event, dict) and "log" in event:
            print(event["log"], flush=True)
        elif isinstance(event, dict) and "result" in event:
            result = event["result"]
    if result is None:
        raise RuntimeError("렌더 결과를 받지 못했습니다.")

    out_dir = HERE / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"travel_3d_{mode}_{stamp}.mp4"
    out_path.write_bytes(result["bytes"])

    print("\n" + "=" * 60)
    print(f"✅ 생성 완료 (mode={mode})")
    print(f"저장 위치: {out_path}")
    print(f"⏱  렌더 시간: {result['elapsed']:.1f}초")
    print(f"📦 파일 크기: {result['size'] / 1e6:.1f} MB")
    print("=" * 60)
