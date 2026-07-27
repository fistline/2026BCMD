#!/usr/bin/env python3
"""SKILL.md 프론트매터를 **YAML 파서로** 검증한다. 인자로 준 스킬 루트를 훑는다.

    python3 tools/check_skills.py <skills-root> [<skills-root> ...]

왜 파서인가 — 이전에는 `grep -q '^name:'` 로 확인했는데, 실제로 스킬을 망가뜨린 버그가
그 검사를 그대로 통과했다.

    description: 예: 예치금 분리보관        # 따옴표 없는 콜론

grep 은 통과시키고 YAML 파서는 ScannerError 를 낸다. 그 상태의 스킬은 로드되지 않아
자동 트리거가 죽는데, 게이트는 초록불이었다. 검사가 잡아야 할 바로 그것을 못 잡으면
게이트가 아니라 장식이다.

`head -8` 로 앞부분만 보던 것도 함께 고쳤다 — description 이 블록 스칼라로 길어지면
필드가 8줄 밖으로 밀려난다.

검사 항목(Agent Skills 개방 표준):
  · 프론트매터가 `---` 로 열고 닫히며 YAML 로 파싱된다
  · `name` 이 있고, **디렉터리명과 같다** (표준 요구사항이자 로더가 매칭하는 키)
  · `name` 이 소문자·숫자·하이픈, 64자 이하
  · `description` 이 있고 비어 있지 않으며 1024자 이하
심링크로 미러된 스킬은 같은 실체이므로 한 번만 본다.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # 조용히 통과시키지 않는다 — 검사 못 한 것과 통과는 다르다
    sys.exit("FAIL: PyYAML 이 없어 프론트매터를 검사할 수 없다. `uv run` 으로 돌리거나 pip install pyyaml.")

NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def check(skill_md: Path) -> list[str]:
    """한 SKILL.md 의 문제 목록. 비어 있으면 통과."""
    text = skill_md.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return ["프론트매터가 '---' 로 시작하지 않는다"]
    parts = text.split("---", 2)
    if len(parts) < 3:
        return ["프론트매터가 '---' 로 닫히지 않는다"]

    try:
        fm = yaml.safe_load(parts[1])
    except yaml.YAMLError as e:
        line = getattr(getattr(e, "problem_mark", None), "line", None)
        where = f" (프론트매터 {line + 1}행 부근)" if line is not None else ""
        # 이 자리에서 가장 흔한 원인을 같이 알려준다 — 안 그러면 원문을 눈으로 훑게 된다
        return [f"YAML 파싱 실패{where}: {e.__class__.__name__}. "
                f"값에 콜론·따옴표가 있으면 '>-' 블록 스칼라로 감싼다"]
    if not isinstance(fm, dict):
        return ["프론트매터가 매핑(key: value)이 아니다"]

    problems = []
    name, dirname = fm.get("name"), skill_md.parent.name
    if not name:
        problems.append("name 이 없다")
    else:
        if name != dirname:
            problems.append(f"name({name!r}) 이 디렉터리명({dirname!r})과 다르다")
        if not NAME_RE.match(str(name)):
            problems.append(f"name({name!r}) 은 소문자·숫자·하이픈만 쓴다")
        if len(str(name)) > 64:
            problems.append(f"name 이 64자를 넘는다 ({len(str(name))})")

    desc = fm.get("description")
    if not desc or not str(desc).strip():
        problems.append("description 이 없거나 비어 있다")
    elif len(str(desc)) > 1024:
        problems.append(f"description 이 1024자를 넘는다 ({len(str(desc))})")
    return problems


def main() -> None:
    roots = [Path(a) for a in sys.argv[1:]]
    if not roots:
        sys.exit(__doc__)

    seen, failed, checked = set(), 0, 0
    for root in roots:
        if not root.is_dir():
            print(f"  (건너뜀: {root} 없음)")
            continue
        for skill_md in sorted(root.glob("*/SKILL.md")):
            key = skill_md.resolve()
            if key in seen:      # 심링크 미러는 같은 실체다
                continue
            seen.add(key)
            checked += 1
            for p in check(skill_md):
                print(f"  FAIL {skill_md}: {p}")
                failed += 1

    if failed:
        sys.exit(f"프론트매터 검사 실패 {failed}건 / 스킬 {checked}개")
    print(f"OK ({checked}개 스킬)")


if __name__ == "__main__":
    main()
