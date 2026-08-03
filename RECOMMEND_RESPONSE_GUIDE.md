# 추천코스 API 응답 처리 가이드

`POST /api/recommend/courses` 가 내려줄 수 있는 **모든 응답 종류**와 프론트 분기 방법.

- 소스: `routers/recommend.py` → `services/recommend_service.py` → `services/route_service.py` → `recommend/itinerary.py`
- 스키마: `schemas/recommend_schema.py`, `schemas/route_schema.py`

---

## STEP 1 — `code` 분기

| code | 언제 | 프론트 처리 |
| --- | --- | --- |
| **200** | 정상 (기차 실패·관광지 0 등 **부분 실패 포함**) | STEP 2로 |
| **422** | Pydantic 검증 실패 — 날짜 8자리 아님, `page` 범위 밖(0~3), 타입 오류 | 입력값 오류 안내 |
| **404** | `출발역을 찾을 수 없습니다.` / `도착역을 찾을 수 없습니다.` / `역을 찾을 수 없습니다.` | `message` 그대로 토스트 |
| **400** | `날짜 형식이 올바르지 않습니다 (YYYYMMDD).`<br>`오는날이 가는날보다 빠릅니다.`<br>`도착역 좌표가 없어 추천을 생성할 수 없습니다.`<br>`출발역 좌표가 없어 도착지 자동 추천을 할 수 없습니다.` (자동모드만) | `message` 그대로 토스트 |
| **502** | TourAPI 키 전량 거부 — `공공데이터포털 API 호출이 거부되었습니다. 일일 호출 한도 초과, 서비스 키 미등록·활용기간 만료, 또는 해당 공공API 장애일 수 있습니다.` | **문구 그대로 노출 금지.** "관광 정보를 불러올 수 없습니다. 잠시 후 다시 시도해주세요"로 치환 |
| **500** | 그 외 | 일반 에러 화면 |

> ⚠️ **기차(TAGO)를 못 불러와도 200이다.** `_fetch_routes`가 예외를 통째로 삼키고 note로 내려보낸다.
> 502는 **관광 데이터까지 죽었을 때만** 발생한다.

---

## STEP 2 — 배너 3층 (note가 나오는 위치)

| 위치 | 노출 지점 | 다루는 내용 |
| --- | --- | --- |
| `data.note` | 화면 최상단 | 결과 0건 |
| `data.destinations[].note` | 도착지 섹션 헤더 | 기차 실패 / 현지여행 / 추천지 없음 / 대도시 폴백 |
| `data.destinations[].itineraries[].note` | 카드 내부 하단 | 환승 안내 / 경유 제외 사유 |

### 2-1. `data.note` — 1종뿐

```
"추천 가능한 도착지를 찾지 못했습니다."   → destinations: [] 확정. 빈 상태 UI
```

그 외에는 항상 `null`.

### 2-2. `destinations[].note` — 6종

| note | 톤 | 같이 나타나는 현상 |
| --- | --- | --- |
| `null` | — | 정상 |
| `출발지와 도착지가 같아 기차 구간이 없습니다(현지 여행).` | info | 전 카드 `route_type: "현지"`, 기차 세그먼트 0 |
| `공공데이터포털 API 호출이 거부되었습니다. …` | ⚠️ warning | **기차 0편.** 문구 치환 필요 → "기차 정보를 불러오지 못했어요" |
| `기차 경로를 불러오지 못했습니다(코스만 제공).` | ⚠️ warning | **기차 0편.** 그대로 노출 가능 |
| `조건에 맞는 추천지를 찾지 못했습니다(TourAPI 실시간 조회 결과 없음).` | ⚠️ warning | **관광지 0곳.** 카드에 기차만. *도착역 지정 모드에서만 발생* |
| `테마 조건에 맞는 도착지 후보를 찾지 못해 인근 대도시를 추천했습니다.` | info | 자동모드 폴백, `destinations` 1개 |

### 2-3. `itineraries[].note` — `" / "` 로 join된 조합 문자열

`split(" / ")` 해서 칩(chip)으로 뿌리면 깔끔하다.

**환승** — 정보성 (파란 칩)

```
가는편 {역명} 환승
오는편 {역명} 환승
{역명}·{역명} 환승          ← 경유 다리 안에서 갈아탄 경우
```

**결손** — 주의 (노란 칩)

```
가는편 경로를 찾지 못했습니다.      ← 편도만 있는 반쪽 여정
오는편 경로를 찾지 못했습니다.
경유 경로는 불러오지 못했습니다.
  └ 키 거부가 원인이면 뒤에 사유 칩이 하나 더 붙는다:
    "공공데이터포털 API 호출이 거부되었습니다. 일일 호출 한도 초과, …"
    → 502·destinations[].note 와 동일하게 문구 치환 대상.
      앞 칩이 이미 사용자용 문구이므로 "공공데이터포털"로 시작하는 칩은 버려도 된다(로그·디버깅용).
```

**지정 경유 실패** — 사용자가 요청한 역이 빠졌다는 뜻. **반드시 노출**

```
{출발역}→{경유역} 구간에 운행 열차가 없어 {경유역} 경유를 제외했습니다.
{경유역}→{도착역} 구간에 운행 열차가 없어 {경유역} 경유를 제외했습니다.
{경유역} 경유는 2~6시간 체류 조건에 맞는 연결편이 없어 제외했습니다.
{경유역} 경유는 N박 숙박 경유에 맞는 연결편이 없어 제외했습니다.
  └ 꼬리로 " 대신 가는 길의 다른 역 경유를 추천합니다." 가 붙을 수 있음
```

조합 예:

```
"가는편 서대전 환승 / 강릉역 경유는 2~6시간 체류 조건에 맞는 연결편이 없어 제외했습니다. 대신 가는 길의 다른 역 경유를 추천합니다."
```

---

## STEP 3 — 카드 렌더링 분기

### 카드 개수 (고정 규칙)

```
auto_selected: false (도착역 지정)   →  destinations 1개  × itineraries 3개  (A/B/C)
auto_selected: true  (도착역 미지정) →  destinations 최대 3개 × itineraries 각 1개 (A/B/C가 목적지별)
```

예외: `itineraries: []` — 경로도 코스도 없을 때. 카드 자리에 빈 상태.

### `route_type` 4종

| 값 | 카드 표기 | 특징 |
| --- | --- | --- |
| `직통` | 직통 |  |
| `환승` | 환승 N회 | `segments`의 `kind:"train"` 개수로 카운트 |
| `경유` | {역명} 경유 | `via_station_idx` 있음 |
| `현지` | 기차 없음 | `label: "현지 여행"`, train 세그먼트 0, `total_travel_minutes: 0`, `total_fare: null` |

### 특수 카드 2종 (별도 UI 필요)

**A. 기차만 있는 카드** — 관광지가 하나도 없을 때

```json
{
  "title": null,
  "main_themes": [],
  "cover_image_url": null,
  "total_preference_score": 0.0,
  "is_round_trip_closed": false,
  "segments": [{ "kind": "train" }]
}
```

→ 썸네일·제목·테마 칩 전부 폴백 처리. "기차편만 안내" 뱃지 권장.

**B. 중복 카드** — 관광지 부족한 목적지에서 서버가 앞 카드를 복제

```
plan_label만 다르고 segments 내용이 앞 카드와 100% 동일
```

→ `page`를 올릴수록 잦아진다. 프론트에서 `segments` 해시 비교로 감지 가능.
숨길지 그대로 둘지는 기획 판단.

---

## STEP 4 — null 방어 체크리스트

전부 **정상 케이스**다. 옵셔널 체이닝 필수.

| 필드 | null 조건 | 폴백 UI |
| --- | --- | --- |
| `title` | 방문지 없음 | 목적지명으로 대체 |
| `cover_image_url` | 이미지 있는 방문지 없음 | 기본 이미지 |
| `main_themes` (`[]`) | 방문지 없음 | 칩 영역 숨김 |
| `score` | **도착역 지정 모드는 항상 null** | 점수 배지 숨김 |
| `total_fare` | 한 구간이라도 요금 미제공 | "요금 정보 없음" |
| `train.stop_station_count`<br>`train.stop_stations` | **SRT·임시열차는 항상 null**<br>(코레일 운행정보 API에 SRT 미수록) | 정차역 섹션 자체를 숨김 |
| `lodging` | **마지막 날(귀가일)은 항상 null**<br>/ 숙소 조회 실패 | 마지막 날은 정상, 그 외엔 "숙소 미정" |
| `place.visit_time`<br>`place.open_time` / `close_time` | 운영시간 파싱 실패 | 시각 표기 생략 |
| `segment.start_time` / `end_time` | 방문 시각 미상 | 타임라인 하단으로 정렬 |

---

## STEP 5 — 경유 관광지 주의사항

```
via_nights = 0 (당일치기 경유)
  → 경유역 관광이 두 기차 사이 visit 세그먼트로 들어감
  → reason 접두어: "경유역 근처 추천지 · …"   ← 목적지 관광과 구분하는 유일한 단서
  → 체류시간 안에 못 넣는 곳(폐점/개점 전)은 조용히 빠짐 → 경유인데 관광 0곳 가능

via_nights ≥ 1 (숙박 경유)
  → 경유역 관광이 코스의 "Day N"으로 편입. 경유 관광 전용 표기 없음
  → label(path)에 박수가 들어가 같은 경유역이라도 카드가 구분됨
```

**자동 경유는 테마 관련도 상위 3개만 응답에 담긴다.** 나머지 경유 경로는 아예 안 오므로
프론트에서 "왜 이 역만?" 처리는 불필요.

---

## 최소 방어 코드

```tsx
const dest = res.data.destinations ?? []
if (!dest.length) return <Empty msg={res.data.note} />

dest.map(d => (
  <Section note={d.note}>
    {d.itineraries.length === 0
      ? <Empty />
      : d.itineraries.map(it => (
          <Card
            title={it.title ?? d.destination_name}
            cover={it.cover_image_url ?? DEFAULT_IMG}
            trainOnly={it.segments.every(s => s.kind === 'train')}
            local={it.route_type === '현지'}
            chips={(it.note?.split(' / ') ?? []).filter(c => !c.startsWith('공공데이터포털'))}
          />
        ))}
  </Section>
))
```

---

## 자주 터지는 3종

1. **기차 없이 관광지만** — `destinations[].note` 확인 → 기차 섹션 숨김
2. **A/B/C가 동일한 카드** — 관광지 부족 목적지의 복제 채움
3. **경유 요청했는데 다른 역이 나옴** — 지정 경유 실패 → 자동 폴백. `itineraries[].note`에 사유 있음, 반드시 노출
