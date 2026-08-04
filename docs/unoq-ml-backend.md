# Running Deep-Learning Models on the Arduino Uno Q — Backend Selection Guide

> Status: complete as of 2026-06. Every "no" below is backed by a runtime error code,
> a measurement, or a firmware listing — not inference. See **§7 Evidence** for receipts.

---

## 0. TL;DR

**The CPU is the only viable inference engine on this board. Target it.**

- ✅ **CPU (4×A53 + NEON)** — fast, proven, any precision. Use ONNX Runtime + XNNPACK,
  TFLite/LiteRT, ncnn (CPU), or llama.cpp (CPU). Piper TTS runs at **RTF 0.365**.
- ⚠️ **GPU (Adreno 702) via ncnn-Vulkan** — the *only* functional accelerator path, but
  **~6.5× slower** than CPU. Use **only** to offload the CPU for concurrency, never for speed.
- ❌ **QNN (HTP / GPU / DSP), TFLite-Hexagon, llama.cpp-HTP** — all closed. The chip boots
  none of the compute engines Qualcomm's accelerated runtimes require.

**There is no int8-NPU to chase.** Quantize to int8 only for CPU memory/throughput; otherwise
fp16/fp32 on CPU is fine. (int8 would only matter for an HVX/HTP path, which is unreachable.)

---

## 1. The hardware reality

| Block | Spec | NN status on this board |
|---|---|---|
| SoC | **QRB2210** (QCM2290 family), `soc_id 524`, Debian Trixie aarch64 | — |
| CPU | Quad **Cortex-A53 @ 2 GHz** + NEON (fp16) | ✅ **the engine** (~21 GFLOPS real sgemm) |
| GPU | **Adreno 702**, 1 compute unit, 844 MHz, Mesa drivers | ⚠️ ~25 GFLOPS peak — too weak |
| Hexagon | **QDSP6 V66 + HVX** (vector) | ❌ booted as **audio aDSP only** — no compute path |
| HTP/NPU | — | ❌ **absent** (HTP starts at V68; this is V66) |

GPU driver stack (all open-source Mesa, **not** Qualcomm proprietary):
- OpenCL: **Rusticl**, device `FD702`, OpenCL 3.0, `cl_khr_fp16` ✓
- Vulkan: **Turnip**, `Turnip Adreno 702`, compute queue ✓, `shaderFloat16` ✓

**Why the accelerators are closed (one sentence each):**
- **GPU is too weak** — peak compute barely exceeds the CPU and collapses on real (memory-bound) workloads.
- **QNN-GPU rejects the board** — it requires Qualcomm's *proprietary* OpenCL + a supported-SoC allowlist; the board has only Mesa.
- **No HTP** — the V66 Hexagon predates the tensor NPU; QNN's HTP runtime doesn't recognize `soc 524`.
- **No compute-DSP** — the firmware boots the Hexagon only as the audio aDSP (no `cdsp.mbn`), so HVX isn't reachable for general compute.

---

## 2. Backend decision table

| Path | Engine / API | Precision | Status | When to use |
|---|---|---|---|---|
| **CPU — ONNX Runtime + XNNPACK** | A53 NEON | int8/fp16/fp32 | ✅ proven | **Default for everything** |
| **CPU — TFLite / LiteRT + XNNPACK** | A53 NEON | int8/fp | ✅ proven | TFLite/`.tflite` & Edge Impulse models |
| **CPU — ncnn** | A53 NEON | fp16/int8 | ✅ proven | ncnn models |
| **CPU — llama.cpp** | A53 NEON | GGUF q4/q8 | ✅ ships | Small local LLMs |
| **GPU — ncnn + Vulkan (Turnip)** | Adreno 702 | fp16 | ⚠️ ~6.5× slower | **Only** to free the CPU (concurrency) |
| GPU — TFLite GPU delegate (Rusticl OpenCL) | Adreno 702 | fp16 | ⚠️ untested; same weak-GPU ceiling | Probably not worth it |
| GPU — QNN GPU backend (`libQnnGpu`) | Adreno 702 | fp16 | ❌ `Unsupported SOC` | — |
| NPU — QNN HTP (`libQnnHtp`) | — | int8 | ❌ no HTP hardware | — |
| DSP — QNN HVX V66 (`libQnnDsp`) | Hexagon V66 | int8 | ❌ no compute-DSP firmware | — |
| LLM — llama.cpp "hexagon" (HTP) | — | GGUF | ❌ needs HTP V68+ | — |

---

## 3. Decision flow for a new model / backend

1. **Default to CPU.** Pick the runtime that matches the model format
   (ONNX→ORT+XNNPACK, TFLite→LiteRT, ncnn→ncnn, GGUF→llama.cpp). Measure RTF/latency.
2. **Is CPU fast enough for the task?** If yes → done. (Most small/medium models are.)
3. **CPU too slow AND you have spare GPU?** The *only* working accelerator is **ncnn-Vulkan**.
   Convert the model to ncnn (`pnnx`), enable fp16, benchmark GPU vs CPU. Expect it to be
   *slower per-inference* — its value is letting the GPU run a model while the CPU does other work.
4. **Do NOT** reach for QNN / HTP / DSP / TFLite-Hexagon on this board — they're closed (§7).
5. **Precision:** fp16/fp32 for CPU and the Vulkan GPU; int8 only as a CPU memory/throughput tweak.

---

## 4. How to test whether a new backend works (reproducible methodology)

The cheapest gates first — this is how we avoided multi-week rabbit holes.

**GPU substrate (2 min):**
```bash
clinfo | grep -iE "Device Name|cl_khr_fp16"          # expect FD702 + fp16
vulkaninfo --summary | grep -iE "deviceName|driverName"   # expect Turnip Adreno 702
```

**Does an accelerator beat CPU? (ncnn, ~5 min):**
```bash
pip install ncnn                  # wheel ships Vulkan
python -c "import ncnn; print(ncnn.get_gpu_count())"   # >0 => GPU visible
# then run a real CNN on use_gpu=True vs False and compare latency + output parity
```

**Raw GPU compute ceiling (settles "could any framework win?"):**
```bash
pip install pyopencl              # runs on Rusticl, no root
# compute-bound FMA kernel => GPU peak GFLOPS; compare to numpy sgemm (CPU).
# If GPU_peak ≲ CPU_real, no software will make the GPU win. (Here: 25 vs 21.)
```

**Does QNN accept the SoC? (the make-or-break for any QNN path):**
```bash
pip install onnxruntime==1.24.4 onnxruntime-qnn      # plugin-EP model
# register EP, force backend_path=libQnnGpu.so (or HTP), disable CPU fallback,
# run a tiny fp16 model with logger severity 0 and READ THE QNN LOG.
# Look for: GPU_ERROR_UNSUPPORTED_PLATFORM / "No Snapdragon SOC detected" / 6999.
```

**Is a DSP/HTP even present?**
```bash
ls /sys/class/remoteproc/*/name | xargs cat          # modem, adsp ... any cdsp?
ls /lib/firmware/qcom/qcm2290/                        # cdsp.mbn present? (no => no compute DSP)
ls /dev/fastrpc-*                                     # fastrpc-cdsp? (no => no compute path)
```

---

## 5. What Arduino itself ships (catalog cross-check)

From `arduino/app-bricks-py` → `models/models-list.yaml`:

- **Every** model with `hw_acceleration_backend: qnn` is gated to `supported_boards: [ventunoq]`
  — a *separate, HTP-equipped* Arduino board.
- **The Uno Q (`unoq`) is assigned only `cpu`-backend models.** Zero accelerated models.
- Arduino's own **`piper-tts` / `melo-tts` / `whisper`** bricks run `backend: cpu` even on `ventunoq`.
  → TTS is a CPU workload, by Arduino's own design.

Runtime bricks and their engines:

| Brick | Engine | Format | unoq? |
|---|---|---|---|
| `ei-models-runner` | EI `runner.js` → TFLite/XNNPACK | `.eim` | ✅ CPU |
| `ei-qnn-models-runner` | EI + QNN TFLite delegate (HTP) | `.eim` | ❌ (ventunoq) |
| `aihub-models-runner` | **LiteRT** (`ai_edge_litert`) + QNN delegate, **CPU fallback** | `.tflite` w8a8/float | ⚠️ CPU |
| `gesture-recognition-runner` | child of aihub (MediaPipe) | `.tflite` | ⚠️ CPU |
| `llamacpp-runner` | llama.cpp CPU | GGUF | ✅ CPU |
| `llamacpp-npu-runner` | llama.cpp HTP (`libggml-htp-v68…v81`) | GGUF | ❌ (ventunoq) |

(`ai-hub` / `ei` / `hf` "handlers" are download-only, not runtimes.)

---

## 6. Environment quirks & gotchas (save yourself the debugging)

- **Audio:** PulseAudio `pacat` ignored the sample-rate flag → chipmunk playback. Use
  **`aplay -r <rate>`** (ALSA) for raw PCM streaming. (Piper amy-low = 16 kHz.)
- **Rusticl** exposes the GPU out of the box here, but in general may need `RUSTICL_ENABLE=freedreno`.
- **ncnn** pip wheel includes Vulkan (no build needed).
- **Edge Impulse `.eim`:** drive headless via `edge_impulse_linux.runner.ImpulseRunner`; the package
  pulls `pyaudio` (stub it) + needs `six`; pass an **absolute path** to the `.eim`; image input =
  **packed-RGB ints**, length `W*H` (not `W*H*3`).
- **ONNX Runtime 1.24** uses the **plugin-EP** model: `register_execution_provider_library(...)` then
  `SessionOptions.add_provider_for_devices(get_ep_devices()[...], {...})` — the legacy
  `providers=["QNNExecutionProvider"]` string is silently ignored.
- **Docker on the board is `masked`** (Arduino manages containers its own way); the `arduino` user is
  in the `docker` group, so once the daemon runs, `--privileged` device access needs no extra root.
- **`/dev/fastrpc-adsp` is root-only** (irrelevant — there's no compute PD behind it anyway).

---

## 7. Evidence (receipts)

**CPU baseline:** Piper `en_US-amy-low` int8, 4 threads pinned to A53 → **RTF 0.365**.
yolo-x-nano @416×416 (LiteRT/XNNPACK) → **373 ms/inference**.

**GPU is too weak (ncnn-Vulkan, SqueezeNet):** CPU 37 ms vs GPU 254 ms = **0.15×**; flat at
0.15–0.16× across input 227→1024 px → a *throughput deficit*, not fixed overhead (no crossover).

**GPU ceiling (pyopencl on Rusticl):** GPU peak **25.5 GFLOPS fp32** (register-FMA, best case) vs
CPU **21.2 GFLOPS** real sgemm = **1.2×** — no headroom; evaporates on memory-bound work.

**QNN-GPU rejects the board (onnxruntime-qnn 2.46, `libQnnGpu.so`):**
```
Found /usr/lib/aarch64-linux-gnu/libOpenCL.so.1
Extension function clSetPerfHintQCOM not available ...        ← Qualcomm OpenCL ext absent in Mesa
GPU ERROR: GPU_ERROR_UNSUPPORTED_PLATFORM(10029) - Unsupported SOC
GPU ERROR: GPU_ERROR_INVALID_ARG(10008) - Adreno  is not supported
QNN_BACKEND_ERROR_CANNOT_INITIALIZE: Backend failed to initialize
```

**QNN-HTP rejects the board (EI `yolo-x-nano-qnn.eim`):**
```
ERROR: [Qnn Delegate] Failed to identify soc 524
<E> No Snapdragon SOC detected
ERROR: ModifyGraphWithDelegate failed     # → EI error -3 (CPU twin ran fine)
```

**QNN-DSP (V66) — no compute target:** Arduino's QNN image has host stubs
(`libQnnDspV66Stub.so`) but no DSP-side `libQnnDspV66Skel.so` (that lives in the full QAIRT SDK).
Deeper blocker — firmware:
```
/lib/firmware/qcom/qcm2290/:  adsp.mbn  modem.mbn  a702_zap.mbn   (NO cdsp.mbn)
remoteproc: modem, adsp        (no cdsp)
/dev/fastrpc-adsp              (no /dev/fastrpc-cdsp)
```
Per Qualcomm doc 80-63442-10, the V66 DSP backend targets the **compute DSP** of flagship SoCs
(SM8150/SM8250). This board boots its Hexagon only as the audio aDSP → no place to run HVX compute.

**Versions:** Arduino QNN image QAIRT **2.40.0**; `onnxruntime-qnn` PyPI: 2.1.1=QAIRT 2.45.41
(first official linux-aarch64), 2.2.0=2.46; we tested with 2.46. (Versions don't matter — the
rejection is at the platform level.)

---

## 8. When to revisit

Only if the **hardware changes** — e.g., a board on the `ventunoq` (HTP-equipped) target, or a
firmware that brings up a `cdsp`. On *this* QRB2210/QCM2290, the accelerator question is settled:
**CPU is the engine; ncnn-Vulkan is the only (slow) offload; QNN is inert.**
