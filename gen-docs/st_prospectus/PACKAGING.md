# 패키징 — sto-filing.skill

단일 소스는 `sto-filing/`(SKILL.md + references/). 배포본은 이를 zip으로 묶은 `sto-filing.skill`.

**소스를 수정했으면 반드시 zip을 재생성한다.** 잊으면 배포본이 옛 버전을 조용히 서빙한다.

```bash
cd gen-docs/st_prospectus
rm -f sto-filing.skill && zip -r -X sto-filing.skill sto-filing
```

- 편집은 항상 `sto-filing/` 안에서 한다. 최상위에 loose `.md` 사본을 만들지 않는다
  (과거에 두 벌이 drift한 사고가 있었다).
- 신고서 산출물 폴더 `<slug>/`는 소스 폴더 밖이므로 zip에 자연히 포함되지 않는다 — 유지할 것.
