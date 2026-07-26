#!/usr/bin/env python3
"""스킬 eval 실행을 assertion 기준으로 채점해 grading.json 을 쓴다.

`pipeline/eval_*.py` 는 색인 검색을 재는 회귀 바닥이고, 이건 **스킬이 만든 문서**를 잰다.
대상이 달라 같은 파일에 두지 않았지만, 규율은 같다 — 근거 없이 통과를 주지 않는다.

**기계로 확인되는 것만 여기서 판정한다.** 판단이 필요한 항목은 verdict=None 으로 남기고
사람이 산출물을 읽고 `<workspace>/manual_grades.json` 에 근거와 함께 채운다.
그 파일은 verdict=None 자리에만 적용된다 — 기계 판정을 손으로 뒤집을 수 있으면 채점이
산출물이 아니라 기대에 맞춰지기 때문이다.

grading.json 의 스키마(`expectations` 배열의 `text`/`passed`/`evidence`)는 skill-creator
뷰어와 집계 스크립트가 기대하는 그대로다. 바꾸면 뷰어가 등급을 못 읽는다.

    python3 tools/skill-eval/grade.py <workspace>/iteration-1 --rules sto-filing
    python3 tools/skill-eval/grade.py <workspace>/iteration-1 --require-complete

**빌드 경로 밖에 있다** — `tools/hitl/` 과 같다. `make verify` 에 붙어 있지 않고, 부를 때만
돈다. 스킬 eval 은 LLM 실행이 선행돼야 해서(케이스당 10분·10만 토큰대) 상시 게이트로 쓸 수
없다. 그래서 기본은 리포트이고, `--require-complete` 는 **채점을 끝냈다고 선언할 때** 쓴다 —
보류가 남아 있으면 그 목록과 채울 키를 찍고 비정상 종료한다.

디렉터리 레이아웃은 skill-creator 규약을 따른다.

    <iteration>/manual_grades.json                  # 사람이 채운 판정 (iteration 별)
    <iteration>/<eval-name>/eval_metadata.json      # prompt + assertions
    <iteration>/<eval-name>/<config>/outputs/       # 채점 대상 산출물
    <iteration>/<eval-name>/<config>/run-N/outputs/ # 같은 구성을 여러 번 돌린 경우
    <iteration>/<eval-name>/<config>/timing.json    # 집계용(있으면)
    <iteration>/<eval-name>/<config>/grading.json   # 이 스크립트의 출력
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import re
from pathlib import Path

# 규칙 모듈 등록부. 스킬이 늘면 여기 한 줄 추가하고 rules_<name>.py 를 옆에 둔다.
# 도메인 정규식(법 조문·문구)은 전부 규칙 모듈 쪽에 있다 — 이 파일은 스킬을 모른다.
RULES = {
    "sto-filing": "rules_sto_filing",
}

# 보장성·금지 표현은 부정형·경고형으로도 반드시 등장한다. 증권신고서라면 오히려
#   "원금이나 수익을 보장하지 않습니다" / "원금·수익 보장과 무관" / "사실상 보장으로 재분류"
# 를 **써야** 한다. 그래서 단순 키워드 검사는 필연적으로 오탐을 낸다 — 실제로 냈다.
# 매치 주변에 부정·경고 표지가 있으면 위반으로 세지 않는다.
NEGATION = re.compile(r"않|무관|없|아니|금지|재분류|위험|말라|불가|배제|되지|해서는|오인|주의")


def load_outputs(run: Path) -> tuple[str, list[str]]:
    """실행 디렉터리의 산출물 전문과 파일명 목록."""
    names, blobs = [], []
    for f in sorted((run / "outputs").glob("*")):
        if f.is_file():
            names.append(f.name)
            try:
                blobs.append(f.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
    return "\n".join(blobs), names


def has(text: str, *pats: str) -> bool:
    """모든 패턴이 있어야 참."""
    return all(re.search(p, text) for p in pats)


def any_of(text: str, *pats: str) -> bool:
    """하나라도 있으면 참. 같은 뜻을 다르게 쓴 올바른 출력을 떨어뜨리지 않으려는 것이다."""
    return any(re.search(p, text) for p in pats)


def banned_hits(text: str, patterns: list[str], negation: re.Pattern = NEGATION) -> list[str]:
    """긍정 단언으로 쓰인 금지 표현만 골라낸다. 앞뒤 60자에 부정 표지가 있으면 뺀다."""
    hits = []
    for pat in patterns:
        for m in re.finditer(pat, text):
            ctx = text[max(0, m.start() - 60): m.end() + 60]
            if not negation.search(ctx):
                hits.append(f"{m.group(0)!r} … “{ctx.strip()[:70]}”")
    return hits


def outputs_digest(run: Path) -> str:
    """산출물 전체의 지문. 파일명과 내용이 하나라도 다르면 값이 달라진다."""
    h = hashlib.sha256()
    for f in sorted((run / "outputs").glob("*")):
        if f.is_file():
            h.update(f.name.encode("utf-8"))
            h.update(hashlib.sha256(f.read_bytes()).digest())
    return h.hexdigest()[:16]


def apply_manual(manual: dict, key: str, run: Path, results: list) -> list[str]:
    """사람 판정을 verdict=None 자리에만, 그리고 **판정할 때 본 그 산출물에만** 채운다.

    경로로만 묶으면 실행을 다시 돌렸을 때 어제의 판정이 오늘 파일에 조용히 재부착된다.
    실제로 그렇게 됐다 — eval 을 재실행했더니 삭제된 산출물을 근거로 든 판정 5건이
    새 산출물에 그대로 붙어 채점이 통과로 나왔다. 그래서 `_digest` 로 결속한다.
    어긋나면 적용을 거부하고 보류로 되돌린다 — 조용히 틀리느니 시끄럽게 비는 게 낫다.

    `_` 로 시작하는 키는 메타데이터이고 assertion 매칭 대상이 아니다.
    반환값은 매칭되지 않은 키 목록.
    """
    table = manual.get(key, {})
    rules = {k: v for k, v in table.items() if not k.startswith("_")}
    if not rules:
        return []

    actual = outputs_digest(run)
    expected = table.get("_digest")
    if expected is None:
        print(f"  ⚠ {key}: 수동 채점이 산출물에 결속되지 않았다. 확인 후 \"_digest\": \"{actual}\" 를 넣어라.")
    elif expected != actual:
        print(f"  ✗ {key}: 산출물이 바뀌었다(기록 {expected} → 현재 {actual}). "
              f"수동 채점 {len(rules)}건을 적용하지 않고 보류로 남긴다 — 새 산출물로 다시 판정하라.")
        return []

    used = set()
    for r in results:
        if r["passed"] is not None:
            continue
        for prefix, (verdict, evidence) in rules.items():
            if r["text"].startswith(prefix):
                r["passed"], r["evidence"] = verdict, f"[직접 열람] {evidence}"
                used.add(prefix)
                break
    return sorted(set(rules) - used)


def discover_runs(eval_dir: Path) -> list[tuple[Path, str]]:
    """채점할 실행들. (디렉터리, 표시이름) 목록.

    두 레이아웃을 다 받는다 — 한 구성을 한 번만 돌리면 `<config>/outputs/` 로 평평하고,
    같은 구성을 여러 번 돌리면 skill-creator 관습대로 `<config>/run-N/outputs/` 로 중첩된다.
    재현 편차를 보려면 반복 실행이 필요한데, 그때만 디렉터리가 한 겹 깊어진다.
    """
    runs = []
    for config in sorted(p for p in eval_dir.iterdir() if p.is_dir()):
        nested = sorted(p for p in config.glob("run-*") if (p / "outputs").is_dir())
        if nested:
            runs += [(r, f"{config.name}/{r.name}") for r in nested]
        elif (config / "outputs").is_dir():
            runs.append((config, config.name))
    return runs


def grade_run(rules, manual: dict, eval_dir: Path, run: Path, label: str | None = None) -> dict:
    text, names = load_outputs(run)
    meta = json.loads((eval_dir / "eval_metadata.json").read_text(encoding="utf-8"))
    ctx = {"text": text, "names": names, "has": has, "any_of": any_of, "banned_hits": banned_hits}

    results = []
    for a in meta["assertions"]:
        verdict = rules.judge(a, ctx)
        if verdict is None:
            verdict = (None, "판단 필요 — 스크립트로 검증 불가")
        results.append({"text": a, "passed": verdict[0], "evidence": verdict[1]})

    orphans = apply_manual(manual, f"{eval_dir.name}/{label or run.name}", run, results)
    if orphans:
        print(f"  ⚠ {eval_dir.name}/{run.name}: 매칭 안 된 수동 채점 {orphans}")

    passed = sum(1 for r in results if r["passed"] is True)
    failed = sum(1 for r in results if r["passed"] is False)
    pending = sum(1 for r in results if r["passed"] is None)
    # timing 은 grading.json 에 넣지 않는다 — skill-creator 집계 스크립트는 여기에 timing 이
    # 있으면 sibling timing.json 을 읽지 않고, 그러면 토큰 수가 0 으로 집계된다.
    out = {
        "expectations": results,
        "summary": {
            "passed": passed, "failed": failed, "pending": pending, "total": len(results),
            "pass_rate": round(passed / (passed + failed), 3) if passed + failed else 0.0,
        },
    }
    (run / "grading.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out["summary"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("iteration", type=Path, help="채점할 iteration 디렉터리 (eval-* 를 담고 있다)")
    ap.add_argument("--rules", default="sto-filing", choices=sorted(RULES),
                    help="적용할 규칙 모듈 (기본: sto-filing)")
    ap.add_argument("--manual", type=Path, default=None,
                    help="수동 채점 파일 (기본: <iteration>/manual_grades.json)")
    ap.add_argument("--require-complete", action="store_true",
                    help="보류가 하나라도 남아 있으면 목록을 찍고 비정상 종료한다 "
                         "(채점을 끝냈다고 선언할 때 쓴다)")
    args = ap.parse_args()

    it = args.iteration.resolve()
    if not it.is_dir():
        ap.error(f"디렉터리가 없다: {it}")

    rules = importlib.import_module(RULES[args.rules])
    # iteration 안에 둔다. 키가 <eval>/<config> 라서 워크스페이스 한 곳에 모으면
    # iteration-1 과 iteration-2 의 같은 케이스가 같은 키를 놓고 충돌한다.
    manual_path = args.manual or (it / "manual_grades.json")
    manual = json.loads(manual_path.read_text(encoding="utf-8")) if manual_path.exists() else {}
    if not manual:
        print(f"수동 채점 파일 없음({manual_path}) — 판단 필요 항목은 보류로 남는다.\n")

    print(f"{'eval':<34}{'구성':<15}{'PASS':>5}{'FAIL':>6}{'보류':>6}{'비율':>8}")
    print("-" * 76)
    pending: dict[str, list[str]] = {}
    for eval_dir in sorted(it.glob("eval-*")):
        if not (eval_dir / "eval_metadata.json").exists():
            continue
        for run, label in sorted(discover_runs(eval_dir)):
            s = grade_run(rules, manual, eval_dir, run, label)
            print(f"{eval_dir.name:<34}{label:<15}"
                  f"{s['passed']:>5}{s['failed']:>6}{s['pending']:>6}{s['pass_rate']:>8.0%}")
            if s["pending"]:
                key = f"{eval_dir.name}/{label}"
                graded = json.loads((run / "grading.json").read_text(encoding="utf-8"))
                pending[key] = [e["text"] for e in graded["expectations"] if e["passed"] is None]

    # 비율은 보류를 분모에서 뺀다. 그래서 미채점이 남은 채로도 100% 가 나온다 — 실제로 나왔고,
    # 29건 중 13건이 비어 있는 "만점"이었다. 채점을 끝냈다고 선언할 때 이 플래그로 막는다.
    total_pending = sum(len(v) for v in pending.values())
    if total_pending:
        hint = "" if args.require_complete else " (--require-complete 로 검사할 수 있다)"
        print(f"\n보류 {total_pending}건 — 비율 계산에서 빠져 있다.{hint}")
    if args.require_complete and pending:
        print(f"\n채점이 끝나지 않았다. {manual_path} 에 아래 키로 판정과 근거를 채운다.\n")
        for key, texts in pending.items():
            print(f'  "{key}": {{')
            for t in texts:
                print(f'    "{t[:40]}": [true, "<근거>"],')
            print("  },")
        raise SystemExit(1)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
