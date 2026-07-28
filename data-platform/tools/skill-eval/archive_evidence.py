#!/usr/bin/env python3
"""eval 워크스페이스를 아카이브하고, 무엇이 들어 있는지 사람이 읽을 목록을 함께 남긴다.

    python3 tools/skill-eval/archive_evidence.py <workspace> [<workspace> ...] --out <dir>

**왜 필요한가.** eval 산출물과 채점 근거는 git 에 없다(생성물을 추적하지 않는 방침).
그래서 이 증거는 한 머신에만 존재한다 — 실제로 세션 중 PC 가 한 번 꺼졌고, `git reset --hard`
로 미커밋 작업을 날린 적도 있다. 도구는 커밋돼 있어 **재실행은 되지만**, 사람이 산출물을
읽고 내린 판정(`manual_grades.json`)은 다시 읽어야 복원된다. 그게 비싼 부분이다.

**지문이 왜 중요한가.** 사람 판정은 `_digest` 로 산출물에 결속돼 있다. 산출물이 사라지면
지문을 맞출 수 없어 그 판정은 영영 적용되지 않는다. 그래서 판정만 따로 빼내 보관하는 것은
답이 아니고, **산출물과 판정을 같은 아카이브에 함께** 둬야 한다.

아카이브는 tar.gz 하나 + 옆에 MANIFEST.md. 매니페스트는 압축을 풀지 않고도
"어느 iteration 에 무엇이 있고 점수가 얼마였나"를 알 수 있게 한다.
"""
from __future__ import annotations

import argparse
import json
import tarfile
from datetime import datetime, timezone
from pathlib import Path


def summarise(ws: Path) -> list[str]:
    """워크스페이스 한 개의 iteration 별 요약 줄."""
    lines = []
    for it in sorted(ws.glob("iteration-*")):
        runs = []
        for grading in sorted(it.glob("*/*/grading.json")) + sorted(it.glob("*/*/*/grading.json")):
            s = json.loads(grading.read_text(encoding="utf-8"))["summary"]
            label = "/".join(grading.parts[len(it.parts):-1])
            runs.append(f"{label} {s['passed']}/{s['total']}"
                        + (f" (보류 {s['pending']})" if s["pending"] else ""))
        manual = "사람판정 있음" if (it / "manual_grades.json").exists() else "사람판정 없음"
        bench = " · benchmark 집계됨" if (it / "benchmark.json").exists() else ""
        lines.append(f"- **{it.name}** — {manual}{bench}")
        lines += [f"    - {r}" for r in runs]
    return lines


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workspaces", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, required=True, help="아카이브를 둘 디렉터리")
    ap.add_argument("--stamp", default=None,
                    help="아카이브 이름에 쓸 날짜(YYYYMMDD). 생략하면 오늘")
    args = ap.parse_args()

    stamp = args.stamp or datetime.now(timezone.utc).strftime("%Y%m%d")
    out = args.out.expanduser()
    out.mkdir(parents=True, exist_ok=True)

    present = [w for w in args.workspaces if w.is_dir()]
    missing = [w for w in args.workspaces if not w.is_dir()]
    for w in missing:
        print(f"  건너뜀: {w} 없음")
    if not present:
        raise SystemExit("보존할 워크스페이스가 없다.")

    tar_path = out / f"eval-evidence-{stamp}.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        for w in present:
            tar.add(w, arcname=w.name)

    body = [f"# eval 증거 아카이브 — {stamp}", "",
            f"`{tar_path.name}` ({tar_path.stat().st_size / 1e6:.1f} MB)", "",
            "산출물과 사람 판정을 **함께** 담았다. 판정은 `_digest` 로 산출물에 결속돼 있어",
            "따로 떼면 다시 적용할 수 없다.", ""]
    for w in present:
        body += [f"## {w.name}", f"`{w}`", ""] + summarise(w) + [""]
    body += ["---", "",
             "복원: `tar xzf " + tar_path.name + "` 한 뒤 원래 경로에 되돌린다.",
             "채점 재현: `python3 tools/skill-eval/grade.py <iteration> --rules <name> --require-complete`"]
    (out / f"MANIFEST-{stamp}.md").write_text("\n".join(body) + "\n", encoding="utf-8")

    # 읽히는지 실제로 확인한다 — 열리지 않는 아카이브는 백업이 아니다.
    with tarfile.open(tar_path) as tar:
        n = len(tar.getnames())
    print(f"  {tar_path}  ({tar_path.stat().st_size / 1e6:.1f} MB · 항목 {n}개, 읽기 확인)")
    print(f"  {out / f'MANIFEST-{stamp}.md'}")


if __name__ == "__main__":
    main()
