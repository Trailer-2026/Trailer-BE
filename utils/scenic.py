import math

# 호수/저수지/수역, 강/하천, 산봉우리, 해변/절벽만 포함 (자연 지형 카테고리)
SCENIC_NATURAL_CATEGORIES = {"water", "waterway", "peak", "natural_view"}

DEFAULT_RADIUS_M = 1500

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
