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
