import json
import os

from dotenv import load_dotenv
from korail2 import Korail
from korail2.korail2 import KORAIL_LOGIN

load_dotenv()

korail_id = os.getenv("ID")
korail_pw = os.getenv("PW")

if not korail_id or not korail_pw:
    print("로그인 실패: .env에 ID/PW가 없습니다")
    exit()

korail = Korail(korail_id, korail_pw, auto_login=False)

ok = korail.login()
print("login result:", ok)
print("logined:", korail.logined)

if not ok:
    # login()은 실패 사유를 삼키므로, 서버 원본 메시지를 직접 다시 받아 출력한다.
    # (korail2는 응답 charset을 잘못 잡아 r.text가 깨지므로 r.content를 utf-8로 직접 디코딩)
    enc_pw = korail._Korail__enc_password(korail_pw)
    data = {
        "Device": "AD",
        "Version": "231231001",
        "txtInputFlg": "4",  # 4=휴대폰번호 / 5=이메일 / 2=회원번호
        "txtMemberNo": korail_id,
        "txtPwd": enc_pw,
        "idx": korail._idx,
    }
    j = json.loads(korail._session.post(KORAIL_LOGIN, data=data).content.decode("utf-8"))
    print("실패 코드:", j.get("h_msg_cd"))
    print("실패 사유:", j.get("h_msg_txt"))
    exit()

print("회원번호:", korail.membership_number)
print("이름:", korail.name)
print("이메일:", korail.email)

reservations = korail.reservations()
print("예약 내역:", reservations)

tickets = korail.tickets()
print("승차권:", tickets)
