"""AI 여행 코스 추천 엔진 (순수 함수, DB/FastAPI 비의존).

속도 1순위 / 최적성 2순위 원칙의 cluster-first, route-second 휴리스틱:
  scoring(가중 코사인 유사도) → clustering(k-means, k=일수)
  → routing(Nearest Neighbor + 2-opt) → 순환 복귀 → constraints(예산/경유지/내일로).

후보지 수십 개 규모에서 전체 파이프라인 1ms 미만(실측 ~0.25ms), 외부 의존성 0(순수 파이썬).
기차 구간은 여기서 다루지 않고 services 레이어가 기존 route_service/train_api로 처리한다.

진입점은 pipeline.build_courses(scored, criteria, k, origin) → 코스 후보(A/B/C) 리스트.
도착지 앵커링·숙소 배정·기차 결합은 services/recommend_service에서 이 엔진을 감싸 수행한다.
모든 단계 구현 완료(scoring·clustering·routing·constraints·pipeline).
"""
