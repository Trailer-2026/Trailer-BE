"""열차별 정차역(train_stop) 수동 적재 스크립트.

한국철도공사 열차운행정보(travelerTrainRunInfo2)에서 '최근 하루치' 정차 시퀀스를 받아
train_stop 테이블을 전량 교체한다. 실제 적재 로직은 services.train_stop_service.refresh가
담당(서버의 일일 자동 갱신 루프와 공유). 서버가 켜져 있으면 자동 갱신되므로 이 스크립트는
초기 적재·수동 재적재·특정 날짜 지정용이다.

실행:  python scripts/sync_train_stops.py [YYYYMMDD]
  - 날짜 미지정 시 어제(운행정보 보존기간이 '~1일 전'이라 어제가 가장 최신).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import train_stop_service  # noqa: E402


def main() -> None:
    ymd = sys.argv[1] if len(sys.argv) > 1 else None
    n = train_stop_service.refresh(ymd)
    print(f"[train_stop] 적재 완료: {n}행" if n else "[train_stop] 적재된 데이터 없음(날짜 확인)")


if __name__ == "__main__":
    main()
