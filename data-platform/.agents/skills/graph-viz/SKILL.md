---
name: graph-viz
description: 코퍼스 의존성 그래프를 브라우저에서 시각화한다. 사용자가 "그래프 보여줘 / 그래프 시각화 / 그래프 열어줘 / graph 띄워줘 / 위임 관계를 그림으로 / visualize the graph"처럼 그래프를 눈으로 보려 하면 라이브 뷰어를 띄운다(브라우저 자동 오픈, 그래프 DB가 여러 개면 드롭다운 선택, DB가 바뀌면 자동 갱신). 공유·오프라인용 단일 HTML 파일이 필요하면 정적 export를 만든다. 법안→법률 위임(delegates_to)·코드 의존 관계를 노드/엣지로 그린다.
allowed-tools: Bash(make graph), Bash(make graph *), Bash(make graph-serve), Bash(make graph-serve *), Bash(uv run python tools/viz/graph.py *), Bash(uv run python tools/viz/server.py *)
compatibility: data-platform 색인(data/serving/index.sqlite)이 빌드되어 있어야 하며 uv 로 실행한다. 색인이 없으면 make build 가 선행되어야 한다.
---

# Graph viz

코퍼스 그래프(gold.entities / gold.relations, 또는 serving 인덱스의 nodes/edges)를 힘-방향
Canvas 그래프로 그린다. 외부 라이브러리 없이 오프라인으로 동작한다. 두 가지 모드가 있다.

## 언제 무엇을

| 사용자 의도 | 실행 |
|---|---|
| **그래프를 보고 싶다** (탐색·상호작용) | 라이브 뷰어 — `make graph-serve` |
| **파일 하나로 공유/보관** (오프라인) | 정적 export — `make graph` |

기본은 **라이브 뷰어**다. "보여줘/열어줘/시각화"는 브라우저로 띄우라는 뜻이다.

## 라이브 뷰어 — `make graph-serve`

```
make graph-serve            # 127.0.0.1:8777 로 브라우저 자동 오픈. Ctrl-C 로 종료.
make graph-serve PORT=9000  # 포트 지정
```

- **DB 선택** — 그래프 DB가 둘 이상이면(예: `lake.gold`와 빌드된 `index.sqlite`) 상단 드롭다운에서
  고른다. 하나뿐이면 그것으로 자동 표시된다. 소스 발견 범위: DuckLake gold + `data/serving/*.sqlite`
  중 nodes/edges 테이블이 있는 것.
- **자동 갱신** — 선택한 DB가 바뀌면(예: `make build`/`make transform` 후, 또는 `make watch`가
  재빌드할 때) 페이지가 스스로 다시 그린다. 노드 위치와 화면은 유지된 채 갱신된다.
- **함께 쓰기** — 입력 파일을 고칠 때마다 DB까지 자동으로 재빌드하려면 **다른 터미널에서 `make watch`**
  를 돌린다. 뷰어는 그 결과를 실시간으로 반영한다.

서버는 stdlib만 쓰는 로컬 도구다(프레임워크·네트워크 없음, `tools/hitl`과 같은 방식).

## 정적 export — `make graph`

```
make graph                  # -> data/serving/graph.html (자립형 단일 HTML, file:// 로 열림)
make graph SOURCE=lake      # gold 레이어에서 (기본: 빌드된 인덱스 있으면 그걸, 없으면 lake)
make graph SOURCE=index     # 빌드된 serving 인덱스에서
```

브라우저로 여는 것은 사용자에게 경로를 알려주거나 `open data/serving/graph.html`(macOS)로 연다.

## 데이터가 없을 때

그래프 DB가 하나도 없으면(신선한 체크아웃) 먼저 만들어야 한다:

- lake.gold 만 필요하면 `make transform`, 전체(서빙 인덱스 포함)는 `make build`.
- 뷰어가 "그래프 DB가 없습니다"를 표시하면 위 명령을 안내하고, `make build`는 전체 파이프라인이라
  무거우니 사용자에게 먼저 확인한다.

## 읽는 법 (화면)

- 색 = 노드 종류: 법안·문서 / 기존 법률 / 코드 심볼 / 모듈.
- 굵고 밝은 선 = 강한 관계(위임 `delegates_to` weight 1.0), 옅은 선 = 약한 관계(언급 0.3).
- 노드에 마우스를 올리면 이웃과 상세(나감/들어옴/연결 목록)가 뜬다. 드래그로 이동, 휠로 확대.
- 다크/라이트 테마 모두 대응.

구현: `tools/viz/graph.py`(정적), `tools/viz/server.py`(라이브), 공통 프론트 에셋 `tools/viz/template.html`.
