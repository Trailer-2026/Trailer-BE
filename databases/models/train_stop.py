from sqlalchemy import BigInteger, Column, Index, Integer, String

from databases.models.base import BaseModel


class TrainStop(BaseModel):
    """열차별 정차역 1건 (한국철도공사 열차운행정보 travelerTrainRunInfo2 기반).

    열차번호(trn_no)는 TAGO GetStrtpntAlocFndTrainInfo의 trainno와 동일 체계라 그대로 매칭한다.
    한 열차의 정차 시퀀스는 (trn_no, seq) 순서로 재구성한다. 역명(stn_nm)은 운행정보/ TAGO 모두
    '서울'·'부산'처럼 접미사 '역' 없는 동일 형식이라 RouteTrain.dep_station/arr_station과 바로 매칭된다.

    운행정보 API는 필터 조회를 지원하지 않고(과거~1일 전) 전량 페이징이라, scripts/sync_train_stops.py가
    최근 하루치를 받아 이 테이블에 전량 교체 적재한다. 정차 패턴은 열차번호별로 안정적이라 미래 여정에도
    그대로 적용한다. SRT(수서 출발, SR 운영)는 이 코레일 API에 없어 미수록 → 조회 시 정차수 null 폴백.
    """

    __tablename__ = "train_stop"
    __table_args__ = (
        Index("idx_train_stop_trn_no_seq", "trn_no", "seq"),
        {"comment": "열차별 정차역 (열차운행정보)"},
    )

    train_stop_idx = Column(BigInteger, primary_key=True, autoincrement=True)
    trn_no = Column(String(10), nullable=False, comment="열차번호(TAGO trainno와 동일)")
    seq = Column(Integer, nullable=False, comment="정차 순서(trn_run_sn)")
    stn_cd = Column(String(12), comment="역코드(stn_cd)")
    stn_nm = Column(String(50), nullable=False, comment="역명('역' 접미사 없음)")
    stop_se_cd = Column(String(4), comment="정차구분코드(01 시발/05 종착/11 여객승하차 등)")
    stop_se_nm = Column(String(20), comment="정차구분명(시발/종착/여객승하차/통과 등)")
    mrnt_nm = Column(String(50), comment="주운행선명(노선, 예: 경부선)")
