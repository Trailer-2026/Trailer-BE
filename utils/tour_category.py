"""TourAPI 분류(contentTypeId·cat1/2/3) → Theme(enum) 매핑. (cat: category)

categoryCode2 트리(권위 코드표)를 근거로 cat3/cat2 코드를 명시 매핑한다.
우선순위: cat3 특수 매핑 > cat2 기본값 > contentTypeId 보강. 다중 태그 허용.
레포츠(A03/contentType 28)는 8개 테마에 잘 안 맞아 수집 대상에서 제외한다.
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

# 새 분류체계(lclsSystm1/2/3, 2025 개편) → Theme. lclsSystmCode2 코드표(246행) 기준.
# 개편 뒤 등록·갱신된 항목은 cat1/2/3 이 비어 있고 이 코드만 있다(해운대해수욕장·경복궁·불국사 등
# 유명 관광지가 대부분 그렇다). 옛 cat 이 있으면 그쪽이 우선이고, 없을 때 여기로 잡는다.
# 우선순위는 cat 과 같다: 세분(3) > 중분류(2) > 대분류(1). 레저스포츠(LS)는 옛 정책대로 제외.
_LCLS3 = {
    "NA010500": [Theme.NATURE, Theme.HEALING],  # 약수터
    "NA040600": [Theme.NATURE, Theme.HEALING],  # 자연휴양림
    "VE010800": [Theme.OCEAN],                   # 등대
    "VE020400": [Theme.THEME_PARK, Theme.OCEAN], # 수족관/아쿠아리움
    "VE040300": [Theme.NATURE, Theme.HEALING],  # 둘레길
    "EX060800": [Theme.FOOD],                    # 화장품/주류/먹거리 산업관광
    "EX070100": [Theme.OCEAN],                   # 유람선/잠수함관광
}
_LCLS2 = {
    "NA01": [Theme.NATURE],                     # 자연경관(산)
    "NA02": [Theme.NATURE],                     # 자연경관(하천·해양) — 해양 세분은 아래 _LCLS2_OCEAN
    "NA03": [Theme.NATURE],                     # 자연생태
    "NA04": [Theme.NATURE],                     # 자연공원
    "NA05": [Theme.NATURE],                     # 기타자연관광
    "VE01": [Theme.CITY],                       # 랜드마크(건물·타워·다리)
    "VE02": [Theme.THEME_PARK],                 # 테마공원
    "VE03": [Theme.NATURE],                     # 도시공원
    "VE04": [Theme.CITY],                       # 골목길·문화거리·마을관광지
    "VE05": [Theme.CITY],                       # 관광단지·리조트
    "VE06": [Theme.CULTURE],                    # 공연시설
    "VE07": [Theme.CULTURE],                    # 전시시설(박물관·미술관)
    "VE09": [Theme.CULTURE],                    # 교육시설(문화원)
    "VE12": [Theme.CULTURE],                    # 기타문화관광지
    "EX01": [Theme.CULTURE],                    # 전통체험
    "EX02": [Theme.CULTURE],                    # 공예체험
    "EX03": [Theme.NATURE],                     # 농·산·어촌 체험
    "EX04": [Theme.HISTORY],                    # 산사체험
    "EX05": [Theme.HEALING],                    # 웰니스(온천·찜질·명상)
    "EX06": [Theme.CULTURE],                    # 산업관광
}
# NA02 중 바다 계열 세분 — 강·호수(NATURE)와 갈라야 해서 세분 코드로 OCEAN 을 준다.
_LCLS2_OCEAN = {"NA020500", "NA020700", "NA020800", "NA020900"}  # 섬·항구/포구·해안절경·해변
_LCLS1 = {
    "HS": [Theme.HISTORY],   # 역사관광(유적·유물·종교성지·안보)
    "EV": [Theme.CULTURE],   # 축제/공연/행사
    "SH": [Theme.CITY],      # 쇼핑
    "FD": [Theme.FOOD],      # 음식
}


def _themes_from_lcls(lcls3: str | None) -> set[Theme]:
    if not lcls3:
        return set()
    if lcls3 in _LCLS2_OCEAN:
        return {Theme.OCEAN}
    if lcls3 in _LCLS3:
        return set(_LCLS3[lcls3])
    if lcls3[:4] in _LCLS2:
        return set(_LCLS2[lcls3[:4]])
    if lcls3[:2] in _LCLS1:
        return set(_LCLS1[lcls3[:2]])
    return set()


# contentTypeId 보강 (cat 정보로 못 잡았을 때)
_CT = {
    "14": [Theme.CULTURE],  # 문화시설
    "15": [Theme.CULTURE],  # 축제공연행사
    "38": [Theme.CITY],     # 쇼핑(시장·상가·거리) → 도시 탐방
    "39": [Theme.FOOD],     # 음식점
}


def themes_for(
    content_type_id: int | str | None,
    cat1: str | None = None,
    cat2: str | None = None,
    cat3: str | None = None,
    lcls3: str | None = None,
) -> list[Theme]:
    """관광 항목 1건의 테마 태그 목록(0개일 수 있음 → 호출측에서 스킵).

    우선순위: cat3(세분) > cat2(기본값) > 새 분류 lclsSystm3 > contentType(보강). cat3가
    잡히면 cat2는 보지 않는다(elif) — 더 구체적인 코드가 항상 이긴다. 새 분류는 옛 cat 이
    비어 있는 항목(2025 개편 뒤 등록·갱신분)을 위한 것이다. contentType은 아무것도
    못 잡았을 때(`not out`)만 쓰는 최후 보루다.
    """
    out: set[Theme] = set()
    if cat3 and cat3 in _CAT3:
        out.update(_CAT3[cat3])
    elif cat2 and cat2 in _CAT2:
        out.update(_CAT2[cat2])
    if not out:
        out.update(_themes_from_lcls(lcls3))

    ct = str(content_type_id or "")
    if not out and ct in _CT:
        out.update(_CT[ct])
    if ct == "39":
        out.add(Theme.FOOD)

    return sorted(out, key=lambda t: t.value)


# 음식점(contentTypeId=39) cat3 → 카드에 표기할 한글 라벨. categoryCode2(A05/A0502) 코드표 그대로.
FOOD_CATEGORY = {
    "A05020100": "한식",
    "A05020200": "서양식",
    "A05020300": "일식",
    "A05020400": "중식",
    "A05020700": "이색음식점",
    "A05020900": "카페/전통찻집",
    "A05021000": "클럽",
}


# 숙박(contentTypeId=32) cat3 → 유형 라벨
LODGING_TYPE = {
    "B02010100": "관광호텔", "B02010500": "콘도미니엄", "B02010600": "유스호스텔",
    "B02010700": "펜션", "B02010900": "모텔", "B02011000": "민박",
    "B02011100": "게스트하우스", "B02011200": "홈스테이",
    "B02011300": "서비스드레지던스", "B02011600": "한옥",
}
