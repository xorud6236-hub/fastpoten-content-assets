# fastpoten-content-assets — 콘텐츠 자산화 시스템

부서 카페 글 약 11,000건을 정리해 "콘텐츠 자산 창고"로 만드는 프로젝트.
(원고·이미지 AI 생성은 다음 단계 — 이 저장소는 창고 만들기까지)

## 지금 상태
기획 완료(docs/ 참조). 코드는 아직 없음. **다음 작업 = CA-1(뼈대 만들기)**.

## 처음 올리기 (회사에서, 5분)
1. github.com 로그인 → New repository → 이름 `fastpoten-content-assets` → **Private** → Create
2. "uploading an existing file" 링크 클릭 → 이 폴더 안 파일 전부 드래그 → Commit
   (또는 GitHub Desktop 사용 시: Add local repository → 이 폴더 → Publish)

## 집에서 이어가기
1. 집 PC에서 저장소 내려받기(clone) — GitHub Desktop이면 Clone 버튼
2. 그 폴더에서 **클로드 코드 실행**
3. 첫 마디: **"CLAUDE.md 읽고 CA-1부터 진행해줘"**
   → 클로드 코드가 기획서를 읽고 planner→implementer→reviewer로 알아서 진행하며, 결정이 필요한 것만 쉬운 말로 물어봄

## 폴더 안내
- `docs/` 기획서(서비스 v9 = 무엇을, 개발 v1 = 어떻게)
- `CLAUDE.md` 클로드 코드가 매번 읽는 규칙서
- `BACKLOG.md` 나중에 할 일 목록
- `src/ data/ corpus/ inbox/` 는 작업이 시작되면 생김 (data·corpus는 깃허브에 안 올라감)

## 절대 규칙 한 줄 요약
전화번호·직원 호칭은 자동으로 가려진다 / 계정 비밀번호는 절대 안 들어간다 / 순위·조회수는 참고일 뿐 성적표가 아니다.
