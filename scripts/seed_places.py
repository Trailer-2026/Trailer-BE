"""한국관광공사 TourAPI(15101578) → place 마스터 시딩.

수집 정책: 시도(area) × contentTypeId 별 상위 N개(arrange=O, 대표이미지 보유 → 좌표·이미지
품질 프록시)만 받아 규모를 제한한다. cat3 한글명으로 Theme를 매핑(utils/tour_category).
content_id(TourAPI contentid)로 idempotent upsert.

사전 준비: config [tourapi] service_key (data.go.kr Decoding 키, 15101578 활용신청 승인).

사용:
  python scripts/seed_places.py --probe          # 1개 지역·소량, DB 미기록(키·응답 검증)
  python scripts/seed_places.py --cap 100        # 시도×유형별 상위 100개 적재
  python scripts/seed_places.py --cap 100 --areas 1,6,32,39
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.dialects.postgresql import insert as pg_insert

from databases.database import SessionLocal
from databases.models.lodging import Lodging
from databases.models.place import Place
from utils import tour_api, tour_category

# 숙박(contentTypeId=32) cat3 → 유형 라벨
LODGING_TYPE = {
    "B02010100": "관광호텔", "B02010500": "콘도미니엄", "B02010600": "유스호스텔",
    "B02010700": "펜션", "B02010900": "모텔", "B02011000": "민박",
    "B02011100": "게스트하우스", "B02011200": "홈스테이",
    "B02011300": "서비스드레지던스", "B02011600": "한옥",
}

# 수집 대상 콘텐츠 유형: 관광지·문화시설·축제공연·쇼핑·음식점
# (레포츠 28은 8개 테마에 잘 안 맞아 제외 — THEME_PARK/OCEAN은 관광지(12) cat3로 커버)
CONTENT_TYPES = [12, 14, 15, 38, 39]

# 희소 테마(OCEAN·HEALING·THEME_PARK) 보강용 cat3 — 전 지역에서 타깃 수집(--boost)
BOOST_CAT3 = [
    "A01011100", "A01011200", "A01011300", "A01011400", "A01011600", "A02020800",  # OCEAN
    "A02020300", "A02020400", "A02020500", "A01010600", "A01011000",               # HEALING
    "A02020600",                                                                    # THEME_PARK
]


def build_cat3_names() -> dict[str, str]:
    """categoryCode2 대→중→소 순회로 {코드: 한글명} 사전 구축(주로 cat3명)."""
    names: dict[str, str] = {}
    for c1 in tour_api.category_code():
        c1code = c1["code"]
        names[c1code] = c1["name"]
        for c2 in tour_api.category_code(cat1=c1code):
            c2code = c2["code"]
            names[c2code] = c2["name"]
            for c3 in tour_api.category_code(cat1=c1code, cat2=c2code):
                names[c3["code"]] = c3["name"]
    return names


def to_place_row(item: dict) -> dict | None:
    """TourAPI item → place insert dict. 좌표·테마 없으면 None(스킵)."""
    try:
        lng = float(item.get("mapx") or 0)
        lat = float(item.get("mapy") or 0)
    except (TypeError, ValueError):
        return None
    if not lat or not lng:
        return None

    ct = item.get("contenttypeid")
    themes = tour_category.themes_for(ct, item.get("cat1"), item.get("cat2"), item.get("cat3"))
    if not themes:
        return None

    return {
        "content_id": str(item.get("contentid")),
        "name": (item.get("title") or "")[:255],
        "region": (item.get("addr1") or None) and item["addr1"][:100],
        "lat": lat,
        "lng": lng,
        "themes": themes,
        "avg_stay_min": tour_category.stay_minutes(ct, themes),
        "image_url": (item.get("firstimage") or None) and item["firstimage"][:500],
    }


def upsert(db, rows: list[dict]) -> int:
    """content_id 충돌 시 갱신. 영향 행 수 반환."""
    if not rows:
        return 0
    stmt = pg_insert(Place).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["content_id"],
        set_={
            "name": stmt.excluded.name,
            "region": stmt.excluded.region,
            "lat": stmt.excluded.lat,
            "lng": stmt.excluded.lng,
            "themes": stmt.excluded.themes,
            "avg_stay_min": stmt.excluded.avg_stay_min,
            "image_url": stmt.excluded.image_url,
        },
    )
    db.execute(stmt)
    return len(rows)


def to_lodging_row(item: dict) -> dict | None:
    """TourAPI 숙박 item → lodging insert dict. 좌표 없으면 None."""
    try:
        lng = float(item.get("mapx") or 0)
        lat = float(item.get("mapy") or 0)
    except (TypeError, ValueError):
        return None
    if not lat or not lng:
        return None
    return {
        "content_id": str(item.get("contentid")),
        "name": (item.get("title") or "")[:255],
        "lodging_type": LODGING_TYPE.get(item.get("cat3") or ""),
        "region": (item.get("addr1") or None) and item["addr1"][:100],
        "lat": lat,
        "lng": lng,
        "tel": (item.get("tel") or None) and item["tel"][:60],
        "image_url": (item.get("firstimage") or None) and item["firstimage"][:500],
    }


def upsert_lodging(db, rows: list[dict]) -> int:
    if not rows:
        return 0
    stmt = pg_insert(Lodging).values(rows)
    cols = ["name", "lodging_type", "region", "lat", "lng", "tel", "image_url"]
    stmt = stmt.on_conflict_do_update(
        index_elements=["content_id"],
        set_={c: getattr(stmt.excluded, c) for c in cols},
    )
    db.execute(stmt)
    return len(rows)


def run_lodging(cap: int, areas: list[int]) -> None:
    print(f"[lodging] 숙박(32) 시도 {len(areas)}개, cap={cap}")
    db = SessionLocal()
    affected = 0
    try:
        for area in areas:
            items, total = tour_api.area_based_list(
                area_code=area, content_type_id=32, num_of_rows=cap, page_no=1
            )
            rows = {r["content_id"]: r for it in items if (r := to_lodging_row(it))}
            affected += upsert_lodging(db, list(rows.values()))
            print(f"  area={area}: {len(items)}건 → {len(rows)} upsert (total={total})")
        db.commit()
        print(f"[lodging] 완료. upsert 누계 {affected}건")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def run(cap: int, areas: list[int] | None, probe: bool, boost: bool = False, lodging: bool = False) -> None:
    if areas is None:
        areas = [int(a["code"]) for a in tour_api.area_code()]

    if lodging:
        run_lodging(cap, areas)
        return

    if boost:
        print(f"[boost] 희소 테마 cat3 {len(BOOST_CAT3)}종 × 시도 {len(areas)}, cap={cap}")
        db = SessionLocal()
        affected = 0
        try:
            for area in areas:
                for c3 in BOOST_CAT3:
                    items, total = tour_api.area_based_list(
                        area_code=area, cat1=c3[:3], cat2=c3[:5], cat3=c3,
                        num_of_rows=cap, page_no=1,
                    )
                    rows = {r["content_id"]: r for it in items if (r := to_place_row(it))}
                    affected += upsert(db, list(rows.values()))
            db.commit()
            print(f"[boost] 완료. upsert 누계 {affected}건")
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return

    if probe:
        cat3_names = build_cat3_names()  # 가독성용(매핑은 코드 기반이라 불필요)
        for ct in (12, 14, 15, 38, 39):
            items, total = tour_api.area_based_list(
                area_code=areas[0], content_type_id=ct, num_of_rows=5, page_no=1
            )
            print(f"[probe] area={areas[0]} type={ct} totalCount={total} sample={len(items)}건")
            for it in items:
                row = to_place_row(it)
                print("  -", it.get("title"), "/ cat3=", it.get("cat3"),
                      cat3_names.get(it.get("cat3") or "", "?"),
                      "->", [t.value for t in row["themes"]] if row else "SKIP")
        print("[probe] DB 미기록. 응답·매핑 확인용.")
        return

    print(f"[seed] 대상 시도 {areas}, 유형 {CONTENT_TYPES}, cap={cap}")
    db = SessionLocal()
    affected = 0
    try:
        for area in areas:
            for ct in CONTENT_TYPES:
                items, total = tour_api.area_based_list(
                    area_code=area, content_type_id=ct, num_of_rows=cap, page_no=1
                )
                rows = [r for it in items if (r := to_place_row(it))]
                # 같은 배치 내 content_id 중복 제거(ON CONFLICT 배치 제약)
                uniq = {r["content_id"]: r for r in rows}
                affected += upsert(db, list(uniq.values()))
                print(f"  area={area} type={ct}: {len(items)}건 → {len(uniq)} upsert (total={total})")
        db.commit()
        print(f"[3/3] 완료. upsert 누계 {affected}건")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap", type=int, default=100, help="시도×유형별 최대 수집 수")
    ap.add_argument("--areas", type=str, default=None, help="시도 코드 CSV (미지정=전체)")
    ap.add_argument("--probe", action="store_true", help="검증용 단발 호출(DB 미기록)")
    ap.add_argument("--boost", action="store_true", help="희소 테마(OCEAN·HEALING·THEME_PARK) cat3 타깃 보강")
    ap.add_argument("--lodging", action="store_true", help="숙박(contentTypeId=32) → lodging 테이블 시딩")
    args = ap.parse_args()
    areas = [int(x) for x in args.areas.split(",")] if args.areas else None
    run(cap=args.cap, areas=areas, probe=args.probe, boost=args.boost, lodging=args.lodging)


if __name__ == "__main__":
    main()
