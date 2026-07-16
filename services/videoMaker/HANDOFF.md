# HANDOFF — 3D 여행 경로 영상 렌더러를 백엔드로 이식

> 이 문서는 **이 코드를 백엔드 레포로 복사한 뒤, 그 폴더에서 새로 작업을 이어갈 AI/개발자**를 위한 인수인계 문서다.
> 원본은 CLI 단발 실행 도구였고, 목표는 이걸 **Python 백엔드(FastAPI/Django)의 서비스로 통합**하는 것이다.
> 현재 위치: `Trailer-BE/services/videoMaker/` — Trailer 백엔드의 한 서비스 모듈로 들어와 있다.

---

## 0. 한 줄 요약

GPS 여행 경로(JSON)를 입력받아 **Mapbox 3D 지도를 헤드리스 Chromium에서 프레임 단위로 렌더 → FFmpeg로 세로형(1080x1920) H.264 MP4**를 만드는 파이프라인. 지금은 `python render_video.py`로 도는 단일 스크립트다.

---

## 1. 옮겨온 파일이 각각 뭔지

| 파일 | 역할 |
|---|---|
| `render_video.py` | **오케스트레이션 전부.** 인자 파싱, 입력 검증, 타임라인 계획, Chromium 구동, 프레임 캡처, 사진 합성(Pillow), FFmpeg 파이프. 2000+ 줄 단일 파일. |
| `map_animation.js` | 브라우저(Chromium) 안에서 도는 코드. 경로 구성, 프레임별 카메라/bearing 계산, Mapbox 레이어 그리기. Python이 `page.evaluate(...)`로 호출한다. |
| `map.html` | `map_animation.js`를 로드하는 셸 페이지. Playwright가 이 파일을 연다. |
| `assets/travel_data.json` | 입력 데이터 예시 (`trackPoints` + `mediaPoints`). |
| `assets/destination_photo.jpg`, `assets/123.jpg` | 사진 합성/플레이스홀더용 이미지 자산. |
| `requirements.txt` | `playwright`, `python-dotenv`, `Pillow`. |
| `.env` | `MAPBOX_ACCESS_TOKEN=pk....` (public 토큰). **없으면 exit code 1.** 커밋 금지. |
| `CLAUDE.md` / `README.md` | 원본 프로젝트 문서. 아키텍처 디테일은 여기에 더 깊게 적혀 있음. |
| `frames/` `output/` `temp/` `debug/` | 작업 디렉터리(렌더 산출물). 비워도 되고, 없으면 런타임에 자동 생성됨. |

> ⚠️ **README/CLAUDE.md의 "카메라 구간"(progress 0.0~1.0 밴드) 설명은 레거시 경로다.** 실제로 `run()`이 호출하는 건 `render_timeline_frames`(세그먼트 기반)이지 `render_map_frames`(progress 기반)가 아니다. 둘을 헷갈리지 말 것.

---

## 2. 어떻게 도는가 (데이터 흐름)

```
travel_data.json
   │  load_raw_travel_data → validate_travel_data (관대한 검증: 잘못된 항목은 warn 후 skip)
   ▼
trackPoints + mediaPoints
   │  build_timeline_segments → TimelineSegment 리스트
   ▼
[ map_move | map_hold | photo ] 세그먼트 시퀀스
   │  render_timeline_frames 가 세그먼트를 순회하며
   │  매 프레임 page.evaluate 로 브라우저 함수 호출 + 화면 캡처
   ▼
PNG/JPEG 프레임 바이트
   │  FfmpegPipeWriter (별도 스레드 + bounded queue) 가 FFmpeg stdin 으로 스트리밍
   ▼
output/travel_3d_N.mp4   (덮어쓰지 않고 자동 증가)
```

### 핵심 개념: 타임라인 세그먼트 3종
- `map_move` — 두 trackIndex 사이를 카메라가 비행. 길이는 Haversine 거리 비율로 배분(`allocate_move_durations`).
- `map_hold` — 정지 지점에 카메라 정지(`stop_seconds`). 이 프레임 PNG가 사진 합성의 배경으로 재사용됨.
- `photo` — 사진 fade-in / hold / fade-out. Python(Pillow)에서 마지막 hold 프레임 위에 합성.

### 데이터 모델
- `trackPoints` — 전체 GPS 경로. **일반 트랙 포인트는 마커/정지를 만들지 않는다.**
- `mediaPoints` — `trackIndex`로 trackPoints를 가리키는 명명된 장소. `photos`가 있으면 정지+사진 시퀀스, 없으면 그냥 지나감.

### 브라우저 진입점 (Python → page.evaluate)
- `window.initializeMap()` — 맵 생성, terrain/atmosphere/buildings/route 레이어 추가, idle 대기.
- `window.renderRouteSegment(start, end, progress, waitMode)` — `map_move` 한 프레임.
- `window.renderStopPoint(trackIndex, name, waitMode)` — `map_hold` 한 프레임.
- `window.renderFrame(progress, waitMode)` — **레거시** 연속 카메라 진입점(`render_map_frames`에서만 사용).
- `window.isRenderReady()`, `getWebGLInfo()`, `warmUpRouteTiles()` — 준비상태/진단/타일 워밍업.

> 모든 프레임은 `flyTo`가 아니라 `map.jumpTo()`를 쓴다. 벽시계 시간과 분리된 **결정론적** 렌더를 위해서다.

---

## 3. 실행 환경 (백엔드에서 반드시 챙길 것)

원본은 `trailer3d` conda 환경 전제로 동작한다:
```bash
conda create -n trailer3d python=3.11 -y
conda run -n trailer3d python -m pip install -r requirements.txt
conda run -n trailer3d playwright install chromium
```
런타임 의존성 3가지를 백엔드 배포 환경에 반드시 확보해야 한다:
1. **Playwright Chromium** (headless) — `playwright install chromium` 필요.
2. **FFmpeg** (`libx264`) — `PATH`에 있어야 함. (선택: `ffprobe`)
3. **Mapbox public 토큰** — 현재는 `.env`에서 읽음.

검증/실행 명령:
```bash
conda run -n trailer3d python render_video.py            # 풀 1080x1920 / 30fps
conda run -n trailer3d python render_video.py --quick    # 540x960 / 15fps 빠른 테스트
conda run -n trailer3d python render_video.py --benchmark-frames 30   # 앞 N프레임만, 전체시간 추정
```
> 테스트/린트/빌드 단계는 **없다.** 검증은 실제 렌더(`--quick` / `--benchmark-frames`) 후 `output/`·`debug/` 이미지 확인으로 한다.

---

## 4. 백엔드로 옮길 때 핵심 고려사항 (가장 중요)

이건 단순 import가 아니라 **CLI → 서비스 전환**이다. 새로 작업할 때 다음을 결정/처리해야 한다:

1. **렌더는 느리고 길다 → 동기 HTTP 요청에서 돌리면 안 된다.**
   풀 렌더는 수십 초~수 분. 반드시 **백그라운드 작업(잡 큐: Celery/RQ/Dramatiq, 또는 FastAPI BackgroundTasks/asyncio worker)**으로 빼고, API는 `job_id`를 즉시 반환 → 상태 폴링/웹훅 구조로 가야 한다.

2. **`argparse` 의존을 끊어야 한다.**
   `render_video.py`는 CLI 인자(`build_config`가 `RenderConfig` 프리셋 위에 CLI override를 얹음)로 설정을 받는다. 백엔드에서는 **요청 파라미터/Pydantic 모델 → `RenderConfig`로 매핑**하는 진입 함수를 새로 만들어야 한다. `run()`을 그대로 호출하지 말고, 설정과 입력 데이터를 인자로 받는 순수 함수로 감싸라.

3. **입력 경로화.**
   현재 입력은 `--travel-data <path>` 파일. 백엔드에서는 요청 body(JSON)로 받아 임시 파일/메모리로 넘기는 어댑터가 필요. 검증 로직(`validate_travel_data`)은 그대로 재사용 가능.

4. **출력 처리.**
   지금은 `output/travel_3d_N.mp4` 로컬 자동증가 저장. 백엔드에서는 **job별 고유 경로 → S3/스토리지 업로드 → URL 반환**으로 바꿔야 한다. `next_output_path()` 자동증가는 멀티 워커에서 경쟁 조건 위험.

5. **동시성/리소스.**
   각 렌더가 Chromium 인스턴스 + FFmpeg 프로세스를 띄운다. 무겁다. **워커당 동시 렌더 수 제한**, 임시 디렉터리 격리(`temp/chromium_profile`가 공유되면 충돌), 작업 종료 후 정리가 필요. 디렉터리 상수(`ROOT`, `FRAMES_DIR`, `OUTPUT_DIR`, `TEMP_DIR`, `DEBUG_DIR`)는 모듈 레벨 고정값이라 **job별로 분리하도록 리팩터해야** 멀티 워커에서 안전하다.

6. **OS 의존성.**
   `find_font()`가 **Windows 폰트 경로**(`C:/Windows/Fonts/malgun.ttf`)를 쓴다. 리눅스 서버 배포 시 한글 폰트(예: Noto Sans CJK) 경로로 교체 안 하면 한글 라벨이 깨진다.

7. **WebGL 소프트웨어 렌더링 = 성능 절벽.**
   서버에 GPU가 없으면 SwiftShader/llvmpipe로 떨어져 `ReadPixels`가 극도로 느려진다. 시작 로그의 `[webgl] renderer=...`를 **반드시 확인.** 프로덕션 성능은 GPU 인스턴스 여부에 좌우된다.

8. **토큰 관리.**
   `.env`의 `MAPBOX_ACCESS_TOKEN`을 백엔드 시크릿 관리(환경변수/Secret Manager)로 옮길 것.

---

## 5. 권장 첫 작업 순서

1. 현 상태로 한 번 돌려서 동작 확인: `--quick` 또는 `--benchmark-frames 30`. `[webgl] renderer` 로그 체크.
2. `render_video.py`에서 **"설정 + 입력 데이터 → mp4 경로"를 반환하는 순수 진입 함수**를 추출 (argparse/파일경로/자동증가 출력 분리).
3. 그 함수를 백엔드 잡 워커에서 호출하는 엔드포인트 설계: `POST /renders`(job 생성) → `GET /renders/{id}`(상태/결과 URL).
4. 디렉터리 상수를 job별 임시 디렉터리로 파라미터화.
5. 폰트/출력/토큰을 리눅스·클라우드 환경에 맞게 교체.

---

## 6. 더 깊은 디테일

원본 프로젝트의 `CLAUDE.md`와 `README.md`에 bearing 스무딩, 캡처 모드(`cdp-png`/`cdp-jpeg`/`playwright-png`), 렌더 대기 모드(`map-render`/`raf`/`none`), 성능 통계(`PerfStats`) 등이 더 자세히 적혀 있다. 카메라 동작을 만질 때만 참고하고, **서비스화 작업에는 위 §4가 우선이다.**
