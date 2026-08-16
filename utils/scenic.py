import math
from functools import lru_cache

from config import Config

# 호수/저수지/수역, 강/하천, 산봉우리, 해변/절벽만 포함 (자연 지형 카테고리)
SCENIC_NATURAL_CATEGORIES = {"water", "waterway", "peak", "natural_view"}

# 풍경 알림에 실어 보내는 카테고리별 일러스트 (GCS 공개 버킷의 객체 경로).
# 아직 카테고리별 그림이 없어 네 카테고리 모두 같은 파일을 가리킨다 — 그림이 나오면 여기 경로만 갈아끼우면 되고, 앱 재배포는 필요 없다.
# 서버는 재배포(재시작)해야 반영된다. 경로가 코드 상수인 데다 scenery_image_url이 결과를 캐시하기 때문이다.
SCENERY_IMAGE_OBJECTS = {
    "water": "scenic/notice/default.png",
    "waterway": "scenic/notice/default.png",
    "peak": "scenic/notice/default.png",
    "natural_view": "scenic/notice/default.png",
}
_SCENERY_IMAGE_FALLBACK = "scenic/notice/default.png"

# 창밖에서 실제로 보이는 가시 범위(m)와 진행 방향 허용 각도(도).
# 거리 컷오프로 멀리 있는 스팟을, heading ±허용각으로 이미 지나간(뒤편) 스팟을 거른다.
VISIBLE_RADIUS_M = 1500
HEADING_TOLERANCE_DEG = 100

@lru_cache(maxsize=8)
def scenery_image_url(category: str | None) -> str | None:
    """카테고리에 맞는 풍경 알림 일러스트의 공개 URL. 버킷 설정이 없으면 None.

    utils.gcs 를 쓰지 않고 URL을 직접 조립한다 — gcs._bucket()은 자격증명이 없으면
    예외를 올리는데, 이 함수는 조회 요청(GET /api/scenic-spots/nearby) 경로에서 불려서
    그림 하나 때문에 조회가 깨지면 안 된다. 버킷은 공개 읽기라 URL 형식이 고정이다.

    카테고리별로 캐시하고 무효화 경로는 두지 않았다 — 결과가 프로세스 수명 동안 고정이라
    무효화할 일이 없어서다(경로는 코드 상수, 버킷명은 import 시점에 읽은 ini 값). 둘 중
    무엇을 바꾸든 서버를 재시작해야 반영된다.
    """
    bucket = Config.read("gcs", "bucket_name")
    if not bucket:
        return None
    obj = SCENERY_IMAGE_OBJECTS.get(category or "", _SCENERY_IMAGE_FALLBACK)
    return f"https://storage.googleapis.com/{bucket}/{obj}"


# 좌표 거리 계산
def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius_m = 6371000.0
    lat1_r, lat2_r = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)

    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlng / 2) ** 2
    )
    c = 2 * math.asin(math.sqrt(a))
    return radius_m * c

def bearing_deg(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """점1→점2 방위각(북=0, 시계방향) [0, 360)."""
    lat1_r, lat2_r = math.radians(lat1), math.radians(lat2)
    dlng = math.radians(lng2 - lng1)
    y = math.sin(dlng) * math.cos(lat2_r)
    x = math.cos(lat1_r) * math.sin(lat2_r) - math.sin(lat1_r) * math.cos(lat2_r) * math.cos(dlng)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def angle_diff_deg(a: float, b: float) -> float:
    """두 방위각의 최소 차 [0, 180]."""
    d = abs(a - b) % 360
    return d if d <= 180 else 360 - d


def project_on_segment(
    a_lat: float, a_lng: float, b_lat: float, b_lng: float, p_lat: float, p_lng: float,
) -> tuple[float, float]:
    """점 P를 구간 A→B에 투영해 (진행 비율 t, 구간에서 떨어진 거리 m)를 반환한다.

    t는 [0, 1]로 자른다 — 구간 밖으로 나가는 투영은 양 끝점에 붙인다. 그래서 t가 0이나
    1이면 '구간 바깥'이라는 뜻이기도 하다.

    좌표 투영 대신 세 변의 길이(haversine)만으로 계산한다(제2코사인법칙):
    구간 길이 c에 대해 A에서 투영점까지의 거리는 (|AP|² - |BP|² + c²) / 2c 이고,
    떨어진 거리는 피타고라스로 얻는다. 위경도를 평면에 펴는 과정이 없어 고위도 왜곡이
    없고, 역 간 수십 km 규모에서 오차는 무시할 만하다.

    풍경 알림 시각표가 두 곳에 쓴다 — 스팟이 구간의 몇 %쯤에 있는지(통과 시각 보간),
    그리고 사용자 GPS가 지금 구간의 몇 %쯤인지(지연 보정).
    """
    c = haversine_m(a_lat, a_lng, b_lat, b_lng)
    if c == 0:
        return 0.0, haversine_m(a_lat, a_lng, p_lat, p_lng)

    ap = haversine_m(a_lat, a_lng, p_lat, p_lng)
    bp = haversine_m(b_lat, b_lng, p_lat, p_lng)
    along = (ap ** 2 - bp ** 2 + c ** 2) / (2 * c)
    # 자르기 전의 along으로 수직거리를 구한다 — 구간 밖 점도 '선분에서 얼마나 벗어났나'가
    # 아니라 '직선에서 얼마나 벗어났나'로 재야 대표 스팟 순위가 뒤집히지 않는다.
    off = math.sqrt(max(0.0, ap ** 2 - along ** 2))
    return min(1.0, max(0.0, along / c)), off


if __name__ == "__main__":
    # 자가검증: 방위각/각도차 (서울 기준 정북/정동, wraparound)
    assert abs(bearing_deg(37.0, 127.0, 38.0, 127.0) - 0) < 0.5    # 정북
    assert abs(bearing_deg(37.0, 127.0, 37.0, 128.0) - 90) < 0.5   # 정동
    assert abs(bearing_deg(37.0, 127.0, 36.0, 127.0) - 180) < 0.5  # 정남
    assert angle_diff_deg(350, 10) == 20
    assert angle_diff_deg(10, 350) == 20
    assert angle_diff_deg(0, 180) == 180

    # 자가검증: 구간 투영 (중점·양 끝·구간 밖·수직 이탈)
    t, off = project_on_segment(37.0, 127.0, 37.0, 128.0, 37.0, 127.5)
    assert abs(t - 0.5) < 0.01 and off < 1000          # 중점
    t, off = project_on_segment(37.0, 127.0, 37.0, 128.0, 37.0, 127.0)
    assert t == 0.0 and off < 1                        # 시작점
    t, _ = project_on_segment(37.0, 127.0, 37.0, 128.0, 37.0, 129.0)
    assert t == 1.0                                    # 구간 밖 → 끝점에 붙는다
    t, off = project_on_segment(37.0, 127.0, 37.0, 128.0, 37.1, 127.5)
    assert abs(t - 0.5) < 0.01 and 10000 < off < 12000  # 위도 0.1° ≈ 11km 벗어남
    print("scenic self-check OK")
