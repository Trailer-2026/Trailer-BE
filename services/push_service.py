"""푸시 알림 발송 서비스 — 수신 설정 확인 → 이력 저장 → FCM 발송을 한 곳에서 처리한다.

알림은 어디까지나 부가 기능이라 실패해도 호출한 쪽(여행 저장, 배치 루프)의 동작을
막지 않는다. 그래서 이 모듈의 함수는 예외를 밖으로 내보내지 않고 경고만 남긴다.

읽기(알림 화면 목록·읽음 처리)는 notification_service가 맡는다 — 발송/조회 책임 분리.
"""
import logging
from datetime import timedelta

from sqlalchemy.orm import Session

from core.enums import NotificationType
from databases.daos import notification_dao, notification_log_dao
from services import fcm_service
from utils.timezone import now_kst

logger = logging.getLogger(__name__)

# 알림 종류 → notification 테이블의 수신 설정 컬럼.
_ALARM_FIELD = {
    NotificationType.TRAVEL_SAVED: "event_alarm",
    NotificationType.TRAVEL_D1: "event_alarm",
    NotificationType.TRAVEL_DELETED: "event_alarm",
    # 탑승 알림도 일정 알림 스위치를 따른다 — 설정 화면의 스위치는 두 개(이벤트/풍경)뿐이라
    # 새 종류를 위해 세 번째를 만들면 앱 설정 화면까지 같이 바꿔야 한다.
    NotificationType.TRAIN_D10M: "event_alarm",
    # 스탬프 획득도 같은 이유로 이벤트 스위치를 따른다(설정 화면 스위치는 둘뿐).
    NotificationType.STAMP_EARNED: "event_alarm",
    NotificationType.SCENERY: "scenery_alarm",
}

# 탭해도 열 화면이 없는 종류 — 대상이 이미 삭제돼 조회하면 404다. 어느 여행이었는지는
# 이력(notification_log.travel_idx)에 남기되, 앱이 이동에 쓰는 FCM data에서는 뺀다.
_NO_DEEPLINK = {NotificationType.TRAVEL_DELETED}

# 알림 화면이 종류별 라벨로 쓰는 제목(디자인의 "풍경알림" 칩). OS 푸시의 제목이기도 하다.
_TITLE_TRAVEL = "일정알림"
_TITLE_SCENERY = "풍경알림"
# 스탬프는 일정과 성격이 달라 칩을 새로 뒀다. 값은 그냥 표시 문자열이라 서버만 바꾸면 되지만,
# 앱이 칩 색을 제목별로 지정해 뒀다면 이 값도 함께 등록해야 한다.
_TITLE_STAMP = "스탬프알림"


def notify(
    db: Session, user_idx: int, ntype: NotificationType, title: str, body: str,
    travel_idx: int | None = None, scenic_spot_idx: int | None = None,
    schedule_idx: int | None = None, ticket_idx: int | None = None,
    stamp_type: str | None = None, image_url: str | None = None,
) -> bool:
    """수신 설정을 확인하고 이력을 남긴 뒤 사용자의 모든 기기로 푸시한다. 발송했으면 True.

    - 설정이 OFF면 이력도 남기지 않고 False (알림 화면에도 안 뜨는 게 자연스럽다).
    - `image_url`은 푸시 배너에 함께 뜨는 사진(현재는 풍경 알림만 쓴다). 이력에는 남기지
      않는다 — 알림 화면 목록은 텍스트 줄이고, 풍경은 애초에 그 목록에 뜨지 않는다.
    - 이력을 먼저 커밋해, FCM 호출이 실패해도 알림 화면에는 남게 한다. 즉 False라도
      이력은 남아 있을 수 있고(FCM 실패), 중복 발송 판정은 이 이력을 기준으로 한다 —
      Firebase 장애 시 같은 알림을 다음 주기에 다시 보내지 않는다(at-most-once).
    - 등록된 기기 토큰이 없어도 True다 — 이력이 남아 알림 화면에는 뜬다.
    - 어떤 예외도 올리지 않는다 — 호출한 쪽의 트랜잭션·루프를 깨지 않기 위해서다.
    """
    try:
        if not _is_enabled(db, user_idx, ntype):
            return False

        notification_log_dao.create(
            db, user_idx=user_idx, notification_type=ntype.value, title=title, body=body,
            travel_idx=travel_idx, schedule_idx=schedule_idx, ticket_idx=ticket_idx,
            stamp_type=stamp_type, scenic_spot_idx=scenic_spot_idx,
        )
        db.commit()

        # data는 앱이 알림을 탭했을 때 어느 화면을 열지 판단하는 딥링크 정보.
        # 해당 없는 키는 빼서 보낸다(FCM data 값은 모두 문자열이라 빈 값과 구분이 안 된다).
        # 값의 문자열 변환은 firebase.send_multicast가 맡는다.
        data = {"type": ntype.value}
        if travel_idx is not None and ntype not in _NO_DEEPLINK:
            data["travel_idx"] = travel_idx
        if scenic_spot_idx is not None:
            data["scenic_spot_idx"] = scenic_spot_idx
        # 직접 입력 승차권은 여행이 없어(travel_idx NULL) 승차권 목록으로 보내야 한다.
        if ticket_idx is not None:
            data["ticket_idx"] = ticket_idx
        # 스탬프는 열 화면이 마이페이지 스탬프 탭이고, 어느 칸을 짚을지는 종류로 정한다.
        if stamp_type is not None:
            data["stamp_type"] = stamp_type
        result = fcm_service.send_push(db, user_idx, title, body, data=data, image_url=image_url)

        # 발송 흔적을 남긴다 — 풍경은 알림 화면에 안 떠서 이 로그가 확인 수단이다.
        # sent=0은 그 사용자의 기기 토큰이 하나도 등록돼 있지 않다는 뜻(에러 아님).
        logger.info(
            "알림 발송 user=%s type=%s sent=%d failed=%d | %s",
            user_idx, ntype.value, result.sent, result.failed, body,
        )
        return True
    except Exception as e:
        logger.warning("알림 발송 실패(무시하고 진행) user=%s type=%s: %s", user_idx, ntype, e)
        db.rollback()
        return False


def notify_travel_saved(db: Session, user_idx: int, travel) -> bool:
    """'일정에 추가되었어요' — 여행이 저장된 직후 발송. 반드시 커밋 이후에 호출한다.

    저장 시점에 이미 출발이 내일이면 D-1 알림도 바로 같이 보낸다. 자정 배치만 믿으면
    이 여행은 D-1 알림을 영영 못 받는다 — 다음 자정(출발 당일 00:00)의 배치는 '내일
    출발'을 찾으므로 이미 지나간 여행이 되기 때문이다.
    """
    sent = notify(
        db, user_idx, NotificationType.TRAVEL_SAVED,
        title=_TITLE_TRAVEL,
        body=f"'{travel.title}'{_josa_i_ga(travel.title)} 일정에 추가되었어요",
        travel_idx=travel.travel_idx,
    )
    if travel.start_date == now_kst().date() + timedelta(days=1):
        notify_trip_d1(db, user_idx, travel)
    return sent


def notify_trip_d1(db: Session, user_idx: int, travel) -> bool:
    """'일정이 하루 남았어요' — 출발 하루 전 발송. 여행당 1회만 나간다(멱등).

    자정 배치와 저장 시점 즉시 발송 두 경로가 같은 여행을 건드릴 수 있어, 이미 보낸
    이력이 있으면 건너뛴다. 이 검사는 빠른 경로일 뿐 경합(check-then-act)에는
    무력해서, 최종 방어는 notification_log의 부분 유니크 인덱스가 맡는다 — 늦게
    INSERT한 쪽이 거기서 걸려 notify 안에서 롤백되고 푸시도 나가지 않는다.
    """
    if notification_log_dao.exists(
        db, user_idx, NotificationType.TRAVEL_D1.value, travel.travel_idx
    ):
        return False
    return notify(
        db, user_idx, NotificationType.TRAVEL_D1,
        title=_TITLE_TRAVEL,
        body=f"'{travel.title}'의 일정이 하루 남았어요",
        travel_idx=travel.travel_idx,
    )


def notify_travel_deleted(db: Session, user_idx: int, travel) -> bool:
    """'일정에서 삭제되었어요' — 여행이 삭제된 직후 발송. 반드시 커밋 이후에 호출한다.

    소프트 삭제라 travel 행 자체는 남아 travel_idx FK도 유효하다. 다만 열면 404이므로
    FCM data에는 travel_idx를 싣지 않는다(_NO_DEEPLINK) — 이력에는 어느 여행이었는지
    남으니 식별은 되고, 앱이 실수로 이동시킬 여지만 없앤다.
    """
    return notify(
        db, user_idx, NotificationType.TRAVEL_DELETED,
        title=_TITLE_TRAVEL,
        body=f"'{travel.title}'{_josa_i_ga(travel.title)} 일정에서 삭제되었어요",
        travel_idx=travel.travel_idx,
    )


def notify_train_departure(
    db: Session, user_idx: int, *, dep_station: str | None, dep_at, minutes_left: int,
    train_label: str | None = None, seat_label: str | None = None,
    travel_idx: int | None = None, schedule_idx: int | None = None,
    ticket_idx: int | None = None,
) -> bool:
    """'곧 출발이에요' — 열차 출발 직전 발송. 출발 1건당 1회만 나간다(멱등).

    추천 코스 승차권(schedule)과 직접 입력 승차권(ticket) 두 출처를 모두 받는다 —
    둘 중 하나의 idx만 준다. 1분마다 도는 루프가 같은 열차를 10번 집으므로 이미 보낸
    이력이 있으면 건너뛴다. 이 검사는 빠른 경로일 뿐이고, 최종 방어는 notification_log의
    부분 유니크 인덱스가 맡는다(D-1과 같은 구조).

    train_label은 추천 코스만 있다('KTX 101') — 직접 입력 승차권엔 열차번호·등급 입력칸이
    없어 None이다. seat_label은 반대로 직접 입력에만 있을 수 있다('3호차 12A').
    dep_station도 None일 수 있다(schedule.dep_station이 nullable) — 문구에서 빠진다.
    """
    if notification_log_dao.exists_for_departure(
        db, user_idx, schedule_idx=schedule_idx, ticket_idx=ticket_idx
    ):
        return False
    return notify(
        db, user_idx, NotificationType.TRAIN_D10M,
        title=_TITLE_TRAVEL,
        body=_departure_body(dep_station, dep_at, minutes_left, train_label, seat_label),
        travel_idx=travel_idx, schedule_idx=schedule_idx, ticket_idx=ticket_idx,
    )


def _departure_body(
    dep_station: str | None, dep_at, minutes_left: int,
    train_label: str | None, seat_label: str | None,
) -> str:
    """탑승 알림 본문 — "10분 뒤 서울역에서 KTX 101 열차가 출발해요 (3호차 12A) · 12:10 출발".

    역명 표기가 출처마다 다르다 — schedule.dep_station은 '서울', ticket은 station을
    조인해 '서울역'이다. 사용자에게 보이는 문구는 하나여야 하므로 '역'을 붙여 맞춘다.

    역명이 없으면(schedule.dep_station은 nullable, 공백만 든 경우 포함) 그 자리를 통째로
    뺀다. 다른 값으로 메우면 없는 역을 가리키게 되고, 남은 정보(남은 시간·열차·출발
    시각)만으로도 '지금 나가야 한다'는 알림의 목적은 이룬다. 직접 입력 승차권은 station을
    조인해 오므로 항상 있다.

    분은 상수(10)가 아니라 **실제 남은 시간**을 쓴다. 서버가 잠깐 멈췄다 재개되면 남은
    시간이 10분보다 짧은 열차에도 알림이 나가는데, 그 때 '10분 뒤'라고 하면 3분 남은
    사람을 느긋하게 만든다. 출발 시각을 뒤에 같이 붙이는 것도 같은 이유다.
    """
    train = f"{train_label} 열차가" if train_label else "열차가"
    # 공백만 든 역명은 없는 것으로 본다 — 안 그러면 "  역에서"가 만들어진다.
    station = (dep_station or "").strip()
    if station:
        if not station.endswith("역"):
            station = f"{station}역"
        body = f"{minutes_left}분 뒤 {station}에서 {train} 출발해요"
    else:
        body = f"{minutes_left}분 뒤 {train} 출발해요"
    if seat_label:
        body += f" ({seat_label})"
    return f"{body} · {dep_at:%H:%M} 출발"


def _josa_i_ga(word: str) -> str:
    """한글 낱말 뒤에 붙일 주격 조사 '이'/'가'를 고른다 (받침 있으면 '이').

    여행 제목은 사용자가 자유롭게 지어서 '부산 여행'(받침 O)도 '제주도'(받침 X)도 온다.
    한글이 아닌 글자로 끝나면(영문·숫자·이모지) 판정이 불가능해 '가'로 둔다.
    """
    if not word:
        return "가"
    last = word[-1]
    if not ("가" <= last <= "힣"):
        return "가"
    has_jong = (ord(last) - 0xAC00) % 28 != 0
    return "이" if has_jong else "가"


def notify_stamp_earned(db: Session, user_idx: int, stamp_type: str, stamp_title: str) -> bool:
    """'스탬프를 획득했어요' — 스탬프가 처음 찍힌 직후 발송.

    **반드시 user_stamp 적립을 커밋한 뒤에 호출한다.** 적립이 '한 번만'을 보장하는 지점이라
    (user_stamp의 유니크 제약), 적립에 성공한 호출만 여기까지 온다 = 알림도 평생 한 번이다.
    그래서 다른 알림들과 달리 여기선 이력을 다시 뒤져 중복을 거르지 않는다.
    """
    return notify(
        db, user_idx, NotificationType.STAMP_EARNED,
        title=_TITLE_STAMP,
        body=f"'{stamp_title}' 스탬프를 획득했어요",
        stamp_type=stamp_type,
    )


def notify_scenery(
    db: Session, user, *, to_station: str, scenic_spot_idx: int,
    image_url: str | None = None, travel_idx: int | None = None,
    schedule_idx: int | None = None, ticket_idx: int | None = None,
) -> bool:
    """'지금 OO역 스팟을 지나고 있어요' — 열차가 풍경 구간에 들어섰을 때 발송. 보냈으면 True.

    언제 어느 구간인지는 scenic_plan_service가 판정하고, 여기서는 알림만 맡는다.

    **탑승 1건에서 스팟 1개당 1회**다. 시각표를 저장하지 않고 1분마다 다시 계산하는
    구조라(scenic_plan_service) 같은 구간이 발송 창 안에서 여러 번 잡힌다. 호출측이 이미
    보낸 스팟을 걸러내지만 그건 빠른 경로일 뿐이고, 최종 방어는 notification_log의 부분
    유니크 인덱스가 맡는다 — 늦게 INSERT한 쪽이 거기서 걸려 notify 안에서 롤백되고 푸시도
    나가지 않는다(D-1·탑승 알림과 같은 구조).

    그래서 다른 알림과 달리 **이력을 남기는 목적이 중복 판정 하나**다. 알림 화면 목록에는
    뜨지 않는다 — 화면에서 풍경은 목록의 한 줄이 아니라 상단 카드이고, 목록에서 빼는 건
    notification_log_dao._HIDDEN_TYPES가 맡는다.

    딥링크와 배너 사진은 그 구간의 대표 관광지 기준이다. 사진은 카테고리별 일러스트라
    조회 응답의 카드와 같은 값을 쓴다(utils.scenic.scenery_image_url 한 군데서 고른다).
    """
    return notify(
        db, user.user_idx, NotificationType.SCENERY,
        title=_TITLE_SCENERY,
        body=_scenery_body(user.nickname, to_station),
        travel_idx=travel_idx,
        schedule_idx=schedule_idx,
        ticket_idx=ticket_idx,
        scenic_spot_idx=scenic_spot_idx,
        image_url=image_url,
    )


def _scenery_body(nickname: str | None, to_station: str) -> str:
    """풍경 알림 본문 — "{닉네임} 님, 지금 {역명} 스팟을 지나고 있어요".

    닉네임은 nullable이라(소셜 로그인 직후 등) 없으면 호칭 없이 문장을 시작한다.
    """
    if nickname:
        return f"{nickname} 님, 지금 {to_station} 스팟을 지나고 있어요"
    return f"지금 {to_station} 스팟을 지나고 있어요"


def is_enabled(db: Session, user_idx: int, ntype: NotificationType) -> bool:
    """수신 설정이 켜져 있는지 — notify를 부르기 전에 미리 알아야 하는 호출부용 공개 창구.

    스탬프 적립이 이걸 쓴다. 알림을 보낼 때는 notify가 이력을 만들며 적립까지 함께
    커밋하지만, 설정이 꺼져 있으면 notify가 아무것도 커밋하지 않고 빠지므로 적립을
    직접 커밋해야 한다 — 그 갈림길을 판단하려면 미리 물어봐야 한다.
    """
    return _is_enabled(db, user_idx, ntype)


def _is_enabled(db: Session, user_idx: int, ntype: NotificationType) -> bool:
    """해당 종류의 알림 수신이 켜져 있는지. 설정 행이 없으면 기본값(수신)으로 본다.

    여기서 설정 행을 새로 만들지는 않는다 — 배치 루프 같은 읽기 경로에서 쓰기가
    생기는 걸 피한다. 행 생성은 사용자가 설정 화면에 들어올 때
    notification_service._get_or_create가 맡는다.
    """
    setting = notification_dao.get_by_user(db, user_idx)
    if setting is None:
        return True
    return bool(getattr(setting, _ALARM_FIELD[ntype]))
