"""Modal T4 GPU 에서 실제 3D 여행 영상(mp4)을 렌더링하고 로컬로 받아오는 워커.

검증된 T4 GPU 컨테이너 레시피를 그대로 적용해, 기존
render_video.py 를 컨테이너 안에서 실행한다. 렌더 시간과 산출물 크기를 출력하고,
생성된 mp4 를 로컬 output/ 으로 다운로드한다.

사전 준비:
  - services/videoMaker/.env 에 MAPBOX_ACCESS_TOKEN=pk... 가 있어야 한다 (로컬 .env 를
    Modal Secret 으로 그대로 주입하므로 별도 secret 생성 불필요).

실행 (로컬, trailer3d 환경):
    modal run modal_render.py                 # 최고 품질(무손실 PNG)
    modal run modal_render.py --mode quality-fast   # JPEG q95, 더 빠름/저렴

결과: services/videoMaker/output/modal_travel_3d_N.mp4 로 저장되고,
      렌더 시간/크기/renderer 가 터미널에 출력된다.
"""

from pathlib import Path

import modal

HERE = Path(__file__).parent

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
    # 렌더러 소스 + 자산을 /app 에 복사 (작업 디렉터리/산출물은 제외)
    .add_local_dir(
        str(HERE),
        remote_path="/app",
        ignore=[
            "output/**", "frames/**", "temp/**", "debug/**",
            "*.mp4", "**/*.mp4", ".env", "__pycache__/**",
            "modal_*.py", "*.md",
            # 3D 기차 디자인 소스(~80MB)는 로컬 변환용 — 렌더는 train_color.glb 만 읽음
            "assets/Little train/**", "assets/lokomotiv/**",
            # 구버전 원본 GLB(11MB, 법선 깨짐) — train_fixed.glb 가 폴백으로 충분
            "assets/train.glb",
        ],
    )
)

app = modal.App("trailer-videomaker-render", image=image)


@app.function(
    gpu="T4",
    timeout=3600,
    # 로컬 services/videoMaker/.env 를 그대로 컨테이너 환경변수로 주입 (MAPBOX_ACCESS_TOKEN)
    secrets=[modal.Secret.from_dotenv(HERE)],
)
def render(
    mode: str = "quality",
    travel_data: str = "",
    theme: str = "",
    light_preset: str = "",
    intro: bool = False,
    outro: bool = False,
):
    import os
    import subprocess
    import sys
    import time

    # NVIDIA 그래픽 lib 은 컨테이너 시작 시 주입되므로 링커 캐시 갱신
    subprocess.run("ldconfig", shell=True)
    os.chdir("/app")

    cmd = [sys.executable, "render_video.py"]
    if mode == "quality-fast":
        cmd.append("--quality-fast")
    # mode == "quality": 플래그 없음 = 최고 품질(무손실 cdp-png, map-render 대기)
    # travel_data 가 주어지면 빌더가 만든 동적 경로/사진/BGM(json 의 bgm 필드) 사용.
    if travel_data:
        cmd += ["--travel-data", travel_data]
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
        f"=== 렌더 시작 (mode={mode}, travel_data={travel_data or 'default'}, "
        f"theme={theme or 'default'}, light={light_preset or 'auto'}, "
        f"intro={'yes' if intro else 'no'}, outro={'yes' if outro else 'no'}) ==="
    )
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.time() - t0

    # render_video.py 의 로그(렌더러/perf/ffprobe)를 그대로 흘려보냄
    print(proc.stdout)
    if proc.returncode != 0:
        print("=== STDERR ===")
        print(proc.stderr)
        raise RuntimeError(f"render_video.py 실패 (exit {proc.returncode})")

    # 가장 최근 생성된 mp4 회수
    outputs = sorted(
        Path("/app/output").glob("travel_3d_*.mp4"),
        key=lambda p: p.stat().st_mtime,
    )
    if not outputs:
        raise RuntimeError("mp4 산출물을 찾지 못했습니다.")
    mp4 = outputs[-1]
    data = mp4.read_bytes()
    print(f"=== 렌더 완료: {mp4.name}, {elapsed:.1f}s, {len(data)/1e6:.1f} MB ===")
    return {
        "filename": mp4.name,
        "bytes": data,
        "elapsed": elapsed,
        "size": len(data),
    }


@app.local_entrypoint()
def main(
    mode: str = "quality",
    travel_data: str = "",
    theme: str = "",
    light_preset: str = "",
    intro: bool = False,
    outro: bool = False,
):
    from datetime import datetime

    result = render.remote(mode, travel_data, theme, light_preset, intro, outro)

    out_dir = HERE / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    # 매 실행마다 고유 파일명(모드+타임스탬프)으로 저장해 이전 결과를 덮지 않는다.
    # (컨테이너 내부 output ID 는 매번 1 로 리셋되므로 로컬에서 고유화한다.)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"travel_3d_{mode}_{stamp}.mp4"
    out_path.write_bytes(result["bytes"])

    print("\n" + "=" * 60)
    print(f"✅ 생성 완료 (mode={mode})")
    print(f"📁 저장 위치: {out_path}")
    print(f"⏱  렌더 시간: {result['elapsed']:.1f}초")
    print(f"📦 파일 크기: {result['size'] / 1e6:.1f} MB")
    print("=" * 60)
