#!/usr/bin/env python3
"""생성 산출물이 커밋으로 새지 않는지 본다. 인자로 준 루트를 훑는다.

    python3 tools/check_outputs.py gen-apps gen-docs/st_prospectus

왜 필요한가 — 이 저장소의 리모트는 공개다. `gen-apps/<slug>/` 와
`gen-docs/st_prospectus/<slug>/` 는 실행이 만들어 내는 자리이고, 거기 담기는 것은
발행사의 증권신고서 초안과 투자자 데이터다. 그런데 그 경로는 ignore 되지 않는다 —
`.gitignore` 는 발행사 폴더 **이름을 손으로 박아** 막고 있었고, 주석 스스로 이렇게
적어 두었다:

    Add a new output folder's pattern here.

즉 다음 발행사 산출물을 막는 것이 사람의 기억뿐이었다. 실제로 한 번 작동했다 —
개발 중간문서 두 건이 스킬 소스 옆에 추적된 채 공개 리모트로 올라갔다(우연히 무해한
내용이었을 뿐이다). push 된 커밋은 지워도 히스토리·포크·캐시에 남으니, 이 실수는
되돌릴 수 없는 쪽에 속한다. 그래서 기억이 아니라 게이트로 옮긴다.

무엇을 소스로 보는가 — **HEAD 가 이미 아는 항목**이다. `prompt-templates/` 처럼
SKILL.md 가 없는 소스 디렉터리도 여기서 걸리지 않고, 그 안의 파일을 새로 추가하는
평범한 작업도 막지 않는다. 판별을 파일명 규칙이 아니라 git 이 아는 사실에 두는 이유다.

무엇을 산출물로 보는가 — HEAD 가 모르고 ignore 도 되지 않은 새 항목이다.

  · **디렉터리** (SKILL.md 없음) → 실패. 스테이징했더라도 실패다. 산출물은 항상
    디렉터리로 떨어지고, `git add -A` 가 그것을 통째로 집어가는 것이 정확히 막아야
    할 사고다. 여기서 스테이징을 면죄부로 인정하면 게이트가 사고를 세탁해 준다.
  · **디렉터리** (SKILL.md 있음) → 새로 쓰는 중인 스킬이다. 알려만 준다.
  · **낱개 파일** → 스테이징했으면 통과(한 파일을 add 하는 것은 의도한 행위다),
    안 했으면 실패. 결정을 강요하는 것이 목적이다: 소스면 add, 산출물이면 ignore.

검사 대상은 `git add -A` 가 실제로 집어갈 것뿐이다(untracked + 스테이징된 신규).
ignore 된 것은 애초에 후보에 들어오지 않아, 의도대로 무시되는 산출물은 조용하다.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def git(*args: str, cwd: Path) -> str:
    """git 출력. 실패는 빈 문자열 — HEAD 가 없는 저장소(커밋 0개)도 통과해야 한다."""
    done = subprocess.run(
        ("git", *args), cwd=cwd, capture_output=True, text=True, check=False
    )
    return done.stdout if done.returncode == 0 else ""


def nul_list(raw: str) -> list[str]:
    """`-z` 출력을 자른다. 한글 경로가 이스케이프되지 않는 유일한 형식이다."""
    return [item for item in raw.split("\0") if item]


def repo_root() -> Path:
    top = git("rev-parse", "--show-toplevel", cwd=Path.cwd()).strip()
    if not top:
        sys.exit("FAIL: git 저장소가 아니라 산출물 유출을 검사할 수 없다.")
    return Path(top)


def head_entries(root: str, base: Path) -> set[str]:
    """HEAD 가 아는 `root` 바로 아래 항목 이름 = 소스."""
    listed = nul_list(git("ls-tree", "--name-only", "-z", "HEAD", f"{root}/", cwd=base))
    return {Path(name).name for name in listed}


def candidates(roots: list[str], base: Path) -> dict[str, str]:
    """`git add -A` 가 집어갈 경로 → 어떻게 집어가는지('untracked' | 'staged')."""
    found: dict[str, str] = {}
    for path in nul_list(
        git("ls-files", "--others", "--exclude-standard", "-z", "--", *roots, cwd=base)
    ):
        found[path] = "untracked"
    for path in nul_list(
        git(
            "diff", "--cached", "--name-only", "--diff-filter=A", "-z", "HEAD",
            "--", *roots, cwd=base,
        )
    ):
        found[path] = "staged"
    return found


def check(roots: list[str], base: Path) -> tuple[list[str], list[str], int]:
    """(실패 사유, 알림, 검사한 후보 파일 수)."""
    known = {root: head_entries(root, base) for root in roots}
    pending = candidates(roots, base)

    # 항목(루트 바로 아래 한 칸)별로 묶는다. dApp 하나가 500 파일이어도 한 줄로 말한다.
    grouped: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for path, how in sorted(pending.items()):
        for root in roots:
            if path == root or path.startswith(f"{root}/"):
                rest = path[len(root) + 1 :]
                if not rest:
                    break
                grouped.setdefault((root, rest.split("/")[0]), []).append((path, how))
                break

    failures: list[str] = []
    notes: list[str] = []
    for (root, segment), members in sorted(grouped.items()):
        if segment in known[root]:
            continue  # HEAD 가 아는 소스. 그 안에 파일을 더하는 것은 평범한 작업이다.
        entry = base / root / segment
        if entry.is_dir():
            if (entry / "SKILL.md").is_file():
                notes.append(f"{root}/{segment}/ — 새 스킬(SKILL.md 있음). 커밋 대상으로 본다")
                continue
            failures.append(
                f"{root}/{segment}/ — 추적되지 않고 ignore 되지도 않은 디렉터리"
                f" ({len(members)}개 파일). 생성 산출물이면 ignore 한다:\n"
                f"      .gitignore 에 →  {root}/{segment}/"
            )
            continue
        # 낱개 파일. 의도해서 스테이징한 것은 저자가 결정한 것으로 본다.
        path, how = members[0]
        if how == "staged":
            continue
        failures.append(
            f"{path} — 추적도 ignore 도 아닌 새 파일. 소스면 `git add`,"
            f" 산출물·작업메모면 ignore 한다:\n"
            f"      .gitignore 에 →  {path}"
        )
    return failures, notes, len(pending)


def main(argv: list[str]) -> int:
    roots = argv[1:]
    if not roots:
        print(f"usage: {argv[0]} <root> [<root> ...]", file=sys.stderr)
        return 2
    base = repo_root()
    live = [root for root in roots if (base / root).is_dir()]
    if not live:
        print(f"NOTE: 검사할 루트가 없다 ({', '.join(roots)}). 통과.")
        return 0

    failures, notes, seen = check(live, base)
    for note in notes:
        print(f"NOTE: {note}")
    if failures:
        print(f"FAIL: 커밋으로 새어 나갈 산출물 {len(failures)}건.", file=sys.stderr)
        for reason in failures:
            print(f"  · {reason}", file=sys.stderr)
        print(
            "\n  이 리모트는 공개다. 발행사 신고서 초안과 투자자 데이터가 여기로 떨어지고,\n"
            "  push 된 것은 지워도 히스토리에 남는다. 위 두 갈래 중 하나로 결정한다.",
            file=sys.stderr,
        )
        return 1
    print(f"OK: 루트 {len(live)}곳에 새는 산출물 없음 (후보 {seen}개 파일 검사)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
