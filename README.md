# Trailer

AI 기반 맞춤형 철도 여행 코스 추천, 실시간 관광 안내, 자동 여행 기록 콘텐츠 생성을 통합한 스마트 기차여행 플랫폼

## Requirements

- **Python**: 3.12.3 (버전 변경 가능)

## 기술 스택

- **Framework**: FastAPI
- **ORM**: SQLAlchemy
- **Database**: PostgreSQL
- **Server**: Uvicorn

## 프로젝트 구조

```
├── main.py            # FastAPI 앱 진입점
├── config/            # 설정 관리
├── core/              # 공통 응답, 예외 처리
├── databases/         # DB 연결, 모델, DAO
├── routers/           # API 라우터
├── schemas/           # 요청/응답 스키마
├── services/          # 비즈니스 로직
├── utils/             # 유틸리티 (로깅 등)
└── requirements.txt
```

## 설치 및 실행

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

## API 문서

서버 실행 후 아래 경로에서 확인:
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

---

## 🗂️ 새 API 추가 시 노션 문서화 가이드

API 명세는 `scripts/sync_notion.py`가 **FastAPI의 OpenAPI 스펙 + 코드 정적 분석**으로 추출해 노션 DB에 자동 동기화합니다(엔드포인트 1개 = 페이지 1개). **마법이 아니라 코드에서 뽑는 것**이라, 아래 관례를 지켜야 Request·Response·Error가 제대로 문서화됩니다.

### ✅ 체크리스트 — 새 엔드포인트가 완전히 문서화되려면

| 항목 | 해야 할 것 | 안 하면 |
|------|-----------|---------|
| **Request** | 함수 인자에 Pydantic 요청 모델 선언 + 각 필드에 `Field(..., description="...")` | 본문 표가 비거나 설명 칸이 빔 |
| **Response (payload)** | 라우트에 `response_model=CommonResponse[XxxResponse]` 선언 (응답 없으면 `CommonResponse[None]`) | 공통 엔벨로프로만 표시되고 payload 형태가 안 나옴 |
| **Response (message)** | 핸들러에서 `CommonResponse.success_response("성공 메시지", ...)` 리터럴 사용 | message 값이 표시되지 않음 |
| **Error** | `raise BadRequestException/NotFoundException/UnauthorizedException/DuplicateException("메시지")` 를 라우터 → service/util/core/DAO/`Depends` 경로로 **도달 가능하게** 발생 | 에러가 누락되고 공통 카탈로그로만 폴백 |

#### 예시
```python
# schemas/xxx_schema.py
class XxxCreateRequest(BaseModel):
    name: str = Field(..., description="이름")

class XxxResponse(BaseModel):
    xxx_idx: int = Field(..., description="PK")

# routers/xxx.py
@router.post(
    "",
    summary="Xxx 생성",
    description="...",
    response_model=CommonResponse[XxxResponse],   # ← Response payload 문서화
)
async def create(request_data: XxxCreateRequest, db: Session = Depends(get_db)):
    result = xxx_service.create(...)               # 서비스에서 raise 하는 예외가 자동 추출됨
    return CommonResponse.success_response("등록 성공", data=result)  # ← message 추출
```

### ⚠️ 자동 추출의 한계 (사각지대는 `description`으로 보강)

- **성공 응답 1개(200/201)만** 문서화됩니다. 상황별로 다른 payload·여러 status code는 표현되지 않습니다.
- **에러는 4종 예외만** 인식합니다(`NotFound·BadRequest·Duplicate·Unauthorized`). **새 예외 타입을 추가하면** `scripts/error_index.py`의 `EXC_CODE` 매핑에 등록해야 잡힙니다.
- 정적 분석이라 **흐름을 보지 않습니다.** 다음은 누락/오탐이 생기므로 라우트 `description`에 직접 한 줄 적어 보강하세요:
  - dict/변수를 통한 **간접 호출**로 가려진 예외 (예: 핸들러를 dict로 디스패치)
  - **`try/except`로 삼켜져** 실제로는 발생하지 않는 예외
- 동적 메시지(f-string 변수)는 `"{변수}"` 형태로 표시되고, `422`(요청 형식 오류)·`500`(서버 오류)은 공통 안내로 처리됩니다.

### ▶️ 동기화 실행

```bash
# 노션 호출 없이 추출 결과만 확인 (엔드포인트 목록)
python scripts/sync_notion.py --dry-run

# 코드에서 엔드포인트별로 추출된 에러 확인
python scripts/error_index.py

# 실제 동기화 (CI 또는 수동)
NOTION_TOKEN=xxx NOTION_DATABASE_ID=xxx python scripts/sync_notion.py
```

---

## ⚠️ 개발 시작 전 필수 습관

🔄 **코딩 시작 전, 항상 git pull 먼저!**
최신 코드와 충돌을 방지하고 팀원들과의 협업을 원활하게 하기 위해
개발을 시작하기 전에 반드시 원격 저장소의 최신 변경사항을 받아오세요.

```bash
# 매일 작업 시작 전 실행!
git pull origin main
```

---

## 📌 Commit Convention

프로젝트의 일관된 커밋 메시지 작성을 위한 규칙입니다.

### 📝 커밋 메시지 구조

```
<emoji> [Type] 커밋 제목

(선택) 본문: 상세한 설명

(선택) 꼬리말: 이슈 번호 등
```

### 🏷️ Type 목록

| Emoji | Type | 설명 |
|-------|------|------|
| ✨ | Feat | 새로운 기능 추가 |
| 🐛 | Fix | 버그 수정 |
| 📝 | Docs | 문서 수정 |
| 💄 | Style | 코드 포맷팅, 세미콜론 누락 등 (코드 변경 없음) |
| ♻️ | Refactor | 코드 리팩토링 |
| ✅ | Test | 테스트 코드 추가 또는 수정 |
| 🔧 | Chore | 빌드 업무 수정, 패키지 매니저 수정 등 |
| 🎨 | Design | CSS 등 사용자 UI 디자인 변경 |
| 💡 | Comment | 주석 추가 및 변경 |
| 🚚 | Rename | 파일 또는 폴더명 수정 |
| 🔥 | Remove | 파일 삭제 |
| 🚑 | !HOTFIX | 급하게 치명적인 버그를 고치는 경우 |

### ✍️ 커밋 메시지 예시

```bash
# 기본 예시
✨ [Feat] 사용자 관리 기능 추가 및 로그인 서비스 개발
🐛 [Fix] 로그인 시 세션 만료 오류 수정
📝 [Docs] README에 프로젝트 설명 추가

# 본문이 있는 예시
♻️ [Refactor] 데이터베이스 연결 코드 최적화

불필요한 연결 풀을 제거하고 싱글톤 패턴으로 변경하여
메모리 사용량을 30% 감소시킴

# 이슈 번호와 함께
🔧 [Chore] requirements.txt 업데이트

Resolves: #123
See also: #456, #789
```

### ✅ 작성 규칙

| 번호 | 규칙 | 설명 |
|------|------|------|
| 1️⃣ | 제목 길이 | 50자 이내로 작성 |
| 2️⃣ | 제목과 본문 분리 | 한 줄 띄워서 분리 |
| 3️⃣ | 본문 내용 | "무엇을", "왜" 변경했는지 작성 |
| 4️⃣ | 제목 형식 | 명령문으로 작성 (과거형 ❌) |
| 5️⃣ | 마침표 금지 | 제목 끝에 마침표(.) 사용 금지 |
| 6️⃣ | 단일 목적 | 한 커밋에는 한 가지 문제만 담기 |

---

## 🔧 properties_dev.ini 설정

`config/properties_dev.ini`는 gitignore 대상입니다. 아래 키들이 필요합니다.

```ini
[app]
db.url = mysql+pymysql://{user}:{password}@{host}:{port}/{database}?charset=utf8mb4
```

---

## 🕒 DB 타임존 설정

시간 컬럼(`created_at` 등)을 한국 시간(KST) 기준으로 기록/조회하려면, 운영 DB에 아래를 한 번 실행하세요. (새 연결부터 적용됨)

```sql
ALTER DATABASE "{database}" SET timezone TO 'Asia/Seoul';
```
