import math

# 호수/저수지/수역, 강/하천, 산봉우리, 해변/절벽만 포함 (자연 지형 카테고리)
SCENIC_NATURAL_CATEGORIES = {"water", "waterway", "peak", "natural_view"}

# 창밖에서 실제로 보이는 가시 범위(m)와 진행 방향 허용 각도(도).
# 거리 컷오프로 멀리 있는 스팟을, heading ±허용각으로 이미 지나간(뒤편) 스팟을 거른다.
VISIBLE_RADIUS_M = 1500
HEADING_TOLERANCE_DEG = 100

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


if __name__ == "__main__":
    # 자가검증: 방위각/각도차 (서울 기준 정북/정동, wraparound)
    assert abs(bearing_deg(37.0, 127.0, 38.0, 127.0) - 0) < 0.5    # 정북
    assert abs(bearing_deg(37.0, 127.0, 37.0, 128.0) - 90) < 0.5   # 정동
    assert abs(bearing_deg(37.0, 127.0, 36.0, 127.0) - 180) < 0.5  # 정남
    assert angle_diff_deg(350, 10) == 20
    assert angle_diff_deg(10, 350) == 20
    assert angle_diff_deg(0, 180) == 180
    print("scenic self-check OK")
