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
from dataclasses import asdict, dataclass, field, replace
from functools import cache
from pathlib import Path

CPU_EP = "CPUExecutionProvider"

# --------------------------------------------------------------------------
# Device classes -- the axis is the HARDWARE CLASS, not the operating system
# --------------------------------------------------------------------------
# An earlier version keyed the candidate order on `platform.system()` and a list
# of EP names. That is the wrong axis twice over: it has to be edited for every
# new OS/vendor pairing, and it cannot express "this machine has an NPU" at all.
# onnxruntime itself now classifies hardware as CPU / GPU / NPU
# (OrtHardwareDeviceType), and its own recommended order is NPU -> GPU -> CPU, so
# that is the axis used here. The OS still decides WHICH providers exist; it just
# no longer decides which are preferred.
CPU, GPU, NPU = "cpu", "gpu", "npu"

# Fallback classification, needed because ORT's device enumeration is incomplete:
# MEASURED here (onnxruntime 1.27.0), `get_ep_devices()` returned only the CPU on
# a machine where the CoreML EP was registered, and the same blind spot is
# reported upstream for the Intel NPU behind the OpenVINO EP. So the EP list is
# always merged in, and each device records where it was learned from.
_EP_CLASS = {
    CPU_EP: CPU,
    "CUDAExecutionProvider": GPU,
    "TensorrtExecutionProvider": GPU,
    "DmlExecutionProvider": GPU,
    "ROCMExecutionProvider": GPU,
    "MIGraphXExecutionProvider": GPU,
    "CoreMLExecutionProvider": GPU,
    "WebGpuExecutionProvider": GPU,
    "QNNExecutionProvider": NPU,
    "VitisAIExecutionProvider": NPU,
    # OpenVINO is whatever `device_type` says; without that wired, treat it as a
    # GPU-class provider and keep it opt-in.
    "OpenVINOExecutionProvider": GPU,
}

# THE ASSET FOLLOWS THE CLASS. This is the three-way split the two-way one could
# not express: an NPU is an integer engine, so "accelerator therefore fp16" --
# true for a GPU -- is wrong for it. A fourth class, GENERATIVE (autoregressive
# decoding under ONNX Runtime GenAI, asset rule reaching into KV-cache dtype), is
# a deliberately reserved slot: nothing in this repo decodes autoregressively,
# and each onnxruntime-genai variant pins a DIFFERENT onnxruntime distribution,
# which would reopen the wheel conflict `[tool.uv] conflicts` exists to close.
# See docs/plans/heterogeneous-device-roles.md for the trigger to fill it in.
_CLASS_ASSET = {CPU: "int8", GPU: "fp16", NPU: "int8"}

# Preference order between classes, per onnxruntime's own recommendation.
_CLASS_ORDER = (NPU, GPU, CPU)

# Providers that exist but are never chosen AUTOMATICALLY, each with the reason,
# because the reasons are not the same and the difference matters to an operator:
# some were measured to be worse, others have simply never run on real hardware
# here. Both are reachable with ORT_PROVIDER=<name>.
_DEMOTED = {
    # MEASURED, three independent negatives on this repo's models (2026-07-27,
    # macOS 15 / M4 Pro, onnxruntime 1.27.0):
    #   * bge-m3 int8  -- 148 partitions, 869 ms/chunk vs 517 on one CPU thread,
    #                     13.5 GB peak, SIGKILL at 32 chunks
    #   * bge-m3 fp16  -- 149 partitions, SIGSEGV on real chunk lengths
    #   * PP-OCRv5     -- cannot bound the dynamic input shape; one exception per
    #                     op and 18x slower (2.1 s -> 38.2 s a page)
    # A native crash cannot be caught in-process, so "always fall back to the
    # CPU" cannot protect a build here. Not choosing it is the only protection.
    "CoreMLExecutionProvider": "measured slower and crash-prone on these models",
    "TensorrtExecutionProvider": "needs a per-model engine build this pipeline does not do",
    "OpenVINOExecutionProvider": "needs an explicit device_type; NPU routing not wired",
    "WebGpuExecutionProvider": "upstream marks it experimental (wrong results / crashes)",
    # UNVERIFIED, not measured-bad. The blocker is specific: NPU runtimes
    # generally compile for STATIC shapes, and this embedder pads to the batch's
    # longest sequence, so the graph it would have to accept is dynamic. Forcing
    # a fixed 512 would both slow it down and change every vector. Try the short,
    # fixed-shape stages (reranker, OCR) first, and report `make bench-ep`.
    "QNNExecutionProvider": "unverified on hardware; dynamic sequence length may not compile",
    "VitisAIExecutionProvider": "unverified on hardware",
}
_OPT_IN_ONLY = frozenset(_DEMOTED)

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


@dataclass(frozen=True)
class Device:
    """One (execution provider, hardware) pair, classified vendor-neutrally."""

    klass: str            # cpu | gpu | npu
    ep: str               # execution provider name
    vendor: str = ""
    device_id: int = 0
    source: str = "eps"   # "ort" (device API) | "eps" (provider list) | "both"

    @property
    def demoted(self) -> str:
        """Why this device is not chosen automatically, or "" if it is eligible."""
        return _DEMOTED.get(self.ep, "")


def _devices_from_ort() -> list:
    """Devices as onnxruntime itself classifies them. Empty when unsupported."""
    try:
        ort = _import_ort()
        entries = ort.get_ep_devices()
    except (RuntimeError, AttributeError):
        return []
    devices = []
    for entry in entries:
        hardware = getattr(entry, "device", None)
        kind = getattr(getattr(hardware, "type", None), "name", "").lower()
        if kind not in {CPU, GPU, NPU}:
            continue
        devices.append(
            Device(
                klass=kind,
                ep=entry.ep_name,
                vendor=str(getattr(hardware, "vendor", "") or entry.ep_vendor or ""),
                device_id=int(getattr(hardware, "device_id", 0) or 0),
                source="ort",
            )
        )
    return devices


@cache
def enumerate_devices() -> tuple:
    """Every usable device on this machine, classified as cpu / gpu / npu.

    Two sources, merged, because neither is sufficient on its own: onnxruntime's
    device API carries the real hardware class (and the vendor, and NPUs, which
    the provider list cannot express), but it is incomplete -- measured here, it
    reported only the CPU on a machine with the CoreML EP registered. The
    provider list is complete but says nothing about hardware, so its entries are
    classified from a static table. Each device records which source knew it, and
    `make gpu-probe` prints that, because a disagreement between the two is
    usually the diagnosis.
    """
    by_ep: dict = {}
    for device in _devices_from_ort():
        by_ep[device.ep] = device
    for ep in available_providers():
        klass = _EP_CLASS.get(ep)
        if klass is None:
            continue  # an EP this build knows nothing about: do not guess a class
        existing = by_ep.get(ep)
        if existing is None:
            by_ep[ep] = Device(klass=klass, ep=ep, source="eps")
        elif existing.klass == klass:
            by_ep[ep] = replace(existing, source="both")
    order = {klass: index for index, klass in enumerate(_CLASS_ORDER)}
    return tuple(sorted(by_ep.values(), key=lambda d: (order.get(d.klass, 99), d.ep)))


def devices_of_class(klass: str, eligible_only: bool = True) -> tuple:
    """Devices of one class, best first. `eligible_only` drops the demoted ones."""
    return tuple(
        device
        for device in enumerate_devices()
        if device.klass == klass and not (eligible_only and device.demoted)
    )


def candidate_providers(system: str | None = None) -> tuple:
    """Accelerator EPs on this machine, best class first. CPU is never included.

    `system` is accepted and ignored: the axis is the device class, not the OS.
    It survives only so an older caller does not break.
    """
    return tuple(
        device.ep
        for device in enumerate_devices()
        if device.klass != CPU
    )


def class_of(provider: str) -> str:
    """Device class of an EP name, defaulting to CPU for anything unknown."""
    for device in enumerate_devices():
        if device.ep == provider:
            return device.klass
    return _EP_CLASS.get(provider, CPU)


def precision_for(provider: str) -> str:
    """The asset a provider's device class wants: GPU fp16, CPU and NPU int8."""
    return _CLASS_ASSET.get(class_of(provider), "int8")


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
def detect(precision_request: str = "auto", stage: str = "") -> DeviceProfile:
    """Choose the device and the asset for this machine. Cached for the process.

    THE RULE IS ONE LINE: pick the best-ranked device class whose asset matches
    the requested one. That replaces an earlier special case ("a GPU is available
    but the asset is int8, so use the CPU instead") which encoded the same
    conclusion for GPUs and got it exactly backwards for NPUs -- an NPU is an
    integer engine and int8 is precisely what it wants. Written as a class/asset
    match, both fall out of the same statement and a fourth class can be added
    without another special case.

    `precision_request` is the FLEET-wide setting (int8 | fp16 | auto), never a
    per-node one: the vectors in a shipped index and the query encoder on every
    spoke have to come from the same asset, which is why index_signature carries
    the precision. `auto` resolves to whatever the best eligible class wants.

    `stage` names the caller (embedder | reranker | ocr) so DEVICE_PLACEMENT can
    pin one stage to a class without moving the others.
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
        return _profile_for(forced, precision, "forced by ORT_PROVIDER")

    if policy == "cpu":
        return _cpu_profile("DEVICE=cpu", precision_request)

    pinned = placement_for(stage)
    if pinned == CPU:
        return _cpu_profile(f"DEVICE_PLACEMENT pins {stage or 'this stage'} to the cpu", precision_request)

    for klass in _CLASS_ORDER:
        if klass == CPU:
            break
        if pinned and klass != pinned:
            continue
        eligible = devices_of_class(klass)
        if not eligible:
            continue
        wanted = precision_request if precision_request in {"int8", "fp16"} else _CLASS_ASSET[klass]
        if wanted != _CLASS_ASSET[klass]:
            # The device exists but wants a different asset than the fleet uses.
            # Measured for the GPU/int8 pairing: 1.7x SLOWER than a single CPU
            # thread. DEVICE=gpu overrides and then owns the result.
            if policy != "gpu":
                continue
            print(
                f"[runtime] DEVICE=gpu forces {eligible[0].ep} with the {wanted} asset, which "
                f"a {klass} does not want ({_CLASS_ASSET[klass]}). This is usually slower than "
                f"the CPU.",
                file=sys.stderr,
            )
        return _profile_for(
            eligible[0].ep, wanted, f"best eligible {klass} device ({eligible[0].source})"
        )

    return _cpu_profile(_no_accelerator_reason(precision_request, pinned), precision_request)


def _no_accelerator_reason(precision_request: str, pinned: str) -> str:
    """Say WHICH of the several ways there was to have no accelerator applied."""
    if pinned:
        return f"DEVICE_PLACEMENT asked for a {pinned} device and none is eligible here"
    accelerators = [device for device in enumerate_devices() if device.klass != CPU]
    if not accelerators:
        return f"no accelerator registered ({installed_wheel()})"
    demoted = [f"{device.ep} ({device.demoted})" for device in accelerators if device.demoted]
    if len(demoted) == len(accelerators):
        return f"every accelerator here is opt-in: {'; '.join(demoted)}. Force one with ORT_PROVIDER"
    wants = {_CLASS_ASSET[device.klass] for device in accelerators if not device.demoted}
    return (
        f"the accelerator(s) here want the {'/'.join(sorted(wants))} asset and this fleet uses "
        f"{precision_request}; set EMBEDDING_PRECISION to match to use them"
    )


def _profile_for(provider: str, precision: str, reason: str) -> DeviceProfile:
    return DeviceProfile(
        provider=provider,
        provider_options=provider_options(provider),
        precision=precision if precision in {"int8", "fp16"} else _CLASS_ASSET[class_of(provider)],
        batch=_batch_for(provider),
        reason=reason,
        wheel=installed_wheel(),
        ort_version=_ort_version(),
        available=available_providers(),
    )


def placement_for(stage: str) -> str:
    """Device CLASS this stage is pinned to by DEVICE_PLACEMENT, or "".

    `DEVICE_PLACEMENT="embedder=gpu,reranker=npu,ocr=cpu"` -- a class, never an EP
    name, so one setting keeps working across machines with different vendors.
    Pinning is a preference, not an assertion: a stage pinned to a class with no
    eligible device falls back to the CPU with a stated reason, because a
    placement typo must not stop a build (rule 2).
    """
    if not stage:
        return ""
    raw = os.environ.get("DEVICE_PLACEMENT", "").strip()
    if not raw:
        return ""
    for item in raw.split(","):
        name, _, klass = item.partition("=")
        if name.strip().lower() != stage.strip().lower():
            continue
        klass = klass.strip().lower()
        if klass not in {CPU, GPU, NPU}:
            raise ValueError(
                f"DEVICE_PLACEMENT: {stage} must be one of cpu/gpu/npu, got {klass!r}"
            )
        return klass
    return ""


# --------------------------------------------------------------------------
# Residency -- what is loaded at the same time, and what that costs
# --------------------------------------------------------------------------
# "Load the model on every device at once" is the shape of heterogeneous
# execution that actually pays (OpenVINO calls it MULTI / CUMULATIVE_THROUGHPUT),
# and its cost is memory, not latency. Sessions here are cached for the life of
# the process, so a run can end up holding the embedder AND the reranker AND an
# OCR model, across more than one device -- measured, embedder + reranker alone
# peaked at 4.47 GB.
#
# This TRACKS and WARNS; it deliberately does not evict. Eviction is only worth
# its risk when there is real pressure to relieve, and at most two models are
# resident today; a cache that silently drops a session mid-build would trade a
# memory warning for a latency mystery. Revisit if a server mode makes three or
# more concurrent.
_RESIDENT: dict = {}


def _physical_memory_mb() -> int:
    """Total RAM, or 0 when it cannot be determined. Cross-platform, no deps."""
    try:  # Linux, macOS
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 * 1024))
    except (ValueError, OSError, AttributeError):
        pass
    try:  # Windows
        import ctypes

        class _Status(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _Status()
        status.dwLength = ctypes.sizeof(_Status)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))  # type: ignore[attr-defined]
        return int(status.ullTotalPhys / (1024 * 1024))
    except Exception:  # noqa: BLE001 - a missing figure must not break a build
        return 0


def resident_budget_mb() -> int:
    """How much model weight may sit resident before this warns.

    Default: 40% of physical RAM. Weights are only part of a session's footprint
    (arenas, the tokenizer, the graph itself), so the headroom is deliberate --
    measured, a 568 MB int8 model in a single query peaked at 2.50 GB RSS.
    """
    override = os.environ.get("RESIDENT_BUDGET_MB", "").strip()
    if override:
        return max(1, int(override))
    total = _physical_memory_mb()
    return int(total * 0.4) if total else 2048


def note_resident(stage: str, asset_path, provider: str = "") -> None:
    """Record that `stage` now holds a model, and warn if the total looks unsafe.

    The asset file size is the proxy for the footprint: it is the one number
    available before any inference runs, it is exact for the weights, and it is
    the figure an operator can act on (choose a smaller asset, or stop loading
    two models at once).
    """
    try:
        size_mb = int(Path(asset_path).stat().st_size / (1024 * 1024))
    except OSError:
        return
    _RESIDENT[stage] = size_mb
    total = sum(_RESIDENT.values())
    budget = resident_budget_mb()
    if total > budget:
        held = ", ".join(f"{name} {mb} MB" for name, mb in sorted(_RESIDENT.items()))
        print(
            f"[runtime] {total} MB of model weights resident ({held}) against a "
            f"{budget} MB budget on {_physical_memory_mb() or '?'} MB of RAM. Actual RSS runs "
            f"several times the weights. Load fewer stages at once, choose the int8 asset, or "
            f"raise RESIDENT_BUDGET_MB if this machine can take it."
            + (f" [{provider}]" if provider else ""),
            file=sys.stderr,
        )


def resident_report() -> dict:
    """What is loaded right now, for `make gpu-probe` and the smoke checks."""
    return {
        "stages": dict(sorted(_RESIDENT.items())),
        "total_mb": sum(_RESIDENT.values()),
        "budget_mb": resident_budget_mb(),
        "physical_mb": _physical_memory_mb(),
    }


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
        # The device view is the one that matters: class, who reported it, and
        # why it is or is not eligible. `source` disagreeing between the two
        # enumerations is usually the diagnosis when an accelerator "vanishes".
        "devices": [
            {
                "class": device.klass,
                "ep": device.ep,
                "vendor": device.vendor,
                "source": device.source,
                "eligible": not device.demoted,
                "why_not": device.demoted,
            }
            for device in enumerate_devices()
        ],
        "asset_per_class": dict(_CLASS_ASSET),
        "placement": os.environ.get("DEVICE_PLACEMENT", "") or "(none)",
        "residency": resident_report(),
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
