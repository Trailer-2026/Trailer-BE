"""TourAPI 분류(contentTypeId·cat1/2/3) → Theme(enum) 매핑. (cat: category)

categoryCode2 트리(권위 코드표)를 근거로 cat3/cat2 코드를 명시 매핑한다.
우선순위: cat3 특수 매핑 > cat2 기본값 > contentTypeId 보강. 다중 태그 허용.
레포츠(A03/contentType 28)는 8개 테마에 잘 안 맞아 시딩 대상에서 제외(seed_places).
"""
from core.enums import Theme

# cat3 코드별 특수 매핑 (cat2 기본값보다 우선)
_CAT3 = {
    # A0101 자연관광지 중 해안 계열 → OCEAN
    "A01011100": [Theme.OCEAN],  # 해안절경
    "A01011200": [Theme.OCEAN],  # 해수욕장
    "A01011300": [Theme.OCEAN],  # 섬
    "A01011400": [Theme.OCEAN],  # 항구/포구
    "A01011600": [Theme.OCEAN],  # 등대
    # A0101 자연+힐링
    "A01010600": [Theme.NATURE, Theme.HEALING],  # 자연휴양림
    "A01011000": [Theme.NATURE, Theme.HEALING],  # 약수터
    # A0202 휴양관광지 세분
    "A02020200": [Theme.CITY],        # 관광단지
    "A02020300": [Theme.HEALING],     # 온천/욕장/스파
    "A02020400": [Theme.HEALING],     # 이색찜질방
    "A02020500": [Theme.HEALING],     # 헬스투어
    "A02020600": [Theme.THEME_PARK],  # 테마공원
    "A02020700": [Theme.NATURE],      # 공원
    "A02020800": [Theme.OCEAN],       # 유람선/잠수함관광
    # A0203 체험관광지
    "A02030100": [Theme.NATURE],      # 농.산.어촌 체험
    "A02030200": [Theme.CULTURE],     # 전통체험
    "A02030300": [Theme.HISTORY],     # 산사체험
    "A02030400": [Theme.CITY],        # 이색체험
    "A02030600": [Theme.CITY],        # 이색거리
    # A0204 산업관광지
    "A02040600": [Theme.FOOD],        # 식음료
}

# cat2 코드 기본값 (cat3 미해당 시)
_CAT2 = {
    "A0101": [Theme.NATURE],   # 자연관광지
    "A0102": [Theme.NATURE],   # 관광자원(기암괴석 등)
    "A0201": [Theme.HISTORY],  # 역사관광지(고궁·성·유적·사찰·종교성지)
    "A0204": [Theme.CULTURE],  # 산업관광지(식음료는 _CAT3 우선)
    "A0205": [Theme.CITY],     # 건축/조형물(전망대·다리·유명건물)
    "A0206": [Theme.CULTURE],  # 문화시설(박물관·미술관·공연장)
    "A0207": [Theme.CULTURE],  # 축제
    "A0208": [Theme.CULTURE],  # 공연/행사
}

# contentTypeId 보강 (cat 정보로 못 잡았을 때)
_CT = {
    "14": [Theme.CULTURE],  # 문화시설
    "15": [Theme.CULTURE],  # 축제공연행사
    "38": [Theme.CITY],     # 쇼핑(시장·상가·거리) → 도시 탐방
    "39": [Theme.FOOD],     # 음식점
}

# 체류시간(분) 휴리스틱 — TourAPI는 체류시간을 주지 않으므로 콘텐츠 유형으로 추정
_STAY_BY_TYPE = {12: 90, 14: 60, 15: 120, 38: 60, 39: 60}
_STAY_DEFAULT = 60


def themes_for(
    content_type_id: int | str | None,
    cat1: str | None = None,
    cat2: str | None = None,
    cat3: str | None = None,
) -> list[Theme]:
    """관광 항목 1건의 테마 태그 목록(0개일 수 있음 → 호출측에서 스킵)."""
    out: set[Theme] = set()
    if cat3 and cat3 in _CAT3:
        out.update(_CAT3[cat3])
    elif cat2 and cat2 in _CAT2:
        out.update(_CAT2[cat2])

    ct = str(content_type_id or "")
    if not out and ct in _CT:
        out.update(_CT[ct])
    if ct == "39":  # 음식점은 항상 FOOD 보강
        out.add(Theme.FOOD)

    return sorted(out, key=lambda t: t.value)


def stay_minutes(content_type_id: int | str | None, themes: list[Theme]) -> int:
    """평균 체류시간(분) 추정. 테마파크면 길게."""
    if Theme.THEME_PARK in themes:
        return 180
    try:
        return _STAY_BY_TYPE.get(int(content_type_id), _STAY_DEFAULT)
    except (TypeError, ValueError):
        return _STAY_DEFAULT
