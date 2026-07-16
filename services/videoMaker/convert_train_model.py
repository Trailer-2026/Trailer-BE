# "Little train" OBJ+MTL -> Mapbox 호환 컬러 GLB 변환 (v4)
#
# 파이프라인:
#   1. MTL 텍스처 참조명 보정(Bronze0_1.jpg -> Bronze0.jpg) 후 작업 폴더 구성
#   2. obj2gltf(npx) 로 OBJ -> GLB (trimesh 의 OBJ 로더는 다중 재질 OBJ 의
#      면 인덱스를 깨뜨려서 사용 불가; obj2gltf 결과는 정상 확인됨)
#   3. 변환 행렬 계산: OBB 주축 정렬(원본이 XZ 평면에서 45도 회전됨) ->
#      길이=X(기수 -X), 위=+Y, 길이 1.899(기존 train_fixed.glb 규격), 원점 센터링
#   4. 도색 스킴 (기본 classic) — 부위 판정은 최종 좌표계 기준:
#      classic     검은 보일러(앞) + 빨간 운전실/탱크(뒤) + 빨간 하부 + 금색 로드.
#                  영상 카메라가 위에서 내려다보므로 윗면이 넓은 뒷부분을
#                  빨갛게 해야 지도 위에서 색이 보인다.
#      --original  원본 텍스처 그대로 (위에서 보면 거의 전부 검정)
#      --palette   장난감 팔레트 (전부 밝은 단색)
#   5. --flip 을 주면 Y축 180도 회전 (기수 방향 반전; 기본은 기존 모델과 동일한 -X)
#
# 실행: conda run -n trailer3d python convert_train_model.py [--original|--palette] [--flip]
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import trimesh
from trimesh.visual.material import PBRMaterial

SRC_DIR = Path(r"C:\Users\Gamzadole\Desktop\Trailer-BE\services\videoMaker\assets\Little train")
WORK_DIR = Path(__file__).parent / "temp" / "littletrain_work"
RAW_GLB = WORK_DIR / "train_raw.glb"
OUT_PATH = Path(r"C:\Users\Gamzadole\Desktop\Trailer-BE\services\videoMaker\assets\train_color.glb")
TARGET_LENGTH = 1.899  # 기존 train_fixed.glb 와 동일
FLIP_NOSE = "--flip" in sys.argv
SCHEME = "palette" if "--palette" in sys.argv else ("original" if "--original" in sys.argv else "classic")

# --palette 용 장난감 기차 팔레트 (재질 패밀리 -> RGB)
FAMILY_COLORS = {
    "Metallic_Varni": (0.80, 0.16, 0.13),   # 차체: 선명한 빨강
    "Bronze": (0.85, 0.63, 0.22),           # 로드/트림: 골드
    "Chrome": (0.80, 0.83, 0.87),           # 배관: 실버
    "Pewter": (0.30, 0.33, 0.38),           # 휠/하부: 짙은 슬레이트
    "Slightly_smoked": (0.62, 0.78, 0.90),  # 창문: 밝은 하늘색
    "vlc75SIM": (0.58, 0.58, 0.60),         # 기타: 회색
}

# 1) 작업 폴더 (MTL 보정 + 텍스처 복사)
WORK_DIR.mkdir(parents=True, exist_ok=True)
if not (WORK_DIR / "Small train.obj").is_file():
    shutil.copy(SRC_DIR / "Small train.obj", WORK_DIR / "Small train.obj")
mtl_text = (SRC_DIR / "Small train.mtl").read_text(encoding="utf-8", errors="replace")
fixed = []
for line in mtl_text.splitlines():
    m = re.match(r"(\s*map_Kd\s+)(.+)", line)
    if m:
        ref = m.group(2).strip()
        real = ref if (SRC_DIR / ref).is_file() else re.sub(r"_\d+(\.jpg)$", r"\1", ref)
        if not (SRC_DIR / real).is_file():
            sys.exit(f"texture not found: {ref}")
        if not (WORK_DIR / real).is_file():
            shutil.copy(SRC_DIR / real, WORK_DIR / real)
        line = m.group(1) + real
    fixed.append(line)
(WORK_DIR / "Small train.mtl").write_text("\n".join(fixed), encoding="utf-8")

# 2) obj2gltf
if not RAW_GLB.is_file():
    subprocess.run(
        ["npx", "-y", "obj2gltf", "-i", "Small train.obj", "-o", RAW_GLB.name],
        cwd=WORK_DIR, check=True, shell=True,
    )
scene = trimesh.load(RAW_GLB)
print("geoms:", len(scene.geometry), "raw extents:", np.round(scene.extents, 2))

# 3) 변환 행렬 계산 (아직 적용하지 않고, 도색 판정에 최종 좌표를 쓰기 위해 먼저 계산)
merged = scene.to_geometry() if hasattr(scene, "to_geometry") else scene.dump(concatenate=True)
to_origin, obb_ext = trimesh.bounds.oriented_bounds(merged)
R = to_origin[:3, :3]
length_axis = int(np.argmax(obb_ext))
up_in_obb = R @ np.array([0.0, 1.0, 0.0])
cand = [i for i in range(3) if i != length_axis]
up_axis = cand[int(np.argmax([abs(up_in_obb[i]) for i in cand]))]
width_axis = ({0, 1, 2} - {length_axis, up_axis}).pop()
P = np.zeros((3, 3))
P[0, length_axis] = 1.0
P[1, up_axis] = np.sign(up_in_obb[up_axis])
P[2, width_axis] = 1.0
if np.linalg.det(P) < 0:
    P[2, width_axis] = -1.0
M = np.eye(4)
M[:3, :3] = P

aligned = merged.copy()
aligned.apply_transform(M @ to_origin)
scale = TARGET_LENGTH / aligned.extents[0]
S = np.eye(4) * scale
S[3, 3] = 1.0
aligned.apply_transform(S)
C = np.eye(4)
C[:3, 3] = -aligned.bounds.mean(axis=0)
T_FINAL = C @ S @ M @ to_origin  # raw 좌표 -> 최종 좌표

aligned.apply_transform(C)
lo, hi = aligned.bounds
print("final bounds:", np.round(aligned.bounds, 3))


def final_center(g):
    """지오메트리 bbox 중심의 최종 좌표 (obj2gltf 노드 변환은 identity)."""
    c = np.append(g.bounds.mean(axis=0), 1.0)
    return (T_FINAL @ c)[:3]


# 4) 재도색
def flat(g, family, rgb, rough=0.6):
    g.visual.material = PBRMaterial(
        name=family,
        baseColorFactor=[rgb[0], rgb[1], rgb[2], 1.0],
        metallicFactor=0.15,
        roughnessFactor=rough,
    )


if SCHEME == "palette":
    for name, g in scene.geometry.items():
        mat = getattr(g.visual, "material", None)
        family = re.sub(r"\d+$", "", str(getattr(mat, "name", None) or name))
        flat(g, family, FAMILY_COLORS.get(family, (0.58, 0.58, 0.60)))
    print("repainted: toy palette")
elif SCHEME == "classic":
    # 검은 보일러 + 빨간 운전실/탱크/하부 + 금색 로드 + 은색 배관.
    # 기수는 -X: X > BOILER_END 이면 뒷부분(운전실·석탄고·측면 탱크) = 빨강.
    # 높이 하위 32% (차대·바퀴) 도 빨강. 나머지(보일러·굴뚝) = 검정.
    CLASSIC = {
        "Bronze": (0.85, 0.63, 0.22),           # 커플링 로드: 골드
        "Chrome": (0.78, 0.81, 0.85),           # 배관: 실버
        "Slightly_smoked": (0.72, 0.83, 0.92),  # 창문: 밝은 하늘색
    }
    BODY_BLACK = (0.10, 0.10, 0.11)
    RED = (0.76, 0.11, 0.09)
    chassis_top = lo[1] + 0.32 * (hi[1] - lo[1])
    BOILER_END = 0.02  # 최종 좌표계 X: 이보다 뒤(+X)는 운전실/탱크 구역
    n_red = 0
    for name, g in scene.geometry.items():
        mat = getattr(g.visual, "material", None)
        family = re.sub(r"\d+$", "", str(getattr(mat, "name", None) or name))
        if family in CLASSIC:
            flat(g, family, CLASSIC[family])
            continue
        cx, cy, _ = final_center(g)
        if cy < chassis_top or cx > BOILER_END:
            flat(g, family, RED)
            n_red += 1
        else:
            flat(g, family, BODY_BLACK, rough=0.5)
    print(f"repainted: classic (red parts: {n_red})")
else:
    print("kept original textures")

# 5) 변환 적용 (+ 필요시 기수 방향 뒤집기)
scene.apply_transform(T_FINAL)
if FLIP_NOSE:
    scene.apply_transform(trimesh.transformations.rotation_matrix(np.pi, [0, 1, 0]))
    print("nose flipped 180deg")
print("final extents:", np.round(scene.extents, 3))

# 6) 내보내기 (법선 포함; Mapbox 는 법선 없는 GLB 를 렌더링하지 못함)
for g in scene.geometry.values():
    _ = g.vertex_normals
glb = trimesh.exchange.gltf.export_glb(scene, include_normals=True)
OUT_PATH.write_bytes(glb)
print(f"saved: {OUT_PATH} ({len(glb)/1e6:.1f} MB)")
chk = trimesh.load(OUT_PATH)
print("reloaded extents:", np.round(chk.extents, 3), "geoms:", len(chk.geometry))
