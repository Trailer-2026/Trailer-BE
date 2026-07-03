# recommend/ — AI 여행 코스 추천 엔진

> 이 디렉터리에서 작업할 때 따를 가이드. 상위 `CLAUDE.md`(레이어드 아키텍처·트랜잭션 규칙)도 함께 적용된다.

## 정체성

**순수 계산 모듈**이다. DB·FastAPI·외부 API에 의존하지 않는다.

- ❌ 여기에 DB 세션(`Session`)·네트워크 호출·`Config` 읽기를 넣지 마라.
- ✅ 입력은 이미 만들어진 값 객체(`ScoredPlace`/`AreaProfile`)와 `SearchCriteria`·`Party`(스키마), 출력은 `Course`(스키마)/순위 매겨진 `AreaProfile`.
- 데이터 적재(TourAPI 실시간 호출)·역 매핑·앵커링·숙소 배정·기차 결합은 **`services/recommend_service.py`** 가 이 엔진을 감싸서 한다. 그 경계를 흐리지 마라.

## 설계 원칙: 속도 1순위 / 최적성 2순위

코스 구성 = 오리엔티어링 문제(prize-collecting multi-day TSP). 정확해(Held-Karp·ILP·메타휴리스틱)는 **의도적으로 배제**하고, **cluster-first → route-second 휴리스틱**을 쓴다. 후보 수십 개에서 전체 ~0.25ms, 외부 의존성 0(numpy도 안 씀, 순수 파이썬).

새 기능을 넣을 때도 이 원칙을 깨지 마라 — 무거운 최적화/외부 라이브러리 도입 금지.

## 진입점

엔진은 두 개의 독립 진입점을 가진다.

**1) 코스 생성** (도착지 좌표가 정해진 뒤)
```python
pipeline.build_courses(scored, criteria, k, origin, first_cap=None, last_cap=None, day_windows=None) -> list[Course]
```
- `scored`: `scoring.score_places(places, themes)` 결과 (`list[ScoredPlace]`)
- `criteria`: `schemas.recommend_schema.SearchCriteria`
- `k`: 여행 일수(=클러스터 수), `origin`: 현지 기준점 좌표 `(lat, lng)`
- `first_cap`/`last_cap`: 첫날(도착일)·마지막날(귀가일) 관광지 상한. `recommend_service._day_caps`가
  main 노선 도착/출발 시각으로 계산해 넘긴다(없으면 전 일자 `_MAX_PER_DAY`). 열차 시각을 코스에
  반영하는 유일한 연결점 — 오후 도착이면 첫날을, 오전 귀가면 마지막날을 덜/안 채운다.
- `day_windows`: 날짜별 관광 가능 시간대 `[(start_h, end_h), …]`. `recommend_service._day_windows`가
  열차 도착/출발 시각으로 계산(없으면 전 일자 기본 9~21시). `scheduling`이 이 시간대 안에서
  **관광지 운영시간(오픈/마감·휴무요일)**에 맞춰 방문 시각을 배정하고, 안 맞는 곳은 차순위로 대체한다.

**2) 도착지 자동 선택** (도착역 미지정 시 — theme + party 기준)
```python
destination.rank_and_diversify(profiles, themes, party, origin, nights, max_travel_minutes, top_k) -> list[AreaProfile]
```
- `profiles`: 서비스가 라이브 스캔(`tour_place.scan_area_profiles`)→역 매핑으로 만든 `list[AreaProfile]`
- 결과: 점수·권역 다양성으로 고른 상위 `top_k` 도착지 후보(역 포함). 상세는 **도착지 선택 로직** 섹션 참조.

## 모듈

코스 파이프라인(①~③④ + 조립)과, 그 앞단의 도착지 선택(`destination.py`)으로 나뉜다.

| 파일 | 역할 |
|---|---|
| `types.py` | 내부 값 객체: `ScoredPlace`(점수화된 장소), `Cluster`(Day 묶음), `Tour`(방문순서) |
| `scoring.py` | ① 가중 코사인 유사도로 테마 적합도 0~1점 (`score_places`) |
| `clustering.py` | ② k-means(결정적)로 일수만큼 날짜 묶기 (`kmeans_by_geo`) |
| `routing.py` | ③④ Nearest Neighbor + 2-opt + 순환 복귀, `haversine` (`nearest_neighbor`/`two_opt`/`close_cycle`) |
| `scheduling.py` | ⑤ Day 내부 **시각 스케줄링**. 관광지 운영시간(오픈/마감·휴무요일)에 맞춰 방문 시각 배정·재정렬, 운영시간 밖이면 차순위 후보로 대체(소프트). 운영시간 정보가 없는 날은 `routing` 동선 순서로 폴백 (`schedule_day`) |
| `pipeline.py` | 단계 조립 → 코스 3개(A/B/C). 점수 인터리브로 겹침 0, 다중 테마 쿼터 균형, 하루 최대 3곳(첫/마지막날은 열차 시각 기반 `first_cap`/`last_cap`으로 축소), Day 순서는 `scheduling`이 운영시간 반영해 확정 |
| `destination.py` | **도착지 선택**(코스 파이프라인과 별개). 도착역 미지정 시 `theme + party` 기준으로 시도 area 후보를 점수화·권역 다양성 필터 (`rank_and_diversify`, 값 객체 `AreaProfile`) |

## 도착지 선택 로직 (`destination.py`)

`services.recommend_service`는 도착역 지정 여부로 분기한다.
- **지정**: 그 역 좌표를 `origin`으로 `build_courses` → 코스 A/B/C(단일 도착지).
- **미지정**: **2단계 점수화**로 순위·도착역·코스를 모두 *도착역 주변* 기준으로 일치시킨다.
  - **Phase A (거친 선별)**: `scan_area_profiles`(시도별 테마분포 라이브 스캔)는 큰 도를 **지리적 부분권(해안/내륙 등)으로 분리**해 각각 후보로 낸다(경북 동부해안→포항, 내륙→영천처럼). 각 후보를 `nearest_major`로 도착역에 매핑 → `rank_and_diversify(top_k=_SHORTLIST_N)`로 상위 N(기본 5, 권역 다양성)개를 추린다. 같은 본부는 점수 높은 부분권 하나만 살아남아(해안권이 내륙권을 이긴다) 해안 도시가 후보에서 누락되지 않는다.
  - **Phase B (정밀 재점수)**: 후보별 **도착역 주변을 실측 스캔**(`tour_place.live_places`)해 그 분포로 `AreaProfile`을 다시 만들어 `rank_and_diversify(top_k=3)`로 최종 top-3. 이 스캔 결과를 **여정 생성에 재사용**(`_itineraries_from`)하므로 추가 호출은 +2회 수준. 코스 앵커는 **도착역 좌표**다.
  - 효과: 시도 평균이 아닌 *역 주변 실측*으로 최종 순위를 매겨, "나주 도착인데 일정은 44km 떨어진 보성"처럼 도착역과 코스가 어긋나던 문제를 없앤다. 내륙역은 주변 실측이 약하면 자연히 밀린다.
  - ⚠️ **철도 도달성**: area 중심과 매핑된 도착역의 거리가 `MAX_STATION_GAP_KM`를 넘으면(바다 건너 제주 등) Phase A에서 제외한다(기차 여행 플랫폼이라 철도 미연결 지역은 도착지가 될 수 없음).

점수식 (가중치는 모듈 상수, 튜닝 가능):
```
score = WEIGHT_THEME·theme_fit + wAge·age_fit + WEIGHT_ACCESS·access_fit − group_penalty
```
- `theme_fit`: 선택 테마들이 **고루 분포**할수록 1(정규화 기하평균 `m×(∏p_t)^(1/m)`). 한 테마라도 비면 0. 테마 미선택/단일 테마면 1.0. 단순 합이 아니라 균형으로 보는 이유: Phase B의 `live_places`가 선택 테마만 걸러오므로 합은 항상 ≈1이 되어 변별이 사라진다 → "OCEAN+FOOD 둘 다 풍부한가"를 균형으로 판별(바다 없는 내륙 미식 도시는 0점).
- `age_fit`: **테마→연령 적합 휴리스틱 상수표 `_AGE_SUIT`** 로 지역 적합도를 구해 `party`(adult/youth/child) 인원비율로 가중평균. ⚠️ 지역별 연령 실데이터가 없어 휴리스틱이다(주석 명시). 인원 미입력이면 성인 기준.
- `access_fit`: nights 대비 적정 거리(`_IDEAL_KM`)에 가까울수록 1, 너무 가깝/멀수록 0. 상한(`_max_distance`, `max_travel_minutes` 반영) 초과 후보는 제외.
- `group_penalty`: 총인원 ≥ `GROUP_LARGE`이고 지역 그룹친화도(`_GROUP_FRIENDLY` 표)가 낮으면 약한 감점.
- **어린이 동반(`party.child>0`)** 이면 `wAge`(0.3→0.4)↑·`wAccess`(0.2→0.1)↓ 자동 전환.
- **다양성**: 같은 권역(`province`, 보통 `Station.region` 본부)은 최대 1곳만 통과 → 한 지역 쏠림 방지.

거리·시간 추정(`_RAIL_KMH`)은 Haversine 근사다. 실제 기차 시간표 연동은 추후 인터페이스 자리(주석)로 비워둔다.

## 규칙·상수 (바꿀 때 주의)

- `pipeline._NUM_COURSES = 3` — 사용자가 셋 중 하나 선택. `_MAX_PER_DAY = 3` — 하루 방문지 상한.
- 다중 테마: `_select_working`이 테마별 쿼터로 균형을 맞춘다(한 테마 쏠림 방지). 단일/0개 테마면 점수 상위 그대로.
- **테마 미선택 방어**: 프론트가 테마 최소 1개 선택을 강제하지만, 백엔드도 빈 테마를 방어한다 — `SearchCriteria.themes`는 스키마상 빈 리스트를 허용하고, 빈 값이 들어오면 `utils/tour_place.py:_DEFAULT_CTYPES`(관광지12·문화14·음식39)로 기본 조회한다. 프론트 검증을 신뢰하되 잘못된/직접 호출로 조용히 빈 추천이 나가지 않도록 최후 방어선으로 남겨둔 것. 스키마에서 `min_length=1`을 강제하지 않는 이유가 이 폴백을 살리기 위함이니, 폴백을 지우려면 스키마 강제를 먼저 넣어라.
- `destination.py` 가중치/휴리스틱 표(`WEIGHT_*`, `_AGE_SUIT`, `_GROUP_FRIENDLY`, `_IDEAL_KM`, `GROUP_LARGE`)는 전부 모듈 상수다. 값을 바꿔 튜닝하되, 연령/그룹 표는 **실데이터가 아니라 휴리스틱**임을 잊지 마라(데이터가 생기면 표를 교체).
- `Theme`는 `core.enums`에서 import. 출력 타입(`Course`/`DayPlan`/`RecommendedPlace`)·`Party`는 `schemas.recommend_schema`.
- 클러스터링·NN·도착지 점수화는 결정적이어야 한다(같은 입력 → 같은 추천). `random`/`Date.now` 류 비결정 요소 넣지 마라.
- 거리 계산은 `routing.haversine`로 통일.

## 관광지 운영시간 (open/close) 반영

코스가 관광지 **오픈/마감 시각·휴무요일**을 고려해 방문 순서·시각을 정한다. 두 레이어로 나뉜다.

- **데이터 취득 — `services/recommend_service.py` + `utils/tour_place.py`** (네트워크)
  - `locationBasedList2`엔 운영시간이 없다. `detailIntro2`(콘텐츠타입별 상세)에서만 나온다.
  - `_attach_hours`가 **코스에 실제 배정될 후보(`pipeline.working_set(scored, themes, k)`, ≈일수×9)에 한해**
    `tour_place.fetch_hours`(detailIntro2 병렬)로 운영시간을 채운다. 장소당 1콜이라 코스 후보로만 제한(quota·속도 보호).
    조회 대상은 반드시 `build_courses`와 **같은 `working_set`**이어야 한다(다중 테마 시 테마 쿼터로 원점수 상위 N개와
    달라져, `scored[:max_working]`로 조회하면 차순위 후보가 미조회인 채 코스에 섞인다).
  - 파싱은 자유텍스트라 방어적(`_parse_hours`/`_parse_closed_weekdays`): `HH:MM~HH:MM` 앞 구간, `24시간·상시·연중무휴`,
    `매주 X요일` 정도만 해석. 자정 넘김은 +24. 격주·첫째주 등 불규칙 휴무는 과제약을 피해 무시. **미상은 시간 제약 없음**으로 둔다.
- **스케줄링 — `recommend/scheduling.py`** (순수 계산)
  - `schedule_day`는 **식사와 관광을 분리**한다: 식당(`content_type_id=39`)은 점심(~12시)·저녁(~18시)
    앵커에만 배치(`_MEALS`, **하루 최대 2끼**), 관광지는 동선(NN+2-opt+원점복귀) 순으로 식사 점유 시간을
    피해 2.5h 슬롯에 채운다. → "밥 먹고 또 밥"(식당 연속) 방지. 식당만 있어도(FOOD 단독) 2끼까지만.
  - 운영시간은 소프트 제약: 개점 전이면 미루고, 마감/창을 넘으면 그 곳을 건너뛰고 **차순위로 대체**.
    그 날 휴무인 곳은 제외. 채울 비식당이 부족하면 가짜 식사로 메우지 않고 그 시간을 비운다.
  - `ScoredPlace.open_hour/close_hour/closed_weekdays`(recommend_service가 채움)를 읽고, 결과 방문 시각은
    `RecommendedPlace.open_time/close_time/visit_time`(HH:MM)로 노출된다. 방문시각은 `reason`에도 표기.
  - ⚠️ **시간 가정**: 관광지 1곳 `_HOURS_PER_PLACE`=**2.5h(150분) 점유**(관람+이동 뭉뚱그림).
    **관광지 간 실측 이동시간은 계산하지 않는다**(Haversine 거리는 방문 *순서* 최적화에만 쓰고 시각 배정엔 미반영).
    관광지 `visit_time`은 하루 시작(첫날=열차 도착, 그 외 09:00)부터 2.5h 간격으로 찍히되 **식사(식당) 구간은 건너뛴다**.
    식당은 점심(~12)·저녁(~18) 앵커 시각에 배정된다(균등 그리드가 아님).
    `_HOURS_PER_PLACE`는 **`recommend/scheduling.py`·`services/recommend_service.py` 두 곳에 동일**하며 `_day_caps`(방문 개수 상한)·
    `_day_windows`(시각 배정)가 함께 쓰므로 바꿀 땐 둘 다 맞춘다. 실측 이동시간 연동은 추후.

## 경유역 관광지 (via / stopover)

사용자가 "가는 길에 특정 역을 들르고 싶다"를 표현하는 기능. 요청 필드는 `SearchCriteria.via_station_idx`
(관광지 place_idx가 아니라 **경유할 역의 station_idx**. 옛 `waypoint_place_idxs`는 미배선 죽은 필드라 제거됨).

경유는 두 종류다. **지정 경유**(사용자가 `via_station_idx`로 역을 콕 집음)와 **자동 경유**(도착역만 지정 →
가는 길 중간역을 시스템이 추천). 둘 다 경유 경로엔 역 근처 관광지가 붙는다. 구현은 두 레이어로 나뉜다(섞지 마라):

- **경로 생성 — `services/route_service.py:recommend(via_station_idx=...)`** (기차 그래프·시각표만)
  - 지정 경유: 그 역을 2~6h 관광 체류로 경유. **가는편 경유**(`_stopover`, path=출발→지정역→도착)와
    **오는편 경유**(`_stopover_return`, path=출발→도착→지정역→출발)를 둘 다 시도해 성립하는 편을 모두 낸다.
    방향은 별도 필드 없이 **path·기차 시각**으로 드러난다(코스/프론트가 그걸로 배치·구분). **자동 중간역은 만들지 않음**.
    반환 `[main, (가는편경유?), (오는편경유?)]`. 둘 다 열차 없으면 `[main]`+`main.note` 안내. 출발·도착역과 같으면 무시.
  - **경유값 방어**: `via_station_idx`는 `SearchCriteria` 검증기가 `≤0`(Swagger 기본값 0 포함)을 `None`으로 정규화한다. route_service도 미존재·철도 미지원·좌표 없는 역이면 **예외 없이 조용히 경유만 생략**(직통 등 열차 정보를 통째로 잃지 않도록) → 자동 경유 모드로 진행.
  - 자동 경유: `_candidate_stops`가 지리적 중간역을 뽑되 **출발·도착 양쪽에서 `max(MIN_LEG_KM, base·MIN_LEG_RATIO)` 이상 떨어진 역만**
    (서울→용산/구포처럼 종점에 붙은 역 제외) + 우회비율 `MAX_DETOUR_RATIO` 이내. 열차가 성립하는 후보 전부를 `[main, 경유들…]`로 반환.
    **여기선 상위 N개로 자르지 않는다** — 최종 선별은 테마 기준이라 recommend_service 몫. 각 경유 route엔 `via_station_idx`가 세팅됨.
- **테마 선별 + 관광지 부착 — `services/recommend_service.py:_enrich_stopovers`** (관광 데이터)
  - `route_service.recommend` 직후 호출. 각 경유역 주변을 `tour_place.live_places(radius_m=_VIA_RADIUS_M)`로 **역 근처(기본 3km)** 만 스캔,
    선택 테마와 겹치는 관광지 **수를 '테마 관련도 점수'** 로 쓰고(그 스캔 결과를 노출에도 재사용), `haversine` 가까운 순 상위 `_VIA_PLACES_N`(기본 3)곳을 `stopover_places`에 붙인다.
  - **자동 경유면** 테마 관련도↓(동점 시 이동시간↑)로 정렬해 상위 `_STOPOVER_N`(기본 3)개만 남기고 나머지 경유는 버린다. **지정 경유면** 단일 경유라 그대로. 직통/환승은 항상 유지·선두.
  - route_service가 아니라 여기서 하는 이유: **route_service는 기차 그래프만**, 관광 데이터·테마는 `tour_place`+`recommend_service` 담당(레이어 경계).

출력 타입 `StopoverPlace`와 `RouteCandidate.stopover_places`/`via_station_idx`는 **`schemas/route_schema.py`** 에 있다
(`recommend_schema`가 `route_schema`를 import하므로 `RecommendedPlace` 재사용은 순환 참조라 불가 → route_schema에 독립 정의).
경유 관광지는 "어떤 기차를 타느냐"에 종속되므로 **코스(`Course`)가 아니라 경로(`RouteCandidate`)에** 붙인다(코스와 route는 형제, 코스는 route 선택과 무관).

## 비범위

- 기차 구간: 여기서 안 푼다. `services/route_service.py`(그래프 탐색)가 담당. 경유역(`via_station_idx`) 경로 생성도 route_service 몫(위 "경유역 관광지" 참조).
- 관광지·숙소 데이터: 실시간 TourAPI 호출이며 `utils/tour_place.py` + `recommend_service`가 담당(경유역 근처 관광지 부착 포함).

## 응답 스키마
```
CommonResponse
  ├─ code: int                      // 200
  ├─ message: str                   // "추천 코스 생성 성공"
  └─ data: RecommendResponse
     ├─ auto_selected: bool         // 도착지 지정=false / AI자동=true
     ├─ note?: str
     └─ destinations[]: DestinationPlan
        ├─ destination_station_idx: int
        ├─ destination_name: str
        ├─ score?: float            // AI자동일 때만 값, 지정이면 null
        ├─ note?: str
        │
        └─ itineraries[]: Itinerary           ◀ 경로별 통합 여정 (기차+관광+숙소 한 몸)
           ├─ label: str           // 경로 표기 "서울→대전→부산"
           ├─ route_type: str      // "직통" | "경유" | "현지"(기차 없음)
           ├─ via_station_idx?: int
           ├─ total_preference_score: float
           ├─ total_travel_minutes: int   // 순수 기차이동(체류 제외)
           ├─ total_fare?: int
           ├─ is_round_trip_closed: bool
           ├─ note?: str
           └─ segments[]: ItinerarySegment   // 시간순: 가는기차→(경유관광)→목적지관광·숙소→오는기차
              ├─ kind: str         // "train" | "visit" | "lodging"
              ├─ day_no: int       // 1=Day1
              ├─ start_time?: datetime   // 열차 출발 / 방문 시작 (KST)
              ├─ end_time?: datetime     // 열차 도착 / 방문 종료 (KST)
              ├─ train?: RouteTrain      // kind=train (train_no/grade/dep·arr_station/dep·arr_time/duration_minutes/fare)
              ├─ place?: RecommendedPlace  // kind=visit (경유역·목적지 공통. place_idx/name/lat·lng/themes/
              │                            //   preference_score/reason/image_url/open·close·visit_time)
              └─ lodging?: Lodging       // kind=lodging (name/lodging_type/region/lat·lng/tel/image_url)
```

> 내부 엔진 타입(응답 비노출): `RouteCandidate`·`Course`/`DayPlan`·`StopoverPlace`. recommend.itinerary.build_itinerary가
> (경로, 코스)를 시간순 segments로 병합한다. 경유역 관광은 두 기차 다리 사이 visit 세그먼트로 편입.
> **코스는 경로별로 재계산**(`_itineraries_from`→`_course_for_route`): 장소조회·운영시간은 목적지당 1회
> (`_prepare_scored`), `build_courses`는 경로마다 그 경로의 도착/출발 시각(`_day_caps`/`_day_windows`)에 맞춰
> 다시 돌린다 → 경유의 늦은 도착이 첫날 관광량에 반영된다. 숙소 조회는 경로 간 memo 공유로 중복 방지.
>
> **경유역 1박(날짜 넘는 경유)**: 여행 3일 이상이면 `_fetch_routes`가 숙박 경유 변형도 요청한다
> (route_service `via_nights=1`, 가는편/오는편 양방향, 지정·자동 공통). 숙박 경로는 `_course_for_overnight`가
> **두 도시(경유·목적지)로 코스를 나눠** 만든다: 전이 열차 날짜로 일수를 가르고, 먼저 묵는 도시는
> 도착일만 제약(자고 다음날 이동), 나중 도시는 도착~귀가 표준. day_no·날짜를 1..k로 재부여해 병합하고
> 좌표 기반 숙소 배정으로 도시별 숙소가 붙는다. 조립기(`itinerary.build_itinerary`)는 세그먼트를
> **시각순 정렬**해 leg2(경유→목적지)가 경유 관광 뒤·목적지 관광 앞에 자연히 놓인다. 숙박 경유는
> stopover_places를 안 쓰고(관광이 코스 날에 들어감) `_enrich_stopovers`가 top-N 선별에만 참여시킨다.