"""The accelerator layer's invariants, checked WITHOUT a GPU, a model, or a build.

Everything here holds on a bare CI runner on all three platforms, which is the
point: the properties that break silently on a platform nobody develops on are
exactly the ones a hosted runner can hold the design to. Speed is not one of
them -- that is `make bench-ep`, on the machine that cares.

    uv run python tools/test_runtime_contract.py
"""

from __future__ import annotations

import os
import platform
import sys
import tempfile
from pathlib import Path

from pipeline import runtime
from pipeline.vector_cache import encode_with_cache, verify_sample

FAILURES: list = []


def check(description: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {description}")
        return
    FAILURES.append(f"{description}: {detail}" if detail else description)
    print(f"  FAIL {description}{': ' + detail if detail else ''}")


def _clear(**environment) -> None:
    """Set/unset env and drop the memoised selection, which is per process."""
    for key, value in environment.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    runtime.detect.cache_clear()


def test_selection() -> None:
    print("provider selection")
    system = platform.system()
    registered = set(runtime.available_providers())
    check("onnxruntime registers at least the CPU provider", runtime.CPU_EP in registered, str(registered))

    # Candidates are the platform's list, filtered by what is registered -- never
    # an EP this build does not have, because asking for one of those raises.
    candidates = runtime.candidate_providers()
    check(
        f"candidates for {system} are all registered",
        all(ep in registered for ep in candidates),
        f"{candidates} vs {sorted(registered)}",
    )
    check(
        "the CPU provider is never a GPU candidate",
        runtime.CPU_EP not in candidates,
        str(candidates),
    )

    _clear(DEVICE=None, ORT_PROVIDER=None, EMBEDDING_PRECISION=None)

    # THE default-preservation invariant: int8 is a CPU format. A machine growing
    # a GPU must not silently change the vectors the build produces.
    profile = runtime.detect("int8")
    check(
        "int8 stays on the CPU even where a GPU provider exists",
        profile.provider == runtime.CPU_EP,
        profile.describe(),
    )
    check("int8 profile reports the int8 asset", profile.precision == "int8")

    # Opting in must actually reach the GPU where there is one that is CHOSEN
    # automatically. A provider on the opt-in list is there because it was
    # measured to crash or to lose to the CPU, so it must NOT be picked here.
    auto = [ep for ep in candidates if ep not in runtime._OPT_IN_ONLY]  # noqa: SLF001
    fp16 = runtime.detect("fp16")
    if auto:
        check(
            "fp16 selects the platform's first auto-selectable GPU provider",
            fp16.provider == auto[0],
            fp16.describe(),
        )
    else:
        check(
            "fp16 falls back to the CPU when no auto-selectable GPU provider exists",
            fp16.provider == runtime.CPU_EP,
            fp16.describe(),
        )
    check(
        "an opt-in-only provider is never selected automatically",
        fp16.provider not in runtime._OPT_IN_ONLY,  # noqa: SLF001
        fp16.describe(),
    )

    _clear(DEVICE="cpu")
    check("DEVICE=cpu overrides everything", runtime.detect("fp16").provider == runtime.CPU_EP)

    _clear(DEVICE=None, ORT_PROVIDER="DefinitelyNotAnExecutionProvider")
    try:
        runtime.detect("int8")
        check("an unregistered ORT_PROVIDER is rejected", False, "no error raised")
    except RuntimeError as error:
        check("an unregistered ORT_PROVIDER is rejected", "not registered" in str(error))
    _clear(ORT_PROVIDER=None)

    check("CPU wants int8", runtime.precision_for(runtime.CPU_EP) == "int8")
    for gpu_ep in ("CUDAExecutionProvider", "DmlExecutionProvider", "CoreMLExecutionProvider"):
        check(f"{gpu_ep} wants fp16", runtime.precision_for(gpu_ep) == "fp16")

    # DirectML cannot run with the memory-pattern optimiser or parallel execution;
    # getting this wrong fails only on Windows with a DX12 GPU, i.e. nowhere that
    # anyone tests by accident.
    options = runtime._session_options("DmlExecutionProvider", 1)  # noqa: SLF001
    check("DirectML sessions disable the memory pattern optimiser", options.enable_mem_pattern is False)


def test_profile_cache() -> None:
    print("device profile")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "device_profile.json"
        profile = runtime.detect("int8")
        runtime.save_profile(path, profile, {"encode_split": [3, 1]})
        loaded = runtime.load_profile(path)
        check("a profile written here is read back here", loaded is not None)
        check(
            "measurements survive the round trip",
            (loaded or {}).get("measurements", {}).get("encode_split") == [3, 1],
        )

        payload = path.read_text(encoding="utf-8").replace(
            runtime.machine_fingerprint(), "a different machine"
        )
        path.write_text(payload, encoding="utf-8")
        check(
            "a profile measured on another machine is ignored",
            runtime.load_profile(path) is None,
        )

        path.write_text("{not json", encoding="utf-8")
        check("a corrupt profile is ignored rather than raising", runtime.load_profile(path) is None)


class _FakeEmbedder:
    """Deterministic, model-free stand-in: the cache's contract is about keys."""

    name = "fake"
    model_name = "fake-model"
    precision = "int8"
    dimensions = 4

    def __init__(self):
        self.encoded = 0

    def encode(self, texts):
        self.encoded += len(texts)
        return [[float(len(text)), 1.0, 2.0, 3.0] for text in texts]


def test_vector_cache() -> None:
    print("vector cache")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "vector_cache.sqlite"
        embedder = _FakeEmbedder()
        # Distinct lengths on purpose: the fake encodes length, so the assembled
        # order is only checkable if the passages are distinguishable by it.
        texts = ["가상자산", "토큰증권 발행인", "가상자산", "전매"]

        first, stats = encode_with_cache(embedder, texts, path, 4)
        check("a repeated passage is encoded once", embedder.encoded == 3, str(stats))

        second, stats = encode_with_cache(embedder, texts, path, 4)
        check("the second pass encodes nothing", stats["encoded"] == 0, str(stats))
        check("cached vectors equal freshly encoded ones", first == second)
        check(
            "order is preserved across the cache",
            [vector[0] for vector in second] == [4.0, 8.0, 4.0, 2.0],
            str([vector[0] for vector in second]),
        )

        _, stats = encode_with_cache(embedder, ["토큰증권 발행인"], path, 4)
        check("passages that left the corpus are pruned", stats["pruned"] == 2, str(stats))

        # A different vector space must never be served from the same cache.
        other = _FakeEmbedder()
        other.precision = "fp16"
        before = other.encoded
        encode_with_cache(other, ["토큰증권 발행인"], path, 4)
        check("changing the asset invalidates the cache", other.encoded > before)

        checked, worst = verify_sample(other, path, 4, ["토큰증권 발행인"])
        check("the sample verifier agrees with the cache", checked == 1 and worst > 0.999999)

        # The cache is an optimisation, never a dependency.
        os.environ["EMBED_CACHE"] = "0"
        try:
            third = _FakeEmbedder()
            vectors, stats = encode_with_cache(third, texts, path, 4)
            check("EMBED_CACHE=0 bypasses the cache", stats["cache"] == "off" and vectors == first)
        finally:
            os.environ.pop("EMBED_CACHE", None)

        broken = Path(directory) / "broken.sqlite"
        broken.write_bytes(b"this is not a database")
        fourth = _FakeEmbedder()
        vectors, stats = encode_with_cache(fourth, texts, broken, 4)
        check(
            "an unusable cache file degrades to plain encoding",
            stats["cache"] == "error" and len(vectors) == len(texts),
            str(stats),
        )


def test_settings_validation() -> None:
    print("settings")
    from pipeline import get_settings

    os.environ["EMBEDDING_PRECISION"] = "float16"
    try:
        get_settings()
        check("a bad EMBEDDING_PRECISION fails on read", False, "no error raised")
    except ValueError as error:
        check("a bad EMBEDDING_PRECISION fails on read", "EMBEDDING_PRECISION" in str(error))
    finally:
        os.environ.pop("EMBEDDING_PRECISION", None)

    check("the default asset is int8", get_settings().embedding_precision == "int8")


def main() -> int:
    print(f"runtime contract: {platform.system()} {platform.machine()}, wheel {runtime.installed_wheel()}\n")
    test_selection()
    print()
    test_profile_cache()
    print()
    test_vector_cache()
    print()
    test_settings_validation()
    print()
    for problem in runtime.preflight():
        print(f"note: {problem}")
    if FAILURES:
        print(f"\nFAILED ({len(FAILURES)}):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("all runtime contract checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
