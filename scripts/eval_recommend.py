# -*- coding: utf-8 -*-
"""추천 엔진 오프라인 성능 평가 — 서버·DB 무관, 발표용 정량 지표를 마크다운 표로 뽑는다.

두 단계로 나뉜다. 관광 API 쿼터를 아끼기 위해 **후보 데이터는 한 번만 받아 파일로 저장**하고,
평가는 그 파일로 몇 번이고 다시 돌린다.

    python scripts/eval_recommend.py --snapshot       # 코스용: TourAPI 호출 → scripts/eval_snapshot.json
    python scripts/eval_recommend.py --snapshot-dest  # 도착지용: 시도 스캔 + 역 매핑(DB) → scripts/eval_dest_snapshot.json
    python scripts/eval_recommend.py                  # 스냅샷으로 평가 → 표 출력 (호출 0)
    python scripts/eval_recommend.py --md out.md      # 표를 파일로

--snapshot 비용: 시나리오(역 × 테마조합)마다 locationBasedList2 몇 콜 + 운영시간(detailIntro2)
최대 27콜. detailIntro2가 하루 1,000/키라 시나리오 18개면 ≈500콜 — 추천 검색 쿼터를 그만큼 먹는다.
실행 전에 콜 수를 찍고 확인을 받는다.

지표 (코스 1개 기준, 시나리오·코스 평균으로 집계)
- 운영시간 충족률: 운영시간이 알려진 방문지 중 [오픈, 마감) 안에 배정되고 휴무일이 아닌 비율
- 시간창 준수율: 하루 일정(첫 방문 ~ 마지막 방문 + 체류)이 그 날 관광 가능 시간대 안에 든 날의 비율
- 선택 테마 커버리지: 고른 테마 중 코스에 실제로 들어간 테마 비율 (다중 테마 균형)
- 테마 다양성: 코스 안 서로 다른 테마 수
- 유형 다양성: 서로 다른 콘텐츠 유형(관광지·문화·음식…) 수 / 방문지 수
- 코스 간 중복: A/B/C 방문지 교집합 수 (버킷 분리로 기본 0, 중간 날 관광지 보충분만 겹칠 수 있음)
- 하루 이동거리: 기준점 → 방문지들 직선거리 합 (km)
- 엔진 시간: build_courses 1회 ms

베이스라인 두 개를 같은 입력·같은 지표로 돌려 나란히 둔다.
- 인기순: 점수 상위 N개를 순서대로 하루 3곳씩. 거리·운영시간 무시.
- 가까운순: 기준점에서 가장 가까운 곳부터 그리디로 하루 3곳씩. 운영시간 무시.

두 번째 표 — 도착지 자동 선택(도착역 미지정)의 **지역 다양성**. 시도별 테마 분포(areaBasedList2,
쿼터 여유 오퍼레이션)를 한 번 스캔해 저장하고, 출발역 4곳 × 테마 3조합 × 1~2박으로 top-3 도착지를
뽑아 잰다. 운영 경로의 Phase A(거친 선별)까지만이다 — Phase B(역 주변 실측 재점수)는 후보마다
locationBasedList2 를 더 부르므로 뺐다. 역 매핑은 서비스와 같은 KTX 정차역 목록(DB)을 스냅샷에 담는다.
- 권역 분산율: top-3 가 서로 다른 관리 본부(8개)인 비율
- 권역 커버: 전체 시나리오의 top-3 에 한 번이라도 등장한 본부 수 / 8
- 베이스라인 점수순: 같은 점수식에서 다양성 필터만 뺀 top-3 / 관광지 수순: 테마 무시, 등록 관광지가 많은 순
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.enums import Theme
from recommend import destination, pipeline, routing, scheduling, scoring
from recommend.types import ScoredPlace
from schemas.recommend_schema import Party, SearchCriteria

SNAPSHOT = Path(__file__).resolve().parent / "eval_snapshot.json"
DEST_SNAPSHOT = Path(__file__).resolve().parent / "eval_dest_snapshot.json"

# 평가 시나리오. 역 좌표는 station 테이블 값(DB 없이 돌리려고 박아 둔다).
_STATIONS = {
    "부산역": (35.1151, 129.0413),
    "강릉역": (37.7638, 128.8998),
    "전주역": (35.8484, 127.1616),
    "경주역": (35.7981, 129.1389),
    "여수엑스포역": (34.7525, 127.7472),
    "안동역": (36.5760, 128.7267),
}
_THEME_SETS = [
    [Theme.OCEAN],
    [Theme.HISTORY, Theme.FOOD],
    [Theme.NATURE, Theme.HEALING, Theme.CULTURE],
]
_NIGHTS = (1, 2)  # 1박2일(k=2)·2박3일(k=3)
_GO_DATE = "20260501"  # 금요일 출발 — 휴무요일 판정에 요일이 필요하다
_MAX_K = 3


def _criteria(themes: list[Theme], nights: int) -> SearchCriteria:
    go = datetime.strptime(_GO_DATE, "%Y%m%d")
    return SearchCriteria(
        origin_station_idx=1, dest_station_idx=2, go_date=_GO_DATE,
        back_date=(go + timedelta(days=nights)).strftime("%Y%m%d"), themes=themes,
    )


# --------------------------------------------------------------------------- #
# 스냅샷 — 시나리오별 후보 + 운영시간을 한 번 받아 저장
# --------------------------------------------------------------------------- #
_POOL_MAX = pipeline._NUM_COURSES * _MAX_K * (pipeline._MAX_PER_DAY + pipeline._MEALS_PER_DAY)  # 45 (식당 없는 테마는 27에서 찬다)


def _snapshot_scenario(station: str, themes: list[Theme]) -> dict:
    """시나리오 1개의 후보(작업셋)+운영시간을 받는다 — 운영 경로(recommend_service._scan_places)와
    같은 완화 사다리(반경 확대 → 테마 완화)를 타서, 후보가 드문 역도 실제 서비스가 내는 코스로 잰다.
    테마를 완화했으면 운영과 같이 빈 테마로 점수화한다(안 그러면 전부 0점 탈락)."""
    from utils import tour_place

    lat, lng = _STATIONS[station]
    scan = tour_place.live_places_relaxed(lat, lng, themes, min_count=pipeline._MAX_PER_DAY * _MAX_K)
    scored = scoring.score_places(scan.places, [] if scan.themes_relaxed else themes)
    pool = pipeline.working_set(scored, themes, _MAX_K)
    hours = tour_place.fetch_hours([(str(sp.place_idx), sp.content_type_id) for sp in pool if sp.content_type_id])
    rows = []
    for sp in pool:
        h = hours.get(str(sp.place_idx))
        d = asdict(sp)
        d["themes"] = [t.value for t in sp.themes]
        if h is not None:
            d["open_hour"], d["close_hour"], d["closed_weekdays"] = h.open_hour, h.close_hour, list(h.closed_weekdays)
        rows.append(d)
    print(f"  {station} {[t.value for t in themes]}: 후보 {len(scan.places)} → 작업셋 {len(pool)}"
          f"{' (반경 확대)' if scan.widened else ''}{' (테마 완화)' if scan.themes_relaxed else ''}")
    return {"station": station, "themes": [t.value for t in themes],
            "widened": scan.widened, "themes_relaxed": scan.themes_relaxed, "places": rows}


def snapshot(sparse_only: bool) -> None:
    """전체 시나리오를 새로 받거나(sparse_only=False), 작업셋이 덜 찬 시나리오만 다시 받는다."""
    existing = {(sc["station"], tuple(sc["themes"])): sc for sc in json.loads(SNAPSHOT.read_text(encoding="utf-8"))}         if sparse_only and SNAPSHOT.exists() else {}
    scenarios = [(s, ts) for s in _STATIONS for ts in _THEME_SETS]
    todo = [(s, ts) for s, ts in scenarios
            if not sparse_only or len(existing.get((s, tuple(t.value for t in ts)), {}).get("places", []))
            < (_POOL_MAX if Theme.FOOD in ts else pipeline._NUM_COURSES * _MAX_K * pipeline._MAX_PER_DAY)]
    print(f"시나리오 {len(todo)}개 — detailIntro2 최대 {len(todo) * _POOL_MAX}콜")
    if input("계속할까요? [y/N] ").strip().lower() != "y":
        return
    for station, themes in todo:
        existing[(station, tuple(t.value for t in themes))] = _snapshot_scenario(station, themes)
    out = [existing[(s, tuple(t.value for t in ts))] for s, ts in scenarios if (s, tuple(t.value for t in ts)) in existing]
    SNAPSHOT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"저장: {SNAPSHOT}")


def _load() -> list[dict]:
    if not SNAPSHOT.exists():
        sys.exit(f"스냅샷이 없습니다. 먼저 --snapshot 을 돌리세요: {SNAPSHOT}")
    data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    for sc in data:
        sc["places"] = [
            ScoredPlace(**{**d, "themes": [Theme(t) for t in d["themes"]], "closed_weekdays": tuple(d["closed_weekdays"])})
            for d in sc["places"]
        ]
    return data


# --------------------------------------------------------------------------- #
# 지표 — 일정을 [(day_no, weekday, window, [(place, arrive_h), …]), …] 로 통일해 잰다
# --------------------------------------------------------------------------- #
_DEFAULT_WINDOW = (scheduling._DAY_START, scheduling._DAY_END)


def _metrics(days: list[tuple[int, list[tuple[ScoredPlace, float]]]], origin, selected: set[Theme] = frozenset()) -> dict:
    go = datetime.strptime(_GO_DATE, "%Y%m%d")
    known = ok_hours = 0
    day_ok = 0
    km = 0.0
    all_places = []
    for idx, visits in days:
        weekday = (go + timedelta(days=idx)).weekday()
        lo, hi = _DEFAULT_WINDOW
        prev = origin
        for p, arrive in visits:
            all_places.append(p)
            km += routing.haversine(prev[0], prev[1], p.lat, p.lng)
            prev = (p.lat, p.lng)
            if p.open_hour is not None or p.close_hour is not None or p.closed_weekdays:
                known += 1
                open_ok = p.open_hour is None or arrive >= p.open_hour - 1e-9
                close_ok = p.close_hour is None or arrive < p.close_hour
                if open_ok and close_ok and weekday not in p.closed_weekdays:
                    ok_hours += 1
        if visits:
            first, last = visits[0][1], visits[-1][1] + scheduling._dwell(visits[-1][0])
            day_ok += first >= lo - 1e-9 and last <= hi + 1e-9
    n = len(all_places) or 1
    return {
        "hours_ok": ok_hours / known if known else None,
        "window_ok": day_ok / len(days) if days else None,
        "cat_div": len({p.content_type_id for p in all_places}) / n,
        "theme_div": len({t for p in all_places for t in p.themes}),
        # 선택 테마 중 코스에 한 곳이라도 들어간 테마의 비율 — "바다+맛집 골랐는데 맛집만" 을 잡는다
        "theme_cov": (len({t for p in all_places for t in p.themes} & selected) / len(selected)) if selected else None,
        "km": km,
        "ids": {p.place_idx for p in all_places},
        "n": len(all_places),
    }


def _ours(scored, criteria, k, origin):
    t0 = time.perf_counter()
    courses = pipeline.build_courses(scored, criteria, k, origin)
    ms = (time.perf_counter() - t0) * 1000
    by_idx = {p.place_idx: p for p in scored}
    results = []
    for c in courses:
        days = []
        for d in c.days:
            visits = [(by_idx[rp.place_idx], _to_hour(rp.visit_time)) for rp in d.places]
            days.append((d.day_no - 1, visits))
        results.append(_metrics(days, origin, set(criteria.themes)))
    return results, ms


def _to_hour(hhmm: str | None) -> float:
    if not hhmm:
        return scheduling._DAY_START
    h, m = hhmm.split(":")
    return int(h) + int(m) / 60


def _naive_days(order: list[ScoredPlace], k: int, origin) -> list:
    """순서대로 하루 3곳씩 끊고, 9시부터 체류+이동시간을 더해 방문 시각을 준다(운영시간 무시)."""
    per = pipeline._MAX_PER_DAY
    days = []
    for idx in range(k):
        chunk = order[idx * per:(idx + 1) * per]
        t, prev, visits = scheduling._DAY_START, None, []
        for p in chunk:
            if prev is not None:
                t += scheduling._dwell(prev) + scheduling._travel_h(prev, p)
            visits.append((p, t))
            prev = p
        days.append((idx, visits))
    return days


def _baseline_popular(scored, criteria, k, origin):
    """점수 상위 N개를 3개 버킷으로 인터리브(우리와 같은 비겹침 조건) → 점수순 그대로."""
    working = pipeline.working_set(scored, criteria.themes, k)
    return [_metrics(_naive_days(working[i::3], k, origin), origin, set(criteria.themes)) for i in range(3)]


def _baseline_nearest(scored, criteria, k, origin):
    """같은 버킷에서 기준점 최근접 그리디 순."""
    working = pipeline.working_set(scored, criteria.themes, k)
    out = []
    for i in range(3):
        bucket = list(working[i::3])
        order, cur = [], origin
        while bucket:
            nxt = min(bucket, key=lambda p: routing.haversine(cur[0], cur[1], p.lat, p.lng))
            bucket.remove(nxt)
            order.append(nxt)
            cur = (nxt.lat, nxt.lng)
        out.append(_metrics(_naive_days(order, k, origin), origin, set(criteria.themes)))
    return out


# --------------------------------------------------------------------------- #
# 도착지 자동 선택 — 지역 다양성
# --------------------------------------------------------------------------- #
_ORIGINS = {  # 출발역 (도착지 후보는 전국)
    "서울역": (37.5547, 126.9707),
    "대전역": (36.3315, 127.4346),
    "광주송정역": (35.1378, 126.7925),
    "부산역": (35.1151, 129.0413),
}
_PROVINCES = 8  # core.enums.RailRegion 본부 수


def snapshot_dest() -> None:
    """테마조합별 시도 스캔(areaBasedList2) + 최근접 KTX역·본부 매핑(DB 1회)을 저장한다."""
    from databases.database import SessionLocal
    from databases.daos import station_dao
    from utils import tour_place

    db = SessionLocal()
    majors = station_dao.major_candidates(db)
    out = []
    for themes in _THEME_SETS:
        scans = tour_place.scan_area_profiles(themes)
        rows = []
        for sc in scans:
            st = station_dao.nearest_of(majors, sc.centroid[0], sc.centroid[1])
            rows.append({
                "area_code": sc.area_code, "centroid": list(sc.centroid), "total": sc.total,
                "theme_counts": {t.value: n for t, n in sc.theme_counts.items()},
                "station": None if st is None else {
                    "station_idx": st.station_idx, "name": st.station_name,
                    "latitude": st.latitude, "longitude": st.longitude,
                    "province": getattr(st.region, "value", st.region) if st.region else None,
                },
            })
        out.append({"themes": [t.value for t in themes], "profiles": rows})
        print(f"  {[t.value for t in themes]}: 후보 권역 {len(rows)}")
    DEST_SNAPSHOT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"저장: {DEST_SNAPSHOT}")


def _profiles(rows: list[dict]) -> list[destination.AreaProfile]:
    from types import SimpleNamespace

    out = []
    for r in rows:
        st = r["station"]
        # 서비스 Phase A(recommend_service._recommend_auto_dest)와 같은 거름 — 역이 없거나 권역 중심이
        # 역에서 MAX_STATION_GAP_KM 넘게 떨어진 후보는 뺀다. TourAPI 좌표 오류로 중심이 남중국해
        # (19.69, 117.99)에 찍힌 1건짜리 권역이 스냅샷에 있다 — 운영에선 이 거름에 걸려 안 나온다.
        if st is None or routing.haversine(*r["centroid"], st["latitude"], st["longitude"]) > destination.MAX_STATION_GAP_KM:
            continue
        out.append(destination.AreaProfile(
            area_code=r["area_code"], centroid=tuple(r["centroid"]),
            theme_counts={Theme(t): n for t, n in r["theme_counts"].items()}, total=r["total"],
            station=SimpleNamespace(**st) if st else None, province=(st or {}).get("province"),
        ))
    return out


def _dedup_station(profiles):
    seen, out = set(), []
    for p in profiles:
        if p.station.station_idx not in seen:
            seen.add(p.station.station_idx)
            out.append(p)
    return out


def _dest_methods(rows, themes, origin, nights):
    """방식별 top-3 도착지. 셋 다 같은 후보·같은 역 dedup 을 거친다."""
    party = Party()
    ours = _dedup_station(destination.rank_and_diversify(_profiles(rows), themes, party, origin, nights, top_k=5))[:3]
    # 점수순: 같은 점수식, 다양성 필터 없음 — top_k 를 크게 줘 전체 순위를 받고 앞에서 3개
    ranked = destination.rank_and_diversify(_profiles(rows), themes, party, origin, nights, top_k=10 ** 6)
    ranked.sort(key=lambda p: p.score, reverse=True)
    by_score = _dedup_station(ranked)[:3]
    # 관광지 수순: 테마·거리 무시
    by_total = _dedup_station(sorted((p for p in _profiles(rows) if p.station), key=lambda p: p.total, reverse=True))[:3]
    return {"Trailer (점수화 → 권역 다양성)": ours, "베이스라인: 점수순(다양성 필터 없음)": by_score, "베이스라인: 관광지 수순": by_total}


def evaluate_dest() -> list[str]:
    if not DEST_SNAPSHOT.exists():
        return [f"(도착지 스냅샷 없음 — --snapshot-dest 로 만들면 지역 다양성 표가 추가된다: {DEST_SNAPSHOT})"]
    data = json.loads(DEST_SNAPSHOT.read_text(encoding="utf-8"))
    agg: dict[str, dict] = {}
    n_scen = 0
    for sc in data:
        themes = [Theme(t) for t in sc["themes"]]
        for origin_name, origin in _ORIGINS.items():
            for nights in _NIGHTS:
                n_scen += 1
                for name, top in _dest_methods(sc["profiles"], themes, origin, nights).items():
                    a = agg.setdefault(name, {"spread": [], "provs": set(), "fit": []})
                    provs = [p.province for p in top]
                    a["spread"].append(len(set(provs)) / len(provs) if provs else 0)
                    a["provs"].update(provs)
                    a["fit"] += [destination._theme_fit(destination._shares(p.theme_counts, p.total), themes) for p in top]
    lines = [
        "",
        f"### 도착지 자동 선택 — 지역 다양성 (출발역 {len(_ORIGINS)} × 테마조합 {len(data)} × 일정 {len(_NIGHTS)}종 = {n_scen}건, top-3)",
        "",
        "| 방식 | 권역 분산율 | 권역 커버 | 테마 적합도(평균) |",
        "|---|---|---|---|",
    ]
    for name, a in agg.items():
        lines.append(f"| {name} | {_pct(a['spread'])} | {len(a['provs'])}/{_PROVINCES} | {_avg(a['fit'])} |")
    lines += [
        "",
        "- 권역 분산율: top-3 도착지가 서로 다른 관리 본부인 비율 (100% = 셋이 전부 다른 권역)",
        "- 권역 커버: 전체 시나리오의 top-3 에 한 번이라도 뽑힌 본부 수 (8개 본부 중)",
        "- 테마 적합도: 선택 테마들이 그 권역에 고루 분포할수록 1 (destination._theme_fit)",
        "- Phase A(시도 분포 기반 거친 선별)까지만 잰 값. 서비스는 이어서 후보 역 주변을 실측해 재점수한다(Phase B)",
    ]
    return lines


# --------------------------------------------------------------------------- #
# 집계 + 표
# --------------------------------------------------------------------------- #
def _pct(xs):
    xs = [x for x in xs if x is not None]
    return f"{statistics.mean(xs) * 100:.1f}%" if xs else "-"


def _avg(xs, fmt="{:.2f}"):
    return fmt.format(statistics.mean(xs)) if xs else "-"


def evaluate(md_path: Path | None) -> None:
    data = _load()
    methods = {"Trailer (cluster→route→schedule)": _ours,
               "베이스라인: 인기순": _baseline_popular,
               "베이스라인: 가까운순": _baseline_nearest}
    agg = {m: {"hours": [], "window": [], "cat": [], "theme": [], "cov": [], "km": [], "overlap": [], "ms": [], "n": []} for m in methods}

    for sc in data:
        origin = _STATIONS[sc["station"]]
        themes = [Theme(t) for t in sc["themes"]]
        for nights in _NIGHTS:
            k = nights + 1
            criteria = _criteria(themes, nights)
            for name, fn in methods.items():
                res = fn(sc["places"], criteria, k, origin)
                if fn is _ours:
                    res, ms = res
                    agg[name]["ms"].append(ms)
                if not res:
                    continue
                a = agg[name]
                a["hours"] += [r["hours_ok"] for r in res]
                a["window"] += [r["window_ok"] for r in res]
                a["cat"] += [r["cat_div"] for r in res]
                a["theme"] += [r["theme_div"] for r in res]
                a["cov"] += [r["theme_cov"] for r in res if not sc.get("themes_relaxed")]
                a["km"] += [r["km"] / k for r in res]
                a["n"] += [r["n"] for r in res]
                ids = [r["ids"] for r in res]
                a["overlap"].append(sum(len(ids[i] & ids[j]) for i in range(len(ids)) for j in range(i + 1, len(ids))))

    lines = [
        f"시나리오 {len(data)}개(역 {len({sc['station'] for sc in data})} × 테마조합 {len(_THEME_SETS)}) × 일정 {len(_NIGHTS)}종 × 코스 3개, 출발 {_GO_DATE}",
        "",
        "| 방식 | 운영시간 충족률 | 시간창 준수율 | 선택 테마 커버리지 | 테마 수/코스 | 유형 다양성 | 하루 이동거리(km) | 코스 간 중복 | 방문지/코스 | 엔진 시간 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, a in agg.items():
        ms = f"{statistics.mean(a['ms']):.2f} ms" if a["ms"] else "-"
        lines.append(
            f"| {name} | {_pct(a['hours'])} | {_pct(a['window'])} | {_pct(a['cov'])} | {_avg(a['theme'], '{:.1f}')} | {_avg(a['cat'])} "
            f"| {_avg(a['km'], '{:.1f}')} | {_avg(a['overlap'], '{:.1f}')} | {_avg(a['n'], '{:.1f}')} | {ms} |"
        )
    lines += [
        "",
        "- 운영시간 충족률: 운영시간이 알려진 방문지 중 오픈~마감 안·휴무일 아님 비율. 베이스라인은 운영시간을 안 보므로 낮게 나오는 게 정상",
        "- 시간창 준수율: 하루 일정이 09~21시 안에 끝난 날의 비율 (체류 2h·이동 평속 30km/h 기준)",
        "- 선택 테마 커버리지: 사용자가 고른 테마 중 코스에 한 곳이라도 들어간 테마 비율 (테마 완화된 시나리오 제외)",
        "- 유형 다양성: 서로 다른 TourAPI 콘텐츠 유형(관광지·문화시설·음식점…) 수 ÷ 방문지 수. 후보 자체가 '관광지(12)' 한 유형에 몰려 있어 낮게 나온다 — 해변·산·사찰이 전부 12",
        "- 코스 간 중복: A/B/C 방문지 교집합 크기 합. 셋 다 인터리브 버킷이라 기본 0 이지만, Trailer 는 중간 날이 "
        "식당만일 때 공용 관광지 풀에서 보충하므로(pipeline.attraction_pool) 0 보다 클 수 있다",
    ]
    lines += evaluate_dest()
    text = "\n".join(lines)
    print(text)
    if md_path:
        md_path.write_text(text + "\n", encoding="utf-8")
        print(f"\n저장: {md_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", action="store_true", help="TourAPI 를 호출해 후보 스냅샷을 새로 만든다")
    ap.add_argument("--sparse-only", action="store_true", help="--snapshot 시 작업셋이 덜 찬 시나리오만 다시 받는다(쿼터 절약)")
    ap.add_argument("--snapshot-dest", action="store_true", help="도착지 자동 선택용 시도 스캔 스냅샷을 만든다(DB 필요)")
    ap.add_argument("--md", type=Path, help="결과 표를 이 파일에 저장")
    args = ap.parse_args()
    if args.snapshot:
        snapshot(args.sparse_only)
    elif args.snapshot_dest:
        snapshot_dest()
    else:
        evaluate(args.md)


if __name__ == "__main__":
    main()
