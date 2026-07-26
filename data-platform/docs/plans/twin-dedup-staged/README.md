# twin-dedup 착지 대기 아티팩트

검증 완료된 퍼지 쌍둥이 탐지기. 다른 세션의 연속 빌드로 유휴 창이 없어 착지 대기.
설계·근거·검증은 `../twin-dedup-fuzzy.md`.

## 착지 (빌드 유휴 시, 한 번에)
```sh
cp docs/plans/twin-dedup-staged/document_twins.sql.staged transform/models/silver/document_twins.sql
cp docs/plans/twin-dedup-staged/documents.sql.staged      transform/models/silver/documents.sql
make build            # document_twins materialize + 회귀 없음 확인
make eval-graph       # green
# 인박스 쌍둥이 catch 검증: 법안 pdf 1건을 data/inbox/documents/에 드롭 후 rebuild →
#   silver.document_twins에 (pdf→hwp) 1행, index에 pdf 미색인 확인
```
`.staged` 접미사라 SQLMesh가 무시(빌드 무교란). document_twins.sql.staged는 그대로 모델, documents.sql.staged는 기존 모델의 대체본(LEFT JOIN + `AND tw.doc_id IS NULL`).
