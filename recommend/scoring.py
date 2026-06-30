"""1단계: 점수화 — 사용자 테마와 추천지 테마의 가중 코사인 유사도."""

import math

from core.enums import Theme
from recommend.types import ScoredPlace

# 테마 벡터 차원 순서 고정 (코사인 계산용)
_THEME_ORDER = list(Theme)


def cosine_weighted(
    place_vec: tuple[float, ...],
    pref_vec: tuple[float, ...],
    weights: tuple[float, ...],
) -> float:
    """가중 코사인 유사도. 세 벡터는 _THEME_ORDER 순서의 동일 길이. 반환 0.0~1.0."""
    num = sum(w * p * q for w, p, q in zip(weights, place_vec, pref_vec))
    da = math.sqrt(sum(w * p * p for w, p in zip(weights, place_vec)))
    db = math.sqrt(sum(w * q * q for w, q in zip(weights, pref_vec)))
    if da == 0.0 or db == 0.0:
        return 0.0
    return num / (da * db)


def _multi_hot(themes: list[Theme]) -> tuple[float, ...]:
    s = set(themes)
    return tuple(1.0 if t in s else 0.0 for t in _THEME_ORDER)


def score_places(
    places: list,                       # list[databases.models.place.Place]
    themes: list[Theme],
    weights: dict[Theme, float] | None = None,
) -> list[ScoredPlace]:
    """선택 테마와 각 추천지 테마를 매칭해 ScoredPlace 목록(score>0)을 점수 내림차순 반환.

    themes가 비면(테마 미선택) 전 추천지를 동일 점수 1.0으로 반환(지리 기반만으로 추천).
    """
    w_vec = tuple(
        (weights or {}).get(t, 1.0) for t in _THEME_ORDER
    )

    if not themes:
        return sorted(
            (_to_scored(p, 1.0) for p in places if _has_coords(p)),
            key=lambda sp: sp.score,
            reverse=True,
        )

    pref_vec = _multi_hot(themes)
    out: list[ScoredPlace] = []
    for p in places:
        if not _has_coords(p):
            continue
        score = cosine_weighted(_multi_hot(p.themes or []), pref_vec, w_vec)
        if score > 0.0:
            out.append(_to_scored(p, score))
    out.sort(key=lambda sp: sp.score, reverse=True)
    return out


def _has_coords(p) -> bool:
    return p.lat is not None and p.lng is not None


def _to_scored(p, score: float) -> ScoredPlace:
    return ScoredPlace(
        place_idx=p.place_idx,
        name=p.name,
        region=p.region,
        lat=p.lat,
        lng=p.lng,
        themes=list(p.themes or []),
        avg_stay_min=p.avg_stay_min,
        score=score,
        image_url=p.image_url,
    )
