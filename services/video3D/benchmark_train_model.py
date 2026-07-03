# 3D 기차 모델(train_color.glb) 적용 여부에 따른 렌더 시간 비교 벤치마크.
#
# 같은 travel data(기본: assets/travel_bench6.json, GPS 6개)로 두 번 렌더한다:
#   1) with-model    — 현재 상태 그대로 (GLB 3D 기차)
#   2) without-model — assets/*.glb 를 잠시 치워 아이콘 폴백으로 렌더
# GLB 파일은 temp/glb_backup/ 으로 옮겼다가 끝나면 반드시 되돌린다.
#
# 실행 (trailer3d env):
#   conda run -n trailer3d python benchmark_train_model.py             # full quality
#   conda run -n trailer3d python benchmark_train_model.py --quick     # 빠른 비교
#   conda run -n trailer3d python benchmark_train_model.py --fast
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
ASSETS = ROOT / "assets"
BACKUP = ROOT / "temp" / "glb_backup"
GLB_NAMES = ["train_color.glb", "train_fixed.glb", "train.glb"]
TRAVEL_DATA = "assets/travel_bench6.json"

PASS_ARGS = [a for a in sys.argv[1:] if a in ("--quick", "--fast")]


def run_render(label: str) -> dict:
    cmd = [sys.executable, "render_video.py", "--travel-data", TRAVEL_DATA, *PASS_ARGS]
    print(f"\n=== {label}: {' '.join(cmd)}")
    started = time.perf_counter()
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    elapsed = time.perf_counter() - started
    out = proc.stdout or ""
    if proc.returncode != 0:
        print(out[-2000:])
        print((proc.stderr or "")[-1000:])
        raise SystemExit(f"{label} 렌더 실패 (exit {proc.returncode})")

    result = {"label": label, "wall_seconds": elapsed}
    # 기차 렌더 방식 / perf 라인 추출
    for line in out.splitlines():
        if "[train]" in line or "train icon" in line:
            result.setdefault("train_lines", []).append(line.strip())
        m = re.search(r"\[perf\] map_frame_total avg=([\d.]+)ms.*max=([\d.]+)ms", line)
        if m:
            result["frame_avg_ms"] = float(m.group(1))
        m = re.search(r"\[perf\] renderFrame_call avg=([\d.]+)ms", line)
        if m:
            result["render_call_avg_ms"] = float(m.group(1))
        if "총 프레임" in line or "total_frames" in line:
            result.setdefault("info", []).append(line.strip())
    print(f"    총 소요: {elapsed:.1f}s")
    for k in ("frame_avg_ms", "render_call_avg_ms"):
        if k in result:
            print(f"    {k}: {result[k]:.1f}ms")
    for line in result.get("train_lines", []):
        print(f"    {line}")
    return result


def main():
    BACKUP.mkdir(parents=True, exist_ok=True)
    with_model = run_render("with-model (3D 기차)")

    moved = []
    try:
        for name in GLB_NAMES:
            src = ASSETS / name
            if src.is_file():
                shutil.move(str(src), str(BACKUP / name))
                moved.append(name)
        without_model = run_render("without-model (아이콘 폴백)")
    finally:
        for name in moved:
            shutil.move(str(BACKUP / name), str(ASSETS / name))
        print(f"\nGLB 복원 완료: {moved}")

    dw = with_model["wall_seconds"]
    dn = without_model["wall_seconds"]
    print("\n===== 결과 =====")
    print(f"3D 기차 적용   : {dw:7.1f}s")
    print(f"아이콘 폴백    : {dn:7.1f}s")
    print(f"차이           : {dw - dn:+7.1f}s ({(dw / dn - 1) * 100:+.1f}%)")
    fa, fb = with_model.get("frame_avg_ms"), without_model.get("frame_avg_ms")
    if fa and fb:
        print(f"프레임 평균    : {fa:.1f}ms vs {fb:.1f}ms ({(fa / fb - 1) * 100:+.1f}%)")


if __name__ == "__main__":
    main()
