"""AI 여행 코스 추천 엔진 (순수 함수, DB/FastAPI 비의존).

속도 1순위 / 최적성 2순위 원칙의 cluster-first, route-second 휴리스틱:
  scoring(가중 코사인 유사도) → clustering(k-means, k=일수)
  → routing(Nearest Neighbor + 2-opt) → 순환 복귀 → constraints(예산/경유지/내일로).

후보지 15~25개 규모에서 전체 파이프라인 1ms 미만, 외부 의존성 0(순수 파이썬).
기차 구간은 여기서 다루지 않고 services 레이어가 기존 route_service/train_api로 처리한다.

1단계 현재: 타입/시그니처 골격만. 알고리즘 본문은 후속 단계에서 구현한다.
"""
