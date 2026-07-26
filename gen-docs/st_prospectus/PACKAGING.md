# 패키징 — sto-filing

이 영역은 **정본 1벌**에서 **단독 실행 프롬프트 3종**을 낸다.
`dist/`는 생성물이며 손으로 고치지 않는다.

```
정본                                생성물
──────────────────────────────      ──────────────────────────────────────
sto-filing/                    ──→  dist/CLASSIFY_PROMPT.md
  SKILL.md                          dist/PROSPECTUS_PROMPT_투자계약증권.md
  references/*.md                    dist/PROSPECTUS_PROMPT_신탁수익증권.md
prompt-templates/*.md          ──→
```

**소스를 수정했으면 반드시 재생성한다.** 잊으면 배포본이 옛 버전을 조용히 서빙한다.

```bash
cd gen-docs/st_prospectus
python3 build_prompts.py
```

커밋 전 확인:

```bash
python3 build_prompts.py --check     # dist/ 가 최신이 아니면 1을 반환한다
```

> **스킬 자체는 패키징하지 않는다.** `.claude/skills/sto-filing`과 `.agents/skills/sto-filing`이
> `sto-filing/` 폴더를 심볼릭 링크로 가리키므로 **폴더가 곧 배포본**이다.
> 예전에 `sto-filing.skill` zip을 함께 두었으나 **읽는 곳이 하나도 없었고**(참조가 전부
> "재생성하라"는 문서였다), 43KB 바이너리가 소스 편집마다 통째로 바뀌어 `git diff`만
> 오염시켜 제거했다. `gen-apps`의 다른 스킬 2종도 zip 없이 폴더로만 배포된다.
> 외부 업로드용 zip이 필요해지면 그때 만든다 — `zip -r -X -q sto-filing.skill sto-filing`.

---

## 무엇이 어디의 정본인가

| 대상 | 정본 | 반영되는 곳 |
|---|---|---|
| 판정 결정트리 (STEP 0~5) | `sto-filing/references/classification.md` | 스킬 · CLASSIFY 프롬프트 |
| 문서 골격 · F/N/J · 위험 카탈로그 1층·3층 · 심사 매트릭스 | `sto-filing/references/common-core.md` | 스킬 · 작성 프롬프트 2종 |
| 유형별 델타 (2층 위험 · 유형 고유 매트릭스) | `references/{investment-contract,trust-beneficiary}.md` | 스킬 · 해당 유형 프롬프트 |
| 실물 대조 정본 | `sto-filing/references/reference-filings.md` | 스킬 · 작성 프롬프트 2종 |
| 참조 우선순위 · 1차 자료 | `sto-filing/references/sources.md` | 스킬 · 프롬프트 3종 전부 |
| 프롬프트 골조 (역할·입력·PHASE·출력계약·자기점검) | `prompt-templates/*.md` | **프롬프트에만.** 스킬에는 반영되지 않는다 |

**`dist/`를 직접 고치지 않는다.** 고쳐도 다음 생성에서 사라진다.
생성 파일 첫머리에 소스 목록과 해시가 박히므로 어디서 왔는지 추적할 수 있다.

---

## 규칙

- 편집은 항상 `sto-filing/` 또는 `prompt-templates/` 안에서 한다. 최상위에 loose `.md` 사본을
  만들지 않는다 (과거에 두 벌이 drift한 사고가 있었다).
- 신고서 산출물 폴더 `<slug>/`는 소스 폴더 밖이므로 zip에 자연히 포함되지 않는다 — 유지할 것.
- 생성은 **결정적**이다. 소스가 같으면 결과가 바이트 단위로 같다. 타임스탬프를 넣지 않으므로
  `git diff`가 실제 변경만 보여준다.
- `prompt-templates/`의 골조는 **프롬프트 전용**이다. 여기에 규범이나 카탈로그를 쓰지 않는다 —
  쓰는 순간 스킬과 프롬프트가 갈라진다. 내용은 언제나 `references/`에 둔다.

---

## 왜 프롬프트를 손으로 쓰지 않는가

유형별 프롬프트를 각각 손으로 유지하면 **공통 골격 439줄이 두 벌로 복제**된다.
실제로 2026-07-26 개정(실물 대조 배선 · 위험 카탈로그 3층 · 유의사항 블록 · 심사 V계열) 7건 중
**5건이 공통부**였다. 두 벌이면 매번 두 번 고치고, 한쪽만 고치는 순간 어긋난다.

생성 방식은 배포 편의(붙여넣기 1회로 실행)와 단일 정본을 동시에 만족시킨다.
