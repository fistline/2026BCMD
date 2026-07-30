---
name: corpus-lookup
description: 프로젝트 루트(또는 gen-docs·gen-apps 작업 중)에서 data-platform의 색인 코퍼스를 조회해 근거 조문으로 답한다. 코퍼스에는 한국 법안·법률 원문(통과된 토큰증권 개정법 2건, 디지털자산 법안들)이 색인돼 있다. 법안·조문·시행일·전매제한·유통제한·예치금 등 문서 내용 질문, 증권신고서·dApp 작업 중 법령 근거 확인에 사용. data-platform 디렉터리 안에서 직접 작업 중일 때는 이 래퍼 대신 원본 스킬(corpus-search·corpus-graph)을 쓴다.
allowed-tools: Bash(make -C data-platform query *), Bash(make -C data-platform impact *), Bash(make -C data-platform collections*)
compatibility: data-platform 색인(data/serving/index.sqlite)이 있어야 하며 uv 로 실행한다. 색인이 없으면 make -C data-platform fetch-index(발행된 색인 설치, 약 92MB·1분) 또는 make -C data-platform build(직접 인코딩, 약 32분)가 선행되어야 한다.
---

# Corpus lookup (루트 래퍼)

data-platform 밖(cwd=프로젝트 루트)에서 코퍼스를 조회하는 **얇은 래퍼**다.
검색 방법·랭킹 해석·컬렉션 규칙의 정본은 `data-platform/.agents/skills/corpus-search/SKILL.md`와
`corpus-graph/SKILL.md`다 — 이 파일은 **호출 형태만** 루트 cwd에 맞춘다. 규칙을 재서술하지 않는다.

## 호출

```bash
make -C data-platform query Q="예치금 분리보관"                  # 내용 검색 (hybrid)
make -C data-platform query Q="장외거래 전매" COLLECTION=<컬렉션>  # 컬렉션 한정
make -C data-platform collections                                # 컬렉션 목록
make -C data-platform impact NODE=<node-id>                      # 그래프 추적 (upstream)
```

## 규칙

- **답은 조회 결과에서.** 결과의 `rel_path` + `heading`을 인용한다. 랭킹 필드(vector_rank 등)
  해석은 corpus-search 원본 규칙을 따른다 — 읽지 않았으면 원본 SKILL.md를 열어 확인한다.
- **색인이 없거나 비어 있으면** 조회가 실패한다. 실패 메시지가 두 갈래를 그대로 알려 준다:
  `make -C data-platform fetch-index`(발행된 색인을 받아 설치, 약 92MB·1분, sha256 검증)와
  `make -C data-platform build`(직접 인코딩, 약 32분). **둘 다 임의로 돌리지 말고 사용자에게
  확인**한다 — 전자는 네트워크를 쓰고 후자는 무겁다. 확인되면 대개 fetch-index가 먼저다.
  단 `.env`의 임베더가 발행자와 달라야 하면 fetch는 다운로드 전에 거부하고 두 시그니처를
  보여 준다 — 그때는 `.env`를 맞추거나 build로 간다.
- 더 깊은 작업(새 소스 온보딩, 검색 miss 교정, 문서 구조 파싱)은 data-platform의 해당 스킬
  (source-onboarding, correction-harvesting, doctype-profile-authoring)로 넘어간다 — cwd를
  data-platform으로 옮겨 수행.
