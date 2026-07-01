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
    """두 테마 벡터가 얼마나 비슷한지 0.0~1.0으로 잰다(1에 가까울수록 취향 일치).

    세 벡터(place_vec·pref_vec·weights)는 모두 _THEME_ORDER와 같은 규칙으로 정렬된 같은 길이의 벡터 (수정 금지).

    보통의 코사인 유사도에 테마별 가중치 w를 얹은 것이다. 분자(내적)와 분모를
    계산하는 세 곳(da·db 포함)에 모두 같은 w를 곱하기 때문에 결과는 여전히 0~1로
    유지된다(한 곳에만 곱하면 값이 1을 넘어 깨진다). w를 키운 테마는 겹칠 때 더
    크게 쳐줘서 "이 테마를 더 중시"하도록 튜닝할 수 있다.

    여기서 place_vec·pref_vec는 테마가 있으면 1, 없으면 0인 다중핫 벡터라 식이
    간단해진다 → "겹치는 테마의 가중치 합 ÷ 각 벡터가 가진 테마 가중치의 크기".
    """
    num = sum(w * p * q for w, p, q in zip(weights, place_vec, pref_vec))
    da = math.sqrt(sum(w * p * p for w, p in zip(weights, place_vec)))
    db = math.sqrt(sum(w * q * q for w, q in zip(weights, pref_vec)))
    # 한쪽이라도 영벡터(테마 없음)면 유사도 정의 불가 → 0으로 처리(0나눗셈 방지 겸용)
    if da == 0.0 or db == 0.0:
        return 0.0
    return num / (da * db)


def _multi_hot(themes: list[Theme]) -> tuple[float, ...]:
    # 테마 목록 → _THEME_ORDER 차원에 맞춘 다중핫(0/1) 벡터. 순서 고정이 코사인 정렬의 전제.
    s = set(themes)
    return tuple(1.0 if t in s else 0.0 for t in _THEME_ORDER)


def score_places(
    places: list,                       # 추천지 객체 목록 (utils.tour_place.LivePlace)
    themes: list[Theme],
    weights: dict[Theme, float] | None = None,
) -> list[ScoredPlace]:
    """선택 테마와 각 추천지 테마를 매칭해 ScoredPlace 목록(score>0)을 점수 내림차순 반환.

    themes가 비면(테마 미선택) 전 추천지를 동일 점수 1.0으로 반환(지리 기반만으로 추천).
    """
    # 가중치 벡터도 _THEME_ORDER 순서로. 미지정 테마는 1.0(균등 가중).
    w_vec = tuple(
        (weights or {}).get(t, 1.0) for t in _THEME_ORDER
    )

    # 테마 미선택: 점수화 생략, 좌표만 있으면 모두 1.0으로 통과(이후 지리 클러스터링이 골라냄)
    if not themes:
        return sorted(
            (_to_scored(p, 1.0) for p in places if _has_coords(p)),
            key=lambda sp: sp.score,
            reverse=True,
        )

    pref_vec = _multi_hot(themes)
    out: list[ScoredPlace] = []
    for p in places:
        # 좌표 없는 곳은 클러스터링·라우팅 불가라 아예 제외
        if not _has_coords(p):
            continue
        score = cosine_weighted(_multi_hot(p.themes or []), pref_vec, w_vec)
        # score==0 = 선택 테마와 겹치는 게 하나도 없음 → 후보에서 버림
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
        score=score,
        image_url=p.image_url,
    )
