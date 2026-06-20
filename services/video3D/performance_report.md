# 렌더링 성능 리포트 — "퀄리티 버전 5분 이내" 목표

## 결론 (TL;DR)

- **근본 원인은 GPU 미사용이었다.** 이 머신에는 **NVIDIA RTX 4060 Ti** 가 있지만,
  headless Chromium 이 `--use-angle=d3d11` 플래그가 없어 **SwiftShader(소프트웨어 렌더링)**
  으로 폴백하고 있었다. 소프트웨어 `ReadPixels`(스크린샷) 비용이 프레임당 2~5초로 병목.
- `--use-angle=d3d11` 한 줄로 headless 에서도 **실제 GPU(D3D11)** 를 사용하게 되었고,
  품질을 그대로 둔 채(1080x1920 / 30fps / 무손실 PNG 캡처) **풀 렌더가 106.8초** 로 떨어졌다.
- 즉 **해상도/FPS/품질을 낮추지 않고도 5분 목표를 큰 마진으로 달성**. JPEG 프리셋(`--quality-fast`)
  은 **41.6초**.
- 반대로 **SwiftShader 로는 5분 달성이 사실상 불가능**하다(720p/24fps 도 834초, 360x640 도 448초).
  GPU 가 계속 SwiftShader 로 떨어지면 해상도를 낮추는 게 아니라 **GPU 를 강제**해야 한다.

---

## 측정 환경

- GPU: NVIDIA GeForce RTX 4060 Ti (driver 32.0.15.9597), D3D11 / ANGLE
- 영상: 기본 Seoul→Busan 경로, `map_seconds=11`, 풀 프레임 수 = **426 프레임** (30fps) / 341 (24fps)
- 활성 렌더 경로: `render_timeline_frames` (legacy `render_map_frames` 아님)
- 측정 명령: `python render_video.py --benchmark-frames 30 ...` (첫 30프레임으로 풀 렌더 추정)
- `estimated_full_render` 는 **보수적 추정**(시작 프레임 포함 평균을 426프레임에 외삽)이며,
  실제 풀 렌더 시간은 더 낮게 나온다(아래 실측 참고).

### WebGL renderer 진단 (standalone probe)

| 런치 설정 | renderer |
|---|---|
| headless, 기존 기본 args (`--ignore-gpu-blocklist` 만) | **SwiftShader** (소프트웨어) |
| headless + `--use-angle=d3d11 --ignore-gpu-blocklist` | **NVIDIA RTX 4060 Ti, D3D11** |
| `--headless=new` + d3d11 | NVIDIA RTX 4060 Ti, D3D11 |
| headed + d3d11 | NVIDIA RTX 4060 Ti, D3D11 |

→ 결정적 플래그는 `--use-angle=d3d11`. headed 로 바꿀 필요 없이 headless 에서 GPU 사용 가능.

---

## 벤치마크 매트릭스 (30프레임, 순차 실행)

| # | gpu-mode | capture | wait | 해상도 | fps | renderer | frame_total_avg | capture(screenshot)_avg | estimated_full_render | output_size(30f) |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 (baseline) | swiftshader | cdp-png | map-render | 1080x1920 | 30 | SwiftShader | **5077.8ms** | 4931.7ms | **2171.6s** (~36분) | 4,753,013 |
| 2 (default 후) | auto/d3d11 | cdp-png | map-render | 1080x1920 | 30 | D3D11 | **353.6ms** | 253.5ms | **153.7s** | 5,026,845 |
| 3 | auto/d3d11 | cdp-jpeg q95 | map-render | 1080x1920 | 30 | D3D11 | 187.7ms | 91.8ms | 82.9s | 5,522,693 |
| 4 | auto/d3d11 | cdp-jpeg q95 | raf | 1080x1920 | 30 | D3D11 | 162.2ms | 100.5ms | 72.0s | 5,591,035 |
| 5 | auto/d3d11 | cdp-jpeg q95 | none | 1080x1920 | 30 | D3D11 | 144.6ms | 118.6ms | 64.6s | 5,486,857 |
| 6 | auto/d3d11 | cdp-jpeg q95 | raf | 1080x1920 | 24 | D3D11 | 170.6ms | 105.5ms | 61.1s | 6,994,346 |
| 7 | auto/d3d11 | cdp-jpeg q95 | raf | 720x1280 | 30 | D3D11 | 122.2ms | 72.2ms | 54.7s | 2,877,294 |

**모든 GPU(D3D11) 조합이 estimated_full_render < 300초.** 기본 무손실 PNG + 엄격한 map-render 대기
(2번)조차 153.7초로 여유롭게 통과.

### SwiftShader 폴백 시도 (모두 5분 초과 — 실패)

| # | gpu-mode | capture | wait | 해상도 | fps | frame_total_avg | capture_avg | estimated_full_render |
|---|---|---|---|---|---|---|---|---|
| 8 | swiftshader | cdp-jpeg q90 | none | 720x1280 | 24 | 2429.2ms | 2421.8ms | **834.2s** |
| 9 | swiftshader | cdp-jpeg q90 | none | 360x640 | 24 | 1305.4ms | 1298.9ms | **448.2s** |

소프트웨어 `ReadPixels` 에는 해상도와 무관한 프레임당 고정 비용(~1.3s @360p)이 있어,
JPEG/wait-mode/해상도를 아무리 낮춰도 14초짜리 클립을 5분 안에 못 만든다.
→ **SwiftShader 가 보이면 해상도를 낮추지 말고 GPU 를 켜라.**

---

## 풀 렌더 실측 (benchmark 아님, 실제 산출물)

| 명령 | renderer | 해상도/fps | wall-clock | perf total | 출력 | 검증 |
|---|---|---|---|---|---|---|
| `render_video.py` (기본, 무손실 PNG) | D3D11 | 1080x1920 / 30 | **106.8s** | 105.31s | travel_3d_40.mp4 | ✅ |
| `render_video.py --quality-fast` (JPEG q95) | D3D11 | 1080x1920 / 30 | **41.6s** | 40.05s | travel_3d_37.mp4 (36.0 MB) | ✅ |

두 산출물 모두 **300초 미만** 로그로 확인됨.

### ffprobe 검증 (travel_3d_37.mp4 / travel_3d_40.mp4)

- width x height = **1080 x 1920**
- fps = **30/1**
- frame_count = **426**
- duration = **14.20초**
- codec = **h264**, pixel_format = **yuv420p**

### 프레임 무결성 검증 (ffmpeg 추출 후 육안 확인)

- 시작 프레임(n=5): 서울 3D 지도 + 경로 마커 정상, Mapbox attribution 유지(정책 준수)
- 중간 프레임(n=200~210): Daegu→Busan 오렌지 경로선 + 카메라 틸트/베어링 정상
- 종료 프레임(n=420): 목적지 사진(한글 텍스트 포함) 합성 정상
- blank/corrupt 없음. cdp-jpeg q95 시각적 아티팩트 없음.
- output_size 비정상 축소 없음: 풀 렌더 36 MB(14.2s, 1080p, CRF18) 는 정상 범위.

---

## 권장 명령

### ① 최고 품질 5분 이내 (권장 · 실측 106.8s)

```bash
conda run -n trailer3d python render_video.py
```

- 1080x1920 / 30fps, **무손실 cdp-png 캡처**, 엄격한 `map-render` 대기, D3D11 GPU 자동 사용.
- 이제 기본값이 `--gpu-mode auto` 이므로 추가 플래그 없이 GPU 가속이 켜진다.
- 더 빠른 근사 무손실(JPEG q95, 41.6s)을 원하면:
  ```bash
  conda run -n trailer3d python render_video.py --quality-fast
  ```

### ② 폴백 — GPU 가 계속 SwiftShader 로 떨어질 때

```bash
conda run -n trailer3d python render_video.py --quality-fast --headed --gpu-mode d3d11
```

- headless old-mode 가 소프트웨어로 떨어지는 환경에서 `--headed` + D3D11 로 GPU 를 강제.
- GPU 가 정말 없는 호스트라면 5분 달성은 불가능(표 8~9 참조). 이때는:
  - **GPU 인스턴스/드라이버를 확보**하는 것이 유일한 해법.
  - 굳이 소프트웨어로 산출물만 뽑아야 한다면 해상도를 360x640 이하로 낮추고도 5분 초과를 감수해야 함.
- GPU 사용을 하드 강제(실패 시 렌더 에러로 즉시 드러남): `--disable-software-rasterizer` 추가.

---

## Before / After 요약

| 항목 | Before (SwiftShader, 기본) | After (기본, GPU 무손실 PNG) | After (`--quality-fast`, JPEG q95) |
|---|---|---|---|
| renderer | SwiftShader (CPU) | D3D11 NVIDIA RTX 4060 Ti | D3D11 NVIDIA RTX 4060 Ti |
| 해상도/FPS | 1080x1920 / 30 | 1080x1920 / 30 (유지) | 1080x1920 / 30 (유지) |
| frame_total_avg | 5077.8ms | 353.6ms | 162.2ms |
| estimated_full_render | 2171.6s | 153.7s | 72.0s |
| **실제 풀 렌더** | ~36분 (외삽) | **106.8s** | **41.6s** |
| 5분 목표 | ❌ | ✅ (마진 ~3분) | ✅ (마진 ~4분) |

> 사용자 제공 baseline(다른 실행 환경): frame_total_avg 2958.6ms / est 1271.1s(~21분) — 그 경우에도
> After 와 비교 시 동일 결론(SwiftShader→D3D11 전환이 핵심).

---

## 코드 변경 요약

- `build_chromium_args(gpu_mode, disable_software_rasterizer)` 추가 — 기존 `CHROMIUM_ARGS` 상수 대체.
  GPU 모드에서 `--use-angle=d3d11 --ignore-gpu-blocklist --enable-gpu-rasterization --enable-zero-copy` 부여.
- CLI: `--gpu-mode {auto,d3d11,default,swiftshader}` (기본 auto), `--headed`,
  `--disable-software-rasterizer`, `--quality-fast` 프리셋 추가.
- `--fast`/`--quality-fast` 가 사용자가 명시한 `--capture-mode/--render-wait-mode/--jpeg-quality/--queue-size`
  를 덮어쓰지 않도록 수정(기본값 None → 사용자 명시값 우선).
- WebGL renderer 로그 강화 + SwiftShader 감지 시 "5분 목표 위험" 경고 출력.
