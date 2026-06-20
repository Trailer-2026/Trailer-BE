# 3D 여행 경로 영상 생성 테스트

Mapbox GL JS의 WebGL 3D 지도를 Playwright Chromium으로 프레임 단위 캡처하고, FFmpeg로 세로형 H.264 MP4를 만드는 테스트 프로젝트입니다. GPS 경로는 서울역에서 부산역까지이며, 대한민국 전체 보기에서 시작해 서울 줌인, 경로 비행, 부산 도착, 목적지 사진 fade 장면으로 이어집니다.

## Mapbox 토큰

Mapbox public access token을 발급한 뒤 프로젝트 루트에 `.env`를 만듭니다.

```text
MAPBOX_ACCESS_TOKEN=pk.your_public_token_here
```

`.env`는 저장소에 포함하지 않습니다. 토큰이 없으면 실행이 중단되고 안내 메시지가 출력됩니다.

## 환경 준비

```bash
conda env list
conda create -n trailer3d python=3.11 -y
conda run -n trailer3d python --version
conda run -n trailer3d python -m pip install -r requirements.txt
conda run -n trailer3d playwright install chromium
ffmpeg -version
```

설치와 실행 검증은 반드시 `trailer3d` 환경에서 진행합니다.

## 실행

기본 렌더는 1080x1920, 30 FPS, 약 13.67초 영상입니다.

```bash
conda run -n trailer3d python render_video.py
```

빠른 테스트는 540x960, 15 FPS입니다.

```bash
conda run -n trailer3d python render_video.py --quick
```

고속 모드는 해상도와 FPS를 유지하면서 CDP JPEG 캡처, rAF 렌더 대기, 워밍업 비활성화, 작은 큐를 사용합니다.

```bash
conda run -n trailer3d python render_video.py --fast
```

출력 파일은 기존 파일을 덮어쓰지 않고 `output/travel_3d_1.mp4`, `output/travel_3d_2.mp4`처럼 자동 증가합니다.

## 성능 옵션

30프레임만 렌더해 전체 시간을 추정할 수 있습니다.

```bash
conda run -n trailer3d python render_video.py --benchmark-frames 30
```

캡처 방식:

```text
--capture-mode playwright-png
--capture-mode cdp-png
--capture-mode cdp-jpeg
--jpeg-quality 95
--capture-from-surface true
```

렌더 대기 방식:

```text
--render-wait-mode map-render
--render-wait-mode raf
--render-wait-mode none
```

워밍업은 기본 비활성화입니다. 필요할 때만 짧게 실행합니다.

```bash
conda run -n trailer3d python render_video.py --warmup --warmup-samples 5 --warmup-timeout-ms 500
```

디버깅용 PNG/JPEG 시퀀스를 저장하려면 다음 옵션을 사용합니다.

```bash
conda run -n trailer3d python render_video.py --save-frames
```

## 프로젝트 구조

```text
현재 폴더/
├─ render_video.py
├─ map.html
├─ map_animation.js
├─ requirements.txt
├─ .env.example
├─ README.md
├─ assets/
│  └─ destination_photo.jpg
├─ frames/
├─ output/
├─ temp/
└─ debug/
```

`assets/destination_photo.jpg`가 없으면 Pillow가 1080x1920 테스트 이미지를 자동 생성합니다.

## 카메라 구간

- `0.00~0.10`: 대한민국 전체와 전체 경로 표시
- `0.10~0.20`: 서울역 줌인과 출발 지점 강조
- `0.20~0.80`: 서울에서 부산까지 Haversine 거리 비율 기반 이동
- `0.80~0.92`: 부산역 근처 확대
- `0.92~1.00`: 도착 지점 정지, pulse와 목적지 label 표시

`renderFrame(progress)`는 progress 기준으로 위치, 지나온 경로, zoom, pitch, bearing을 직접 계산합니다. 매 프레임 `jumpTo()`를 사용하고, bearing은 최단 각도 보간, 최대 회전 속도 제한, look-ahead, 원형 평균을 적용합니다.

## 3D 지도

`map_animation.js`의 `MAP_STYLE` 상수로 지도 스타일을 바꿀 수 있습니다.

```javascript
const MAP_STYLE = "mapbox://styles/mapbox/standard";
```

3D terrain은 Mapbox raster-dem source와 `setTerrain()`으로 적용합니다. 3D 건물은 가능한 스타일에서만 추가하며, 실패해도 경고만 출력하고 지형, 경로, 카메라 렌더링은 계속합니다. fog/atmosphere 효과도 지원되면 적용합니다.

## 렌더링이 느린 이유

가장 큰 비용은 Mapbox WebGL 화면을 Chromium에서 읽어와 PNG/JPEG로 캡처하는 단계입니다. 특히 GPU가 없거나 SwiftShader 소프트웨어 렌더러가 사용되면 `ReadPixels` 병목이 커집니다. 실행 로그의 `[webgl] renderer=...`, `devicePixelRatio`, `canvas=...`를 확인해 실제 렌더러와 해상도를 점검하세요.

빠른 확인은 `--quick` 또는 `--benchmark-frames 30`을 사용합니다. 최종 품질을 유지하면서 시간을 줄이려면 먼저 `--fast`를 테스트하세요.

## Mapbox 사용 정책과 attribution

Mapbox public token은 브라우저 렌더링에 노출될 수 있는 값입니다. 사용량, 과금, attribution 요구사항은 Mapbox 정책을 따릅니다. 이 프로젝트는 Mapbox attribution 컨트롤을 숨기지 않습니다.

## WebGL 또는 Chromium 문제 해결

- `conda run -n trailer3d playwright install chromium`을 다시 실행합니다.
- `--disable-gpu` 같은 옵션을 추가하지 않습니다.
- Windows 로컬은 GPU 사용을 우선하고, GPU가 없는 서버는 SwiftShader fallback을 허용합니다.
- `[webgl] renderer`에 `SwiftShader`, `Software Renderer`, `llvmpipe`, `Microsoft Basic Render Driver`가 나오면 CPU 렌더링이므로 느릴 수 있습니다.
- Mapbox 콘솔 warning은 Python 로그에 출력됩니다. 3D 건물 레이어가 없는 스타일은 경고 후 계속 진행합니다.

## trackPoints / mediaPoints input

`--travel-data assets/travel_data.json` can load a full GPS track and separate media stops.

```json
{
  "trackPoints": [
    {
      "latitude": 37.5547,
      "longitude": 126.9706,
      "timestamp": "2026-06-01T09:00:00"
    }
  ],
  "mediaPoints": [
    {
      "trackIndex": 0,
      "name": "서울역",
      "photos": []
    }
  ]
}
```

- `trackPoints` is the complete GPS path. Normal GPS log points do not create markers.
- `mediaPoints` is the list of named places or photo stops. `trackIndex` points into `trackPoints`.
- A media point without photos is passed without stopping.
- A media point with photos renders map hold, label, ordered photo fade in/hold/fade out, and returns to the same map frame.
- Missing photo files print a warning and only that photo is skipped.

High quality render:

```bash
conda run -n trailer3d python render_video.py ^
  --travel-data assets/travel_data.json ^
  --render-wait-mode map-render ^
  --capture-mode cdp-png
```

Photo timing:

```text
--stop-seconds 0.8
--photo-fade-in-seconds 0.4
--photo-hold-seconds 1.6
--photo-fade-out-seconds 0.4
```
