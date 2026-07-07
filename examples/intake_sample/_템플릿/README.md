# 실제 글 투입 방법 (CA-3)

실제 카페 글로 정리 파이프라인을 검증하려면 글 1건을 아래 형식으로 넣으면 됩니다.

## 1) 폴더 만들기
`inbox/<글슬러그>/` 폴더를 만든다. (예: `inbox/사회복지사2급_비용/`)
※ `inbox/`는 개인정보 보호를 위해 git에 올라가지 않습니다(로컬에만).

## 2) 파일 2개 넣기
- **body.txt** — 글 본문 전체를 그대로 붙여넣기 (제목 줄이 맨 위에 있어도 자동 제외됨)
- **meta.json** — 아래 항목 채우기 (모르면 비워도 됨)

```json
{
  "title": "글 제목",
  "cafe_name": "공준모",
  "account_id": "업로드계정라벨",
  "staff_name": "담당자이름",
  "keyword": "대표 키워드",
  "keyword_tier2": "2차 키워드(없으면 삭제)",
  "category": "심리·상담",
  "publish_date": "2026-01-06",
  "normalized_url": "https://cafe.naver.com/카페/글번호",
  "images": [
    { "image_order": 1, "image_type": "제목배너", "image_role": "썸네일", "image_source_type": "내부제작", "reuse_scope": "image_reuse_allowed", "contains_person": false, "contains_logo": true, "contains_text": true, "nearby_paragraph_no": 1 }
  ]
}
```

## 3) 실행
```
python src/intake_manual.py inbox/<글슬러그>
```
→ 본문 3버전(원문/정제/마스킹) 저장 + 문단 역할 + 이미지 분류 + 사람용 요약(out/intake/)이 나옵니다.

## 재사용 범위(reuse_scope) 값
- `image_reuse_allowed` — 내부 제작물 등 원본 재사용 가능
- `image_pattern_only` — 원본 재사용 금지(개인정보·외부 이미지), 패턴만 참고
- `image_rights_review` — 권한 확인 필요

완성 예시는 옆 폴더 `examples/intake_sample/임상심리사2급_응시자격/` 참고.
