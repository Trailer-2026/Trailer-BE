"""한국철도공사 열차운행정보(travelerTrainRunInfo2) 클라이언트 — 열차별 실제 정차역.

data.go.kr B551457. 이 API는 열차번호/날짜 필터 조회를 지원하지 않고(보존기간 3개월~1일 전)
전량 페이징만 된다. 그래서 '최근 하루치'를 페이징으로 받아 열차번호별 정차 시퀀스를 만든다
(정차 패턴은 열차번호별로 안정적이라 미래 여정에도 재사용). 적재는 scripts/sync_train_stops.py.

TAGO(GetStrtpntAlocFndTrainInfo)의 trainno와 이 API의 trn_no는 동일 코레일 번호 체계(검증됨).
SRT(수서 출발, SR 운영)는 코레일이 아니라 이 API에 없다 → 미수록(정차수 null 폴백).
"""
from config import Config
from utils import dgo

_RUN_INFO = "https://apis.data.go.kr/B551457/run/v2/travelerTrainRunInfo2"
# 운행정보 API는 TAGO(1613000)와 같은 data.go.kr 계정 키로 접근된다 → traininfo 키 재사용.
_KEY_SECTION, _KEY_NAME = "traininfo", "service_key"


def _service_key() -> str:
    return Config.read(_KEY_SECTION, _KEY_NAME)


def fetch_day(ymd: str, *, rows_per_page: int = 2000, max_pages: int = 30) -> list[dict]:
    """run_ymd=ymd 하루치 정차역 레코드 전체를 페이징으로 받아 리스트로 반환.

    응답은 (run_ymd → trn_no → trn_run_sn) 오름차순이라 같은 날짜가 앞쪽에 연속으로 모여 있다.
    대상 날짜 블록을 다 지나면(이후 날짜만 나오면) 조기 종료해 불필요한 호출을 막는다.
    반환: {trn_no, seq, stn_cd, stn_nm, stop_se_cd, stop_se_nm, mrnt_nm} dict 리스트.
    """
    key = _service_key()
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        params = {
            "serviceKey": key,
            "numOfRows": rows_per_page,
            "pageNo": page,
            "_type": "json",
        }
        rows = dgo.items(dgo.get_body(_RUN_INFO, params, timeout=30))
        if not rows:
            break
        matched_this_page = False
        for x in rows:
            if x.get("run_ymd") != ymd:
                continue
            matched_this_page = True
            # 필수 필드(열차번호·정차순번·역명)가 없거나 순번이 숫자가 아니면 그 행만 건너뛴다
            # — 불량 행 하나가 KeyError/ValueError로 하루치 전체 적재를 무너뜨리지 않도록.
            trn_no, stn_nm = x.get("trn_no"), x.get("stn_nm")
            try:
                seq = int(x["trn_run_sn"])
            except (KeyError, TypeError, ValueError):
                continue
            if not trn_no or not stn_nm:
                continue
            out.append({
                "trn_no": trn_no,
                "seq": seq,
                "stn_cd": x.get("stn_cd"),
                "stn_nm": stn_nm,
                "stop_se_cd": x.get("stop_se_cd"),
                "stop_se_nm": x.get("stop_se_nm"),
                "mrnt_nm": x.get("mrnt_nm"),
            })
        # 대상 날짜를 이미 수집했는데 이 페이지엔 하나도 없으면 블록을 지난 것 → 종료.
        if out and not matched_this_page:
            break
    return out
