#!/usr/bin/env python3
"""정본 `.agents/skills/` 를 벤더 어댑터 디렉터리(`.claude/skills/`)로 펼치고, 검증한다.

왜 생성물인가 — `.claude/skills/` 는 Claude Code 가 읽는 경로일 뿐, 내용이 없다.
그런데 커밋하면 두 가지를 치른다: 벤더 이름이 형상관리에 남고, git 이 심링크를
나르는 방식이 Windows 에서 깨진다(`core.symlinks=false` 로 클론하면 링크가 경로
문자열이 담긴 **텍스트 파일**로 떨어지고, 스킬은 오류 없이 그냥 로드되지 않는다).
그래서 추적하는 것은 `.agents/skills/` 뿐이고 어댑터는 여기서 만든다 —
`make hooks` 와 같은 성격의 "clone 마다 한 번" 항목이다.

--check 는 어댑터가 **없으면 통과한다.** 생성물의 부재는 규약 위반이 아니라 아직
만들지 않은 상태이고, 여기서 실패시키면 `.githooks/pre-commit` 이 `make check` 를
부르는 이 저장소에서 clone 직후 첫 커밋이 막힌다. 실패는 **있는데 어긋난 경우**다.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path

CANON = Path(".agents") / "skills"
ADAPTERS = (Path(".claude") / "skills",)
# 어댑터에서 정본으로 돌아가는 상대 경로. `<base>/.claude/skills/<name>` 기준이라
# 두 단계를 올라가면 `<base>` 다. 절대 경로를 쓰지 않는 이유는 클론 위치가 사람마다
# 다르기 때문이고, 그래서 링크는 옮겨 심어도 살아 있다.
BACK = Path("..") / ".."


def skill_names(canon: Path) -> list[str]:
    """정본에서 스킬로 인정되는 것 = SKILL.md 를 가진 하위 디렉터리.

    정본 자체가 심링크일 수 있어(루트의 sto-filing 은 gen-docs 를 가리킨다)
    `is_dir()` 로 판정한다 — 심링크를 따라가 준다.
    """
    if not canon.is_dir():
        return []
    return sorted(
        entry.name
        for entry in canon.iterdir()
        if entry.is_dir() and (entry / "SKILL.md").is_file()
    )


def tree_digest(root: Path) -> str:
    """디렉터리 내용의 지문. 복사본이 정본과 갈라졌는지 보는 유일한 방법이다.

    심링크로 만들어진 어댑터라면 경로 동일성만으로 판정이 끝나므로 여기까지 오지
    않는다. 이 함수가 필요한 것은 심링크를 만들 수 없어 복사로 떨어진 경우 뿐이고,
    복사는 편집되면 조용히 드리프트한다 — 심링크였다면 불가능했던 사고다.
    """
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def link_ok(adapter_entry: Path, canon_entry: Path) -> tuple[bool, str]:
    """어댑터 항목이 정본과 같은 것을 가리키는가."""
    if not adapter_entry.exists():
        return False, "없음 (깨진 링크이거나 미생성)"
    try:
        same = adapter_entry.resolve() == canon_entry.resolve()
    except OSError as error:                              # noqa: BLE001 - 경로 해석 실패도 불일치다
        return False, f"경로 해석 실패: {error}"
    if same:
        return True, "심링크"
    if adapter_entry.is_dir():
        if tree_digest(adapter_entry) == tree_digest(canon_entry):
            return True, "복사본 (내용 일치)"
        return False, "복사본이 정본과 다름 — `make skills` 로 다시 만든다"
    # Windows 에서 core.symlinks=false 로 클론하면 링크가 경로 문자열이 담긴 텍스트
    # 파일이 된다. 스킬은 오류 없이 로드되지 않으므로, 여기서 이름을 붙여 준다.
    return False, "디렉터리가 아님 — Windows 의 core.symlinks=false 클론일 수 있다"


def check(base: Path) -> tuple[int, int]:
    """(실패 수, 검사한 어댑터 수). 어댑터가 아예 없으면 통과시킨다(모듈 docstring)."""
    canon = base / CANON
    names = skill_names(canon)
    failed = checked = 0
    for adapter in ADAPTERS:
        target = base / adapter
        if not target.is_dir():
            if names:
                print(f"  NOTE {target} 없음 — `make skills` 로 만든다 (스킬 {len(names)}개)")
            continue
        checked += 1
        present = sorted(entry.name for entry in target.iterdir())
        for name in names:
            entry = target / name
            ok, why = link_ok(entry, canon / name)
            if not ok:
                print(f"  FAIL {entry}: {why}")
                failed += 1
            elif not (entry / "SKILL.md").is_file():
                # 이중 심링크(어댑터 → 정본 → gen-apps)를 실제로 열어 본다. 경로가
                # 맞아도 중간 어디가 끊기면 스킬은 로드되지 않는다.
                print(f"  FAIL {entry}: SKILL.md 를 열 수 없다 ({why})")
                failed += 1
        for orphan in set(present) - set(names):
            kind = "심링크" if (target / orphan).is_symlink() else "디렉터리"
            print(f"  FAIL {target / orphan}: 정본에 없는 {kind} — `make skills` 가 정리한다")
            failed += 1
    return failed, checked


def symlinks_work(directory: Path) -> bool:
    """이 디렉터리에서 심링크를 만들 수 있는가. 한 번만 물어보고 재사용한다.

    복사본은 `--check` 를 통과하는 한 영원히 복사본으로 남는다 — 내용이 같으면
    link_ok 가 참이기 때문이다. 그러면 심링크를 쓸 수 있는 기계에서도 드리프트할 수
    있는 형태가 유지되고, 실제로 그렇게 됐다: 폴백 경로를 시험한 뒤 남은 복사본이
    정본을 다음에 고칠 때까지 조용히 기다렸다가 게이트를 깨웠다. 만들 수 있으면
    올려붙인다.
    """
    probe = directory / ".symlink-probe"
    try:
        probe.symlink_to(Path("."))
    except (OSError, NotImplementedError):
        return False
    finally:
        if probe.is_symlink():
            probe.unlink()
    return True


def sync(base: Path) -> int:
    """어댑터를 정본에 맞춘다. 반환값은 손댄 항목 수."""
    canon = base / CANON
    names = skill_names(canon)
    if not names:
        return 0
    touched = 0
    for adapter in ADAPTERS:
        target = base / adapter
        target.mkdir(parents=True, exist_ok=True)
        linkable = symlinks_work(target)
        for name in names:
            entry = target / name
            wanted = BACK / CANON / name
            ok, _ = link_ok(entry, canon / name)
            # 심링크를 쓸 수 있는데 복사본이면, 내용이 같아도 다시 심는다.
            if ok and linkable and not entry.is_symlink():
                ok = False
            # 해석 결과가 같아도 링크 **문자열**이 다르면 다시 심는다. 루트의 세 스킬은
            # 정본이 다시 심링크라(.agents/skills/sto-filing -> gen-docs/...) 어댑터가
            # 정본을 건너뛰고 최종 대상을 직접 가리켜도 resolve() 는 같은 곳에 닿는다.
            # 그러면 게이트는 통과하지만 토폴로지가 머신마다 갈라지고, 정본을 옮겼을 때
            # 어댑터가 따라오는지가 운에 맡겨진다. 생성기는 항상 정본을 경유시킨다.
            # 문자열로 비교한다. Path("a/") == Path("a") 라서 Path 비교는 후행 슬래시
            # 같은 표기 차이를 통과시키고, 그러면 링크가 어디서 만들어졌느냐에 따라
            # 모양이 달라진 채 남는다. 생성기의 출력은 바이트 단위로 결정적이어야 한다.
            if ok and (not entry.is_symlink() or os.readlink(entry) == str(wanted)):
                continue
            if entry.is_symlink() or entry.exists():
                shutil.rmtree(entry) if entry.is_dir() and not entry.is_symlink() else entry.unlink()
            try:
                entry.symlink_to(wanted, target_is_directory=True)
            except (OSError, NotImplementedError):
                # Windows: 개발자 모드나 관리자 권한이 없으면 심링크를 못 만든다.
                # 복사로 떨어뜨리되 --check 가 내용 해시로 드리프트를 감시한다.
                shutil.copytree(canon / name, entry, symlinks=False)
            touched += 1
            print(f"  + {entry} -> {wanted}")
        for orphan in sorted({e.name for e in target.iterdir()} - set(names)):
            stale = target / orphan
            if stale.is_symlink():
                stale.unlink()
                touched += 1
                print(f"  - {stale} (정본에 없음)")
            else:
                # 사용자가 직접 만든 개인 스킬일 수 있다. 지우지 않는다.
                print(f"  ! {stale} 는 정본에 없지만 실제 디렉터리라 두었다 — 필요 없으면 직접 지운다")
    return touched


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--check"]
    checking = "--check" in sys.argv[1:]
    bases = [Path(a) for a in args] or [Path(".")]

    if checking:
        failed = checked = 0
        for base in bases:
            f, c = check(base)
            failed += f
            checked += c
        if failed:
            sys.exit(f"스킬 어댑터 검사 실패 {failed}건")
        print(f"OK (어댑터 {checked}개 디렉터리{'' if checked else ' — 아직 없음'})")
        return

    touched = sum(sync(base) for base in bases)
    print(f"OK (변경 {touched}건)" if touched else "OK (이미 최신)")


if __name__ == "__main__":
    os.umask(os.umask(0o022))  # 링크 권한이 사람마다 달라지지 않게 한다
    main()
