"""Execution-provider selection: one place that decides WHERE an ONNX model runs.

Every ONNX model in this repo (the embedder, the reranker, and the optional ONNX
OCR backend) builds its session here, so "does this machine have a usable GPU?"
is answered once, the same way, on Windows, macOS and Linux.

Three rules the design turns on, each learned from a measurement or a documented
failure rather than assumed:

  1. THE EP DECIDES THE ASSET, AND A GPU IS NOT ALWAYS THE ANSWER.
     int8 is a CPU format. On a GPU it needs Tensor-Core int8 plus a TensorRT
     calibration to pay off, and without them it is *slower* than the CPU --
     measured here on Apple CoreML with the int8 bge-m3 graph: 1775 nodes split
     into 148 CoreML partitions, 869 ms/chunk against 517 ms/chunk on one CPU
     thread, and peak RSS 13.5 GB. So a GPU is selected only when a GPU-shaped
     asset (fp16) is available for it; otherwise this returns CPU on purpose.

  2. A GPU PROBLEM IS NEVER A BUILD FAILURE.
     Requesting an EP that onnxruntime did not register raises, so the candidate
     list is always intersected with `get_available_providers()`, provider
     options are applied defensively (an option key an older build rejects must
     not take the session down with it), and every failure path ends at CPU with
     a warning rather than an exception.

  3. THE WHEELS ARE MUTUALLY EXCLUSIVE.
     `onnxruntime`, `onnxruntime-gpu`, `onnxruntime-directml` and
     `onnxruntime-openvino` all import as `onnxruntime` and must never be
     installed together. That failure is silent and total -- the wrong wheel on a
     box with no CUDA can take out the *default* pipeline, not just the GPU path
     -- so `preflight()` reports which wheel is actually installed and what it
     did or did not register.

Determinism. The bit-stable-rebuild guarantee was always "on this machine";
it is now "on this machine with this device profile". Within a profile the
options pinned below keep a rerun identical (cuDNN algorithm search fixed to
HEURISTIC rather than the default exhaustive auto-tune, TF32 off where the build
accepts the flag, sequential execution for DirectML). Across different hardware
the contract is weaker and explicit: the same asset on a different EP must agree
to a tolerance (cosine >= 0.9999 and identical top-10), which
`tools/check_ep_equivalence.py` asserts.

Knobs (all optional, all default to today's behaviour):
    DEVICE=auto|cpu|gpu     policy. `auto` uses a GPU only when rule 1 allows.
    ORT_PROVIDER=<EPName>   force one EP. Fails loudly if it is not registered.
    ORT_THREADS=<n>         intra-op threads for the CPU EP.
    EMBEDDING_PRECISION     int8|fp16|auto (see pipeline/build_rag.py).
"""

from __future__ import annotations

import json
import os
import platform
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from functools import cache
from pathlib import Path

CPU_EP = "CPUExecutionProvider"

# Ordered candidates per platform. Highest first; each is kept only if
# onnxruntime actually registered it. TensorRT and OpenVINO are opt-in (they
# need a per-model engine build / calibration step this pipeline does not do
# yet), so they are listed but gated behind ORT_PROVIDER.
_ORDER = {
    "Linux": ("CUDAExecutionProvider", "ROCMExecutionProvider", "MIGraphXExecutionProvider"),
    "Windows": ("CUDAExecutionProvider", "DmlExecutionProvider"),
    "Darwin": ("CoreMLExecutionProvider",),
}
_OPT_IN_ONLY = frozenset(
    {
        "TensorrtExecutionProvider",
        "OpenVINOExecutionProvider",
        "WebGpuExecutionProvider",
        # CoreML is opt-in on EVIDENCE, not on principle. Three independent
        # measurements on this repo's models (2026-07-27, macOS 15 / M4 Pro,
        # onnxruntime 1.27.0), all negative:
        #   * bge-m3 int8  -- 148 partitions, 869 ms/chunk vs 517 on one CPU
        #                     thread, 13.5 GB peak, SIGKILL at 32 chunks
        #   * bge-m3 fp16  -- 149 partitions, SIGSEGV on real chunk lengths
        #   * PP-OCRv5     -- cannot bound the dynamic input shape; one exception
        #                     per op and 18x slower (2.1 s -> 38.2 s a page)
        # A native crash cannot be caught in-process, so "always fall back to the
        # CPU" cannot protect a build here -- the only protection is not choosing
        # it. `ORT_PROVIDER=CoreMLExecutionProvider` still forces it for anyone
        # measuring a different model on newer hardware.
        "CoreMLExecutionProvider",
    }
)

# EPs that want the fp16 asset. Everything else gets int8.
#
# This is a two-way split and the axis will eventually need four. Do NOT add an
# NPU provider (QNN, VitisAI, OpenVINO-with-NPU) to this set: an NPU is an
# integer engine and wants int8, so "accelerator therefore fp16" is wrong for it
# -- it needs its own class, alongside a fourth for GENERATIVE stages, whose
# asset rule reaches into KV-cache dtype and whose runtime is ONNX Runtime GenAI
# rather than plain onnxruntime.
#
# The generative class is a reserved slot, not an oversight (evaluated
# 2026-07-27, deliberately not adopted): nothing in this repo decodes
# autoregressively -- the embedder is an encoder, the reranker a cross-encoder,
# PP-OCRv5 a CNN+CRNN, and the one generative model (PaddleOCR-VL) has no ONNX
# export. Adopting it now would also triple the wheel matrix, because each
# onnxruntime-genai variant pins a DIFFERENT onnxruntime distribution and would
# reopen the mutual-exclusion hole `[tool.uv] conflicts` exists to close.
# Rationale and the trigger for filling it in: docs/plans/heterogeneous-device-roles.md
_FP16_EPS = frozenset(
    {
        "CUDAExecutionProvider",
        "TensorrtExecutionProvider",
        "DmlExecutionProvider",
        "ROCMExecutionProvider",
        "MIGraphXExecutionProvider",
        "CoreMLExecutionProvider",
        "WebGpuExecutionProvider",
    }
)

# Distributions that all install the same `onnxruntime` module.
_WHEELS = (
    "onnxruntime",
    "onnxruntime-gpu",
    "onnxruntime-directml",
    "onnxruntime-openvino",
    "onnxruntime-rocm",
    "onnxruntime-qnn",
)


@dataclass(frozen=True)
class DeviceProfile:
    """What was chosen, what it costs, and why -- recorded, not guessed."""

    provider: str
    provider_options: dict = field(default_factory=dict)
    precision: str = "int8"
    batch: int = 16
    reason: str = ""
    wheel: str = "unknown"
    ort_version: str = ""
    available: tuple = ()

    @property
    def is_gpu(self) -> bool:
        return self.provider != CPU_EP

    def describe(self) -> str:
        return f"{self.provider} / {self.precision} (batch {self.batch}) -- {self.reason}"


# --------------------------------------------------------------------------
# Introspection
# --------------------------------------------------------------------------
def _import_ort():
    try:
        import onnxruntime as ort
    except ImportError as error:  # pragma: no cover - exercised by the preflight path
        raise RuntimeError(
            "onnxruntime is not installed. The ONNX providers need the onnx-embed "
            "extra: uv sync --extra onnx-embed"
        ) from error
    return ort


@cache
def installed_wheel() -> str:
    """Which onnxruntime distribution provides the module, per package metadata.

    They cannot be told apart by `import onnxruntime`, and installing two of them
    leaves whichever unpacked last -- so this reports every one it finds and the
    caller treats more than one as a fault.
    """
    from importlib.metadata import PackageNotFoundError, version

    found = []
    for name in _WHEELS:
        try:
            found.append(f"{name}=={version(name)}")
        except PackageNotFoundError:
            continue
    return ", ".join(found) if found else "unknown"


@cache
def available_providers() -> tuple:
    try:
        return tuple(_import_ort().get_available_providers())
    except RuntimeError:
        return ()


def candidate_providers(system: str | None = None) -> tuple:
    """GPU EPs worth trying on this platform, in priority order, that exist here."""
    system = system or platform.system()
    registered = set(available_providers())
    return tuple(ep for ep in _ORDER.get(system, ()) if ep in registered)


def precision_for(provider: str) -> str:
    """The asset an EP wants. Rule 1: GPU means fp16, CPU means int8."""
    return "fp16" if provider in _FP16_EPS else "int8"


def gpu_provider() -> str | None:
    """The first registered GPU EP, ignoring the asset rule. None if there is none.

    `detect()` refuses a GPU when the asset is int8, because for THIS repo's
    bge-m3 assets that was measured to be slower than the CPU. A component that
    brings its own GPU-shaped models -- the ONNX OCR backend, whose PP-OCRv5
    graphs are plain fp32 -- needs the unconditional answer instead.
    """
    if _policy() == "cpu":
        return None
    forced = os.environ.get("ORT_PROVIDER", "").strip()
    if forced:
        return forced if forced in available_providers() and forced != CPU_EP else None
    candidates = [ep for ep in candidate_providers() if ep not in _OPT_IN_ONLY]
    return candidates[0] if candidates else None


# --------------------------------------------------------------------------
# Provider options -- determinism first, speed second
# --------------------------------------------------------------------------
def provider_options(provider: str) -> dict:
    """Options pinned so a rerun on the same machine reproduces the same numbers."""
    if provider == "CUDAExecutionProvider":
        return {
            # The default EXHAUSTIVE search benchmarks convolution algorithms at
            # runtime and can pick a different one per process, which makes the
            # output vary run to run. HEURISTIC is stable.
            "cudnn_conv_algo_search": "HEURISTIC",
            # TF32 silently truncates fp32 mantissas on Ampere+. Off keeps the
            # arithmetic reproducible; on GPUs without TF32 the flag is a no-op.
            "use_tf32": "0",
            "do_copy_in_default_stream": "1",
        }
    if provider == "CoreMLExecutionProvider":
        # ALL lets CoreML use the Neural Engine, which is where its throughput is.
        # MLProgram is the modern format and the one with fp16 support.
        return {"MLComputeUnits": os.environ.get("COREML_UNITS", "ALL"), "ModelFormat": "MLProgram"}
    return {}


def _session_options(provider: str, threads: int, precision: str = "int8"):
    ort = _import_ort()
    options = ort.SessionOptions()
    options.intra_op_num_threads = max(1, int(threads))
    options.inter_op_num_threads = 1
    if precision == "fp16":
        # MEASURED (onnxruntime 1.27.0, Xenova/bge-m3 fp16): the DEFAULT
        # optimisation level (ORT_ENABLE_ALL) crashes at session creation --
        # SimplifiedLayerNormFusion trips over onnxruntime's own inserted
        # precision-free cast ("Attempting to get index by a name which does not
        # exist: InsertedPrecisionFreeCast_<node>"). EXTENDED loads in 0.2 s. This is
        # not a tuning choice: without it the fp16 asset cannot be loaded at all,
        # which would take the CPU FALLBACK down with it on a box whose GPU
        # session failed.
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_EXTENDED
    if provider == "DmlExecutionProvider":
        # Both are documented DirectML requirements, not tuning: the EP does not
        # support the memory pattern optimiser, and it needs sequential execution.
        options.enable_mem_pattern = False
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    return options


def make_session(onnx_path, profile: DeviceProfile, threads: int = 1):
    """Build an InferenceSession on `profile`'s EP, degrading instead of failing.

    Three fallbacks, in order: full provider options -> no provider options ->
    CPU. An option key that a slightly older onnxruntime build rejects raises at
    session construction, and that must not be the end of the build.
    """
    ort = _import_ort()
    path = str(onnx_path)
    provider = profile.provider
    if provider != CPU_EP:
        # Two DISTINCT attempts: with the pinned options, then bare. When there are
        # no options to drop the second attempt is the same as the first, so it is
        # skipped rather than repeated (and rather than printing a retry message
        # for a retry that cannot differ).
        attempts = [profile.provider_options] if not profile.provider_options else [profile.provider_options, {}]
        for options in attempts:
            try:
                return ort.InferenceSession(
                    path,
                    sess_options=_session_options(provider, threads, profile.precision),
                    providers=[(provider, options)] if options else [provider],
                )
            except Exception as error:  # noqa: BLE001 - any EP failure means "use the CPU"
                last = options is attempts[-1]
                print(
                    f"[runtime] {provider} session failed ({type(error).__name__}: {error}); "
                    f"{'falling back to CPU' if last else 'retrying without provider options'}",
                    file=sys.stderr,
                )
    return ort.InferenceSession(
        path,
        sess_options=_session_options(CPU_EP, threads, profile.precision),
        providers=[CPU_EP],
    )


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------
def _policy() -> str:
    value = os.environ.get("DEVICE", "auto").strip().lower()
    if value not in {"auto", "cpu", "gpu"}:
        raise ValueError(f"DEVICE must be auto, cpu or gpu; got {value!r}")
    return value


def _cpu_profile(reason: str, precision: str = "int8") -> DeviceProfile:
    """A CPU profile for the asset that was ASKED FOR, not the one the CPU prefers.

    The distinction is load-bearing: the caller has already opened the fp16 file
    when it lands here, and the session options differ by asset (fp16 needs the
    optimisation level capped or onnxruntime cannot even build the session). A
    profile that reported int8 while the fp16 graph was being loaded produced
    exactly that crash.
    """
    return DeviceProfile(
        provider=CPU_EP,
        provider_options={},
        precision=precision if precision in {"int8", "fp16"} else "int8",
        batch=_batch_for(CPU_EP),
        reason=reason,
        wheel=installed_wheel(),
        ort_version=_ort_version(),
        available=available_providers(),
    )


def _ort_version() -> str:
    try:
        return _import_ort().__version__
    except RuntimeError:
        return ""


@cache
def detect(precision_request: str = "auto") -> DeviceProfile:
    """Choose the EP and the asset for this machine. Cached for the process.

    `precision_request` is the fleet-wide setting (int8 | fp16 | auto). It is a
    FLEET decision, not a per-node one: the vectors in a shipped index and the
    query encoder on every spoke have to come from the same asset, which is why
    `index_signature` carries the precision and this function never overrides an
    explicit request.
    """
    policy = _policy()
    forced = os.environ.get("ORT_PROVIDER", "").strip()

    if forced:
        if forced not in available_providers():
            raise RuntimeError(
                f"ORT_PROVIDER={forced!r} is not registered by this onnxruntime "
                f"({installed_wheel()}). Registered: {', '.join(available_providers()) or 'none'}. "
                f"See `make gpu-probe`."
            )
        precision = precision_request if precision_request in {"int8", "fp16"} else precision_for(forced)
        return DeviceProfile(
            provider=forced,
            provider_options=provider_options(forced),
            precision=precision,
            batch=_batch_for(forced),
            reason="forced by ORT_PROVIDER",
            wheel=installed_wheel(),
            ort_version=_ort_version(),
            available=available_providers(),
        )

    if policy == "cpu":
        return _cpu_profile("DEVICE=cpu", precision_request)

    candidates = [ep for ep in candidate_providers() if ep not in _OPT_IN_ONLY]
    if not candidates:
        demoted = [ep for ep in candidate_providers() if ep in _OPT_IN_ONLY]
        reason = (
            f"the only GPU provider(s) here are opt-in ({', '.join(demoted)}); set "
            f"ORT_PROVIDER to force one"
            if demoted
            else f"no GPU execution provider registered ({installed_wheel()})"
        )
        return _cpu_profile(reason, precision_request)

    provider = candidates[0]
    wanted = precision_request if precision_request in {"int8", "fp16"} else precision_for(provider)
    if wanted != "fp16":
        # Rule 1. Measured, not assumed: int8 on a GPU EP was 1.7x SLOWER than one
        # CPU thread here. DEVICE=gpu can override, and then owns the result.
        if policy != "gpu":
            return _cpu_profile(
                f"{provider} is available but the requested asset is int8, which is a CPU "
                f"format (set EMBEDDING_PRECISION=fp16 to use the GPU)",
                wanted,
            )
        print(
            f"[runtime] DEVICE=gpu with {wanted} on {provider}: int8 on a GPU EP is "
            f"usually slower than the CPU. Prefer EMBEDDING_PRECISION=fp16.",
            file=sys.stderr,
        )

    return DeviceProfile(
        provider=provider,
        provider_options=provider_options(provider),
        precision=wanted,
        batch=_batch_for(provider),
        reason=f"first registered GPU provider for {platform.system()}",
        wheel=installed_wheel(),
        ort_version=_ort_version(),
        available=available_providers(),
    )


ENCODE_BATCH_DEFAULT = 16


def _batch_for(provider: str) -> int:
    """Passages per forward pass. THE SAME ON EVERY DEVICE, on purpose.

    This looks like a throughput knob and is not: the tokenizer pads to the
    longest sequence in the batch, so the batch's COMPOSITION changes the numbers
    the graph produces. Measured on this corpus: one passage encoded alone versus
    the same passage batched beside a long one differs by cosine 0.9918 -- three
    orders of magnitude larger than any execution-provider difference. Handing the
    GPU a bigger batch would therefore mean a GPU-built index and a CPU-built
    index disagree about roughly 1% of every vector, silently.

    So batch size is part of the vector definition, not of the hardware profile.
    ENCODE_BATCH still overrides it -- and `index_signature` records the override,
    so a fleet cannot half-adopt one.
    """
    override = os.environ.get("ENCODE_BATCH")
    if override:
        return max(1, int(override))
    return ENCODE_BATCH_DEFAULT


# --------------------------------------------------------------------------
# Profile cache (machine-fingerprinted, so a synced copy is never trusted)
# --------------------------------------------------------------------------
def machine_fingerprint() -> str:
    """Identity of the hardware+wheel combination a measurement is valid for.

    The data plane is synced between hub and spokes, so a cached profile can
    physically arrive on a machine it was not measured on. Comparing this before
    reading it is what makes that harmless.
    """
    return "|".join(
        (
            platform.system(),
            platform.machine(),
            platform.python_version(),
            installed_wheel(),
            ",".join(available_providers()),
        )
    )


def load_profile(path) -> dict | None:
    path = Path(path)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("fingerprint") != machine_fingerprint():
        return None
    return payload


def save_profile(path, profile: DeviceProfile, measurements: dict | None = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fingerprint": machine_fingerprint(),
        "profile": asdict(profile),
        "measurements": measurements or {},
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# Preflight / probe
# --------------------------------------------------------------------------
def preflight() -> list:
    """Problems worth telling the operator about. Empty means nothing to say."""
    problems: list = []
    wheel = installed_wheel()
    if wheel.count("onnxruntime") > 1 and "," in wheel:
        problems.append(
            f"MORE THAN ONE onnxruntime wheel is installed ({wheel}). They all provide the "
            f"same module and must never be mixed: uninstall all but one, then re-sync."
        )
    registered = available_providers()
    if not registered:
        problems.append("onnxruntime registered no execution providers at all (broken install).")
        return problems
    if "onnxruntime-gpu" in wheel and "CUDAExecutionProvider" not in registered:
        problems.append(
            "onnxruntime-gpu is installed but CUDAExecutionProvider did not register -- the "
            "CUDA/cuDNN runtime is missing or mismatched. The CPU path still works; install "
            "the matching CUDA runtime or go back to the plain `onnxruntime` wheel."
        )
    if "onnxruntime-directml" in wheel and "DmlExecutionProvider" not in registered:
        problems.append(
            "onnxruntime-directml is installed but DmlExecutionProvider did not register."
        )
    return problems


@contextmanager
def _capture_native_stderr():
    """Capture C-level stderr (fd 2), where onnxruntime writes EP diagnostics.

    The CoreML partition count -- the single number that says whether a GPU will
    help on a Mac -- is only ever printed by the native layer, so Python-level
    redirection cannot see it.
    """
    import tempfile

    holder: dict = {"text": ""}
    saved = os.dup(2)
    with tempfile.TemporaryFile(mode="w+b") as sink:
        os.dup2(sink.fileno(), 2)
        try:
            yield holder
        finally:
            sys.stderr.flush()
            os.dup2(saved, 2)
            os.close(saved)
            sink.seek(0)
            holder["text"] = sink.read().decode("utf-8", "replace")


def probe(onnx_path=None, save_to=None) -> dict:
    """Report what this machine can do. Operator tool; `make gpu-probe`."""
    report = {
        "platform": f"{platform.system()} {platform.machine()}",
        "wheel": installed_wheel(),
        "ort_version": _ort_version(),
        "available_providers": list(available_providers()),
        "candidates": list(candidate_providers()),
        "problems": preflight(),
    }
    # Default int8, matching Settings.embedding_precision: the shipped default has
    # to keep producing today's bytes, so a GPU appearing on the box changes
    # nothing until the fleet opts into the fp16 asset.
    profile = detect(os.environ.get("EMBEDDING_PRECISION", "int8").strip().lower())
    report["selected"] = asdict(profile)
    report["selected_summary"] = profile.describe()
    # What the fleet would get by opting in -- the whole point of the probe is to
    # answer "is fp16 worth it on THIS machine" without editing .env first.
    report["if_fp16"] = detect("fp16").describe()

    report["opt_in_only"] = [ep for ep in report["candidates"] if ep in _OPT_IN_ONLY]
    if onnx_path and Path(onnx_path).exists():
        # Partition counts: how much of the graph the EP will actually run. A
        # heavily partitioned graph copies tensors back and forth at every
        # boundary and is why an "accelerated" run can be slower than the CPU.
        # Only the providers this machine would actually CHOOSE are built here:
        # an opt-in-only provider is on that list because it was measured to
        # crash, and a probe must not take itself down proving it again.
        for provider in [ep for ep in report["candidates"] if ep not in _OPT_IN_ONLY]:
            with _capture_native_stderr() as holder:
                try:
                    make_session(onnx_path, DeviceProfile(provider=provider,
                                                          provider_options=provider_options(provider)))
                    failed = None
                except Exception as error:  # noqa: BLE001
                    failed = f"{type(error).__name__}: {error}"
            captured = holder["text"]
            note = {"error": failed} if failed else {}
            for line in captured.splitlines():
                if "number of partitions supported by" in line:
                    note["partitions"] = line.split("GetCapability,")[-1].strip()
            report.setdefault("graph_support", {})[provider] = note or {"partitions": "not reported"}

    if save_to:
        save_profile(save_to, profile)
        report["saved_to"] = str(save_to)
    return report


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Report the ONNX execution provider this machine will use.")
    parser.add_argument("--model", help="Optional .onnx path to report per-EP graph support for.")
    parser.add_argument("--save", action="store_true", help="Write the chosen profile to the data plane.")
    args = parser.parse_args(argv)

    from pipeline import get_paths

    paths = get_paths()
    model = args.model
    if model is None:
        # Default to the embedder asset if it is already in the local cache; a
        # probe must never download.
        try:
            from pipeline.build_rag import cached_asset_path

            model = cached_asset_path()
        except Exception:  # noqa: BLE001 - probing must work before any model exists
            model = None

    report = probe(model, save_to=paths.processed / "device_profile.json" if args.save else None)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    for problem in report["problems"]:
        print(f"[runtime] WARNING: {problem}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
