#!/usr/bin/env python3
"""증권신고서 프롬프트 3종을 스킬 정본에서 생성한다.

정본은 언제나 `sto-filing/`(SKILL.md + references/)과 `prompt-templates/`다.
`dist/`는 생성물이며 **손으로 고치지 않는다.** 고치면 다음 생성에서 덮어써진다.

    python3 build_prompts.py            # 생성
    python3 build_prompts.py --check    # 최신 여부만 확인 (CI/커밋 전)

출력은 결정적이다 — 소스가 같으면 결과가 바이트 단위로 같다. 타임스탬프를 넣지 않고
소스 파일의 해시를 기록하므로, git diff 가 실제 변경만 보여준다.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path


class BuildError(Exception):
    """소스가 없거나 치환이 끝나지 않았을 때. main 에서 잡아 종료코드로 바꾼다."""


ROOT = Path(__file__).resolve().parent
SKILL = ROOT / "sto-filing"
REFS = SKILL / "references"
TPL = ROOT / "prompt-templates"
DIST = ROOT / "dist"

GATE_INVESTMENT = (
    "투자계약증권은 사전 인허가가 필요 없다. 증권신고서만 제출하면 된다.\n"
    "> 다만 **집합투자 재분류**(풀링·운용재량·자산교체·매각대금 재투자)와\n"
    "> **채무증권 재분류**(사실상 원금보장, 매도인 재매입 의무·권리)를 계속 경계한다."
)

GATE_TRUST = (
    "**혁신금융서비스 지정과 신탁업 인가 수탁자가 모두 확보된 경우에만 진행한다.**\n"
    "> 자본시장법 §110은 금전신탁 수익증권을 전제하므로, 둘 중 하나라도 없으면\n"
    "> 발행 자체가 불가능하다. 그 경우 `CLASSIFY_PROMPT.md`의 게이트로 돌아간다."
)

BUILDS = [
    {
        "out": "CLASSIFY_PROMPT.md",
        "parts": [
            TPL / "classify.head.md",
            REFS / "classification.md",
            REFS / "sources.md",
            TPL / "classify.tail.md",
        ],
        "vars": {},
    },
    {
        "out": "PROSPECTUS_PROMPT_투자계약증권.md",
        "parts": [
            TPL / "draft.head.md",
            REFS / "sources.md",
            REFS / "reference-filings.md",
            REFS / "common-core.md",
            REFS / "investment-contract.md",
            TPL / "draft.tail.md",
        ],
        "vars": {"SECURITY_TYPE": "투자계약증권", "TYPE_GATE": GATE_INVESTMENT},
    },
    {
        "out": "PROSPECTUS_PROMPT_신탁수익증권.md",
        "parts": [
            TPL / "draft.head.md",
            REFS / "sources.md",
            REFS / "reference-filings.md",
            REFS / "common-core.md",
            REFS / "trust-beneficiary.md",
            TPL / "draft.tail.md",
        ],
        "vars": {"SECURITY_TYPE": "비금전신탁 수익증권", "TYPE_GATE": GATE_TRUST},
    },
]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def demote(text: str, levels: int = 1) -> str:
    """참조 파일은 자체 문서로 쓰여 `# 제목`으로 시작한다.
    합칠 때 한 단계 낮춰 프롬프트의 절 계층과 충돌하지 않게 한다.
    코드펜스 안의 `#` 는 건드리지 않는다.

    markdown 은 H6 이 최대다. 넘치면 H6 으로 고정한다 — 강등하지 않고 두면
    부모(H1→H2)와 계층이 역전된다."""
    out, in_fence = [], False
    for line in text.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            out.append(line)
            continue
        if not in_fence and stripped.startswith("#"):
            hashes = len(stripped) - len(stripped.lstrip("#"))
            rest = stripped[hashes:]
            if rest.startswith(" ") or rest == "":
                indent = line[: len(line) - len(stripped)]
                out.append(indent + "#" * min(hashes + levels, 6) + rest)
                continue
        out.append(line)
    return "\n".join(out)


def render(build: dict) -> str:
    missing = [p for p in build["parts"] if not p.exists()]
    if missing:
        raise BuildError(f"소스 없음: {', '.join(str(m) for m in missing)}")

    manifest = "\n".join(
        f"> - `{p.relative_to(ROOT)}` ({sha(p)})" for p in build["parts"]
    )
    header = (
        "<!-- 생성 파일 — 손으로 고치지 마십시오. -->\n"
        "<!-- 정본: sto-filing/ + prompt-templates/  ·  재생성: python3 build_prompts.py -->\n\n"
        "> [!NOTE]\n"
        "> **이 파일은 생성물입니다.** 내용을 바꾸려면 아래 소스를 고치고 재생성하십시오.\n"
        "> 여기서 직접 수정하면 다음 생성에서 사라집니다.\n"
        f"{manifest}\n\n---\n\n"
    )

    chunks = []
    for i, part in enumerate(build["parts"]):
        text = part.read_text(encoding="utf-8").strip()
        # 템플릿(head/tail)은 프롬프트 계층 그대로, 참조 파일은 한 단계 낮춘다.
        if part.parent == REFS:
            text = demote(text, 1)
        chunks.append(text)
        if i < len(build["parts"]) - 1:
            chunks.append("\n---\n")

    body = header + "\n".join(chunks) + "\n"
    for key, val in build["vars"].items():
        body = body.replace("{{" + key + "}}", val)

    if "{{" in body:
        leftover = {s.split("}}")[0] for s in body.split("{{")[1:]}
        raise BuildError(f"치환되지 않은 토큰: {sorted(leftover)}")
    return body


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="최신 여부만 확인하고 아무것도 쓰지 않는다")
    args = ap.parse_args()

    # --check 는 부작용이 없어야 한다. 생성 모드에서만 디렉터리를 만든다.
    if not args.check:
        DIST.mkdir(exist_ok=True)

    stale = []
    for build in BUILDS:
        target = DIST / build["out"]
        new = render(build)
        old = target.read_text(encoding="utf-8") if target.exists() else None

        if args.check:
            if old != new:
                stale.append(build["out"] if target.exists() else f"{build['out']} (없음)")
            continue

        if old == new:
            print(f"  = {build['out']:<40} 변경 없음")
        else:
            target.write_text(new, encoding="utf-8")
            lines = new.count("\n") + 1
            print(f"  ✓ {build['out']:<40} {lines:>5}줄  {len(new.encode()):>7} bytes")

    if not args.check:
        return 0

    if stale:
        print("dist/ 가 오래되었습니다: " + ", ".join(stale))
        print("  python3 build_prompts.py")
        return 1

    print("dist/ 최신입니다.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BuildError as exc:
        print(f"빌드 실패: {exc}", file=sys.stderr)
        sys.exit(2)
