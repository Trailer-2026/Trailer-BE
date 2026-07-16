"""Modal T4 GPU 위에서 헤드리스 Chromium이 실제 GPU를 쓰는지 검증. 

로컬(이 레포)에서 실행:
    modal run modal_gpu_probe.py

Modal이 아래 image를 클라우드에서 빌드하고, T4 GPU가 붙은 컨테이너에서
probe()를 돌린 뒤 결과를 로컬 터미널로 스트리밍한다.

해석:
  - renderer 에 'NVIDIA' / 'Tesla T4' 가 보이는 백엔드 → ✅ GPU 사용 (이걸 채택)
  - 전부 'SwiftShader' / 'llvmpipe' → ❌ 소프트웨어. 진단 로그(아래 ICD/EGL 경로)로 원인 추적.
"""

import modal

# Chromium(WebGL) + GPU 유저스페이스 라이브러리를 갖춘 이미지.
# Modal이 호스트의 NVIDIA 드라이버를 컨테이너에 주입하지만,
# Vulkan/EGL '로더'와 ICD 등록은 우리가 깔아줘야 ANGLE이 GPU를 찾는다.
image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("playwright==1.49.0")
    .run_commands(
        # 헤드리스 크롬 + OS 의존 라이브러리
        "playwright install --with-deps chromium",
        # GPU 백엔드용 로더들: Vulkan 로더, EGL/GL(glvnd), 진단 도구
        "apt-get update && apt-get install -y --no-install-recommends "
        "libvulkan1 vulkan-tools libglvnd0 libgl1 libegl1 libgles2 pciutils",
        # NVIDIA Vulkan ICD 등록. 그래픽 lib(libGLX_nvidia.so.0)은 런타임에 주입되고,
        # json 은 '이름'만 참조하므로 빌드 시점에 만들어도 된다.
        "mkdir -p /usr/share/vulkan/icd.d",
        "printf '%s' '{\"file_format_version\":\"1.0.0\",\"ICD\":"
        "{\"library_path\":\"libGLX_nvidia.so.0\",\"api_version\":\"1.3.242\"}}' "
        "> /usr/share/vulkan/icd.d/nvidia_icd.json",
        # NVIDIA EGL vendor 등록 (glvnd). 50_mesa(소프트웨어)보다 먼저 시도되도록 10_.
        "printf '%s' '{\"file_format_version\":\"1.0.0\",\"ICD\":"
        "{\"library_path\":\"libEGL_nvidia.so.0\"}}' "
        "> /usr/share/glvnd/egl_vendor.d/10_nvidia.json",
    )
    # 핵심: NVIDIA 컨테이너 런타임은 capabilities 에 'graphics' 가 있어야
    # Vulkan ICD / EGL vendor 라이브러리를 주입한다. 기본값(compute,utility)이면
    # CUDA(nvidia-smi)만 되고 OpenGL/Vulkan 은 SwiftShader 로 떨어진다.
    # VK_ICD_FILENAMES / __EGL_VENDOR_LIBRARY_FILENAMES 로 로더가 NVIDIA 만 쓰도록 강제.
    .env({
        "NVIDIA_DRIVER_CAPABILITIES": "all",
        "NVIDIA_VISIBLE_DEVICES": "all",
        "VK_ICD_FILENAMES": "/usr/share/vulkan/icd.d/nvidia_icd.json",
        "__EGL_VENDOR_LIBRARY_FILENAMES": "/usr/share/glvnd/egl_vendor.d/10_nvidia.json",
        "LD_LIBRARY_PATH": "/usr/lib/x86_64-linux-gnu",
    })
)

app = modal.App("trailer-gpu-probe", image=image)

PROBE_HTML = (
    "data:text/html,"
    "<canvas id=c></canvas><script>"
    "const gl=document.getElementById('c').getContext('webgl')"
    "||document.getElementById('c').getContext('experimental-webgl');"
    "let r='NO_WEBGL_CONTEXT';"
    "if(gl){const e=gl.getExtension('WEBGL_debug_renderer_info');"
    "r=e?gl.getParameter(e.UNMASKED_RENDERER_WEBGL):gl.getParameter(gl.RENDERER);}"
    "document.title=r;</script>"
)

COMMON = ["--ignore-gpu-blocklist", "--enable-gpu-rasterization", "--no-sandbox"]

BACKENDS = {
    "vulkan":      ["--use-angle=vulkan", "--enable-features=Vulkan"],
    "gl-egl":      ["--use-angle=gl-egl"],
    "gl":          ["--use-angle=gl"],
    "default":     [],
    "swiftshader": ["--use-angle=swiftshader"],  # 비교용 기준선
}

SOFTWARE_TERMS = ("swiftshader", "llvmpipe", "software", "basic")


@app.function(gpu="T4")
def probe():
    import subprocess
    from playwright.sync_api import sync_playwright

    def sh(cmd):
        try:
            return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()
        except Exception as e:  # noqa: BLE001
            return f"(err: {e})"

    # NVIDIA 그래픽 lib 은 컨테이너 시작 시점에 주입되므로, 빌드 때 만들어진
    # 링커 캐시(ld.so.cache)에는 없다. 한 번 갱신해야 로더가 libGLX/libEGL_nvidia 를 찾는다.
    sh("ldconfig")

    print("=" * 78)
    print("환경 진단")
    print("=" * 78)
    print("[nvidia-smi]\n" + sh("nvidia-smi || echo 'nvidia-smi 없음'"))
    print("[lspci GPU]\n" + sh("lspci -nn | grep -iE 'nvidia|vga|3d|display' || echo '(GPU 장치 없음)'"))
    print("[CAPS env]\n" + sh("echo NVIDIA_DRIVER_CAPABILITIES=$NVIDIA_DRIVER_CAPABILITIES"))
    print("[vulkan ICD]\n" + sh("ls -1 /usr/share/vulkan/icd.d/ 2>/dev/null || echo '(없음)'"))
    print("[EGL vendor]\n" + sh("ls -1 /usr/share/glvnd/egl_vendor.d/ 2>/dev/null || echo '(없음)'"))
    print("[nvidia GL/EGL/Vulkan libs]\n" + sh(
        "find / -xdev \\( -name 'libGLX_nvidia.so*' -o -name 'libEGL_nvidia.so*' "
        "-o -name 'libGLESv2_nvidia.so*' -o -name 'nvidia_icd*.json' "
        "-o -name 'libnvidia-glcore.so*' \\) 2>/dev/null | head -20 || echo '(nvidia 그래픽 lib 없음)'"))
    print("[vulkaninfo]\n" + sh("vulkaninfo --summary 2>/dev/null | grep -iE 'deviceName|driverName' || echo '(vulkaninfo 실패)'"))

    print("\n" + "=" * 78)
    print("Chromium WebGL renderer (ANGLE 백엔드별)")
    print("=" * 78)
    print(f"{'backend':<12} {'mark':<12} renderer")
    print("-" * 78)

    best = None
    for name, extra in BACKENDS.items():
        args = ["--headless=new", *COMMON, *extra]
        try:
            with sync_playwright() as p:
                b = p.chromium.launch(headless=True, args=args)
                page = b.new_page()
                page.goto(PROBE_HTML)
                page.wait_for_timeout(600)
                renderer = page.title() or "(empty)"
                b.close()
        except Exception as e:  # noqa: BLE001
            renderer = f"ERROR: {e}"

        is_sw = any(t in renderer.lower() for t in SOFTWARE_TERMS)
        bad = "ERROR" in renderer or "NO_WEBGL" in renderer
        mark = "❌ software" if is_sw else ("⚠️ fail" if bad else "✅ GPU")
        print(f"{name:<12} {mark:<12} {renderer}")
        if best is None and not is_sw and not bad and name != "swiftshader":
            best = name

    print("-" * 78)
    if best:
        flag = BACKENDS[best][0]
        print(f"\n✅ GPU로 잡힌 백엔드: '{best}'  → build_chromium_args 리눅스 분기에 '{flag}' 사용")
    else:
        print("\n❌ 모든 백엔드가 software/실패. 위 진단 로그(Vulkan ICD / EGL vendor)로 원인 추적 필요.")
    return best


@app.local_entrypoint()
def main():
    result = probe.remote()
    print(f"\n[로컬] probe 결과: {result}")
