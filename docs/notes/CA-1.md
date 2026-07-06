# CA-1 작업노트 — 뼈대 (2026-07-06)

## 한 것

1. **저장소 정리**: `fastpoten-content-assets.zip`에 갇혀 있던 확정본(CLAUDE.md, README.md, BACKLOG.md, .gitignore, docs/서비스기획서-v9.md, docs/개발기획서-v1.md)을 제자리로 복원. 루트의 개발기획서 중복본은 제거(docs/가 정위치).
2. **폴더 뼈대**: `src/ data/ corpus/ inbox/ tests/` 생성 (data·corpus·inbox는 깃 제외).
3. **`src/db.py`** — 전체 스키마 생성(서비스기획서 v9 §3 기준, 멱등):
   - 자산 테이블 11개: staff, posts, post_paragraphs, post_images, reference_signals, keywords, post_keywords, content_clusters, post_embeddings, extraction_logs, review_pairs
   - 룰북 테이블 3개: rulebook_categories, rulebook_banned_words, rulebook_pii_patterns
   - DB 파일: `data/content_assets.sqlite3` (CRM과 완전 별개)
4. **`src/load_rulebook.py`** — 룰북 V4.2 최소 적재(멱등, 재실행 안전):
   - 카테고리 15건(①시트) / 금지어 14건(④시트, CTA 문구는 범위 밖이라 미적재) / 개인정보 패턴 6건
   - 개인정보 패턴은 룰북 엑셀에 없어 **서비스기획서 v9 §8·§8-2 정의를 seed**로 넣음: 전화번호(일반·대표번호), 오픈채팅 링크, 직원 호칭(쌤/멘토/팀장) = 정규식 / 직원 실명·카페 닉네임 = name_list 자리만(CA-3에서 목록 확보 후 채움)
5. **`tests/test_ca1.py`** — 12개 테스트 전부 통과:
   - 스키마: 14테이블 존재, 멱등 재실행, posts에 본문 텍스트 컬럼 없음(경로만), 등급/감점 컬럼 없음
   - 적재: 건수 정확(15/14/6), 재실행해도 중복 누적 없음, 정규식 유효성 + 실제 예시 탐지(010번호·1588번호·오픈채팅·"김철수쌤")
   - 보안: 계정 정보 테이블/password 컬럼 부재

## 보안 처리 (불변 2)

- `1부서 카페 포스팅 시트.xlsx` 원본에 **계정 정보 탭(자격증명)** 이 있어 깃 반입 금지 처리(.gitignore).
- 대신 **계정 정보 탭만 제거한 사본** `1부서 카페 포스팅 시트_계정정보제거.xlsx`(25개 탭)를 커밋 → 집에서 CA-2 진행 가능.
- zip 원본도 깃 제외(내용물은 전부 복원됨).

## 설계 판단 기록

- **post_paragraphs의 raw_text/clean_text**: 불변 4("본문은 파일에만")는 posts의 전체 본문 기준. 문단 텍스트는 v9 §3-4가 명시한 분석용 필드라 스키마에 포함. posts에는 경로 컬럼(body_*_path)만 있음 — 테스트로 강제.
- **룰북 ②팩트·③매칭규칙·⑤키워드(653개)**: CA-1 범위 밖. ⑤키워드는 CA-2(엑셀 적재)~CA-6(갭 히트맵)에서, ②팩트는 CA-6(룰북 역검증)에서 적재 예정.
- 새 의존성 없음: 표준 sqlite3 + 기설치 openpyxl만 사용.

## 사용자 눈검수 안내 (개발기획서 §6에는 CA-1 항목이 없어 최소 확인만)

1. `data/` 폴더 안에 `content_assets.sqlite3` 파일이 생겼는지
2. 아래 명령이 오류 없이 도는지(어느 PC에서든):
   ```
   python src/db.py
   python src/load_rulebook.py
   python tests/test_ca1.py
   ```
3. GitHub 저장소에 **원본 엑셀(계정 정보 포함)과 zip이 안 올라갔는지** — "_계정정보제거" 사본만 보여야 정상

## 다음: CA-2 — 엑셀 '○○ 현황' 시트 파싱 → posts + reference_signals 적재
