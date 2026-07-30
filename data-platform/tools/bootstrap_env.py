"""Give a fresh clone an `.env` that is allowed to install the published index.

The default embedder is `hashing`, in `.env.example` and in the code, and that is
a deliberate choice: it needs no model download, so `make build` works offline on
a machine that has never fetched anything. It also means a fresh clone's
`index_signature` does not match the published index, and `make fetch-index`
refuses it -- correctly, and confusingly, because nothing the user did was wrong.

This writes the three lines that resolve it, taking the values from
`index_release.json` rather than hardcoding them, so the file cannot drift from
whatever was actually published.

IT NEVER OVERWRITES AN EXISTING `.env`. That file holds secrets and local
choices; the whole point of it being git-ignored is that nothing in the repo owns
it. When one exists this only reports whether it agrees, and names the lines to
change if it does not.

WHAT IT DOES NOT DO IS VERIFY. Recomputing `index_signature` needs the embedder
loaded, which needs the model downloaded (`make warm-models`), which has not
happened yet at bootstrap time. So this sets values and `make fetch-index` proves
them a step later, refusing with both signatures side by side if they disagree.
Two checks of the same thing would be one check and one imitation of it.

    uv run python tools/bootstrap_env.py
    python3 tools/bootstrap_env.py --env /tmp/x/.env --example .env.example
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The lines this file owns. Everything else in .env.example is copied through --
# it is the only documentation of most knobs, and a stripped .env would cost the
# reader that.
KEYS = ("EMBEDDING_PROVIDER", "EMBEDDING_MODEL", "EMBEDDING_DIM")


def wanted(pointer: dict) -> dict:
    return {
        "EMBEDDING_PROVIDER": str(pointer.get("embedding_provider", "")),
        "EMBEDDING_MODEL": str(pointer.get("embedding_model", "")),
        "EMBEDDING_DIM": str(pointer.get("embedding_dim", "")),
    }


def read_settings(text: str) -> dict:
    """The uncommented assignments for the keys we care about."""
    found = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() in KEYS:
            found[key.strip()] = value.strip()
    return found


def rewrite(example: str, values: dict) -> str:
    """Replace the three assignments in place; comment out nothing, add nothing.

    A commented `# EMBEDDING_MODEL=...` line in the example is replaced by a real
    one, because a model is required once the provider is not `hashing` and a
    consumer should not have to notice that the line was commented.
    """
    remaining = dict(values)
    out = []
    for line in example.splitlines():
        stripped = line.strip()
        key = stripped.removeprefix("#").strip().partition("=")[0].strip()
        if key in remaining and (stripped.startswith(f"{key}=") or stripped.startswith(f"# {key}=") or stripped.startswith(f"#{key}=")):
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    # A key the example never mentioned still has to land somewhere.
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    parser.add_argument("--example", type=Path, default=ROOT / ".env.example")
    parser.add_argument("--pointer", type=Path, default=ROOT / "index_release.json")
    args = parser.parse_args(argv)

    if not args.pointer.exists():
        print(
            f"[env] no {args.pointer.name}: nothing has been published, so there is no embedder\n"
            "[env] to match. Leaving .env alone; `make build` works with the default."
        )
        return 0

    pointer = json.loads(args.pointer.read_text(encoding="utf-8"))
    values = wanted(pointer)
    missing = [key for key, value in values.items() if not value]
    if missing:
        print(
            f"[env] {args.pointer.name} does not record {', '.join(missing)} (an older publish).\n"
            "[env] Leaving .env alone; re-publish to record them."
        )
        return 0

    if args.env.exists():
        current = read_settings(args.env.read_text(encoding="utf-8"))
        wrong = {key: (current.get(key), value) for key, value in values.items() if current.get(key) != value}
        if not wrong:
            print(f"[env] {args.env.name} already names the published embedder.")
            return 0
        print(f"[env] {args.env.name} exists and is NOT overwritten -- it holds secrets and local choices.")
        print("[env] To install the published index, these lines have to match it:")
        for key, (have, want) in wrong.items():
            print(f"[env]   {key}={want}    (currently {have if have is not None else 'unset'})")
        print("[env] Or keep your settings and build your own index with `make build`.")
        return 1

    if not args.example.exists():
        raise SystemExit(f"[env] no {args.example} to copy from.")

    args.env.write_text(rewrite(args.example.read_text(encoding="utf-8"), values), encoding="utf-8")
    print(f"[env] wrote {args.env.name} from {args.example.name}, with the published embedder:")
    for key, value in values.items():
        print(f"[env]   {key}={value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
