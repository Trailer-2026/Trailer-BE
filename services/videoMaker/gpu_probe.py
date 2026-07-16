"""헤드리스 Chromium이 실제 GPU를 쓰는지 검증하는 독립 실행 스크립트.

리눅스 GPU 서버(SSH)에서 실행해, ANGLE 백엔드별로 WebGL renderer가
실제 GPU(NVIDIA …)로 잡히는지 SwiftShader(소프트웨어)로 떨어지는지 확인한다.

사용:
    pip install playwright
    playwright install chromium
    playwright install-deps        # 시스템 라이브러리 (sudo 필요할 수 있음)
    python gpu_probe.py

출력의 renderer가 'NVIDIA …' 면 ✅ GPU 사용, 'SwiftShader'/'llvmpipe' 면 ❌ 소프트웨어.
"""

import sys
from playwright.sync_api import sync_playwright

# WebGL UNMASKED_RENDERER_WEBGL 을 읽어 화면에 박는 최소 페이지
PROBE_HTML = """data:text/html,
<canvas id=c></canvas><script>
  const gl = document.getElementById('c').getContext('webgl')
          || document.getElementById('c').getContext('experimental-webgl');
  let r = 'NO_WEBGL_CONTEXT';
  if (gl) {
    const ext = gl.getExtension('WEBGL_debug_renderer_info');
    r = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL)
            : gl.getParameter(gl.RENDERER);
  }
  document.title = r;
</script>
"""

COMMON_GPU_ARGS = [
    "--ignore-gpu-blocklist",
    "--enable-gpu-rasterization",
]

# 리눅스에서 시도해볼 ANGLE 백엔드 후보들 (위에서부터 우선)
BACKENDS = {
    "gl":         ["--use-angle=gl"],
    "egl":        ["--use-angle=gl-egl"],
    "vulkan":     ["--use-angle=vulkan"],
    "default":    [],                       # 크롬이 알아서 고르게
    "swiftshader":["--use-angle=swiftshader"],  # 소프트웨어 기준선(비교용)
}

SOFTWARE_TERMS = ("swiftshader", "llvmpipe", "software", "basic")


def probe(backend_name: str, extra_args: list[str]) -> str:
    args = ["--headless=new", *COMMON_GPU_ARGS, *extra_args]
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=args)
            page = browser.new_page()
            page.goto(PROBE_HTML)
            page.wait_for_timeout(500)
            renderer = page.title()
            browser.close()
            return renderer or "(empty)"
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"


def main() -> int:
    print(f"platform={sys.platform}\n")
    print(f"{'backend':<12} {'flag':<26} renderer")
    print("-" * 78)
    best = None
    for name, extra in BACKENDS.items():
        renderer = probe(name, extra)
        flag = extra[0] if extra else "(none)"
        is_sw = any(t in renderer.lower() for t in SOFTWARE_TERMS)
        mark = "❌ software" if is_sw else ("⚠️" if "ERROR" in renderer or "NO_WEBGL" in renderer else "✅ GPU")
        print(f"{name:<12} {flag:<26} {renderer}   {mark}")
        if best is None and not is_sw and "ERROR" not in renderer and "NO_WEBGL" not in renderer and name != "swiftshader":
            best = name
    print("-" * 78)
    if best:
        print(f"\n✅ 추천 백엔드: --use-angle={BACKENDS[best][0].split('=')[1] if BACKENDS[best] else 'default'}  (이 값을 build_chromium_args 리눅스 분기에 넣으면 됨)")
    else:
        print("\n❌ GPU 백엔드를 못 찾음. nvidia-smi / 드라이버 / playwright install-deps 를 확인하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
