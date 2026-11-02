# Future Architecture Plan: NPU & Dedicated Edge AI Acceleration (GPU + NPU + Hailo-10H)

* **Target Version**: V.33+ (Next Hardware Expansion Milestone)
* **Document Status**: Phase 1 Completed in V.32 (v2.4.0) • Phases 2–4 Pending Physical Hailo-10H / Multi-NPU Hardware
* **Recent Completed Milestones**:
  * **V.30 (v2.3.1)**: Danbooru Boolean Logic Engine (`AND`, `OR`, `NOT`, `NOR`), Strict Safety Filtering, and Lightbox Sizing Modes.
  * **V.31 (v2.3.2–v2.3.4)**: Universal Minimizable Dialogs (`MinimizableDialog`), Two-Way Windows Taskbar Sync, Transparent PNG White-Canvas Engine, and DanbuDL Danbooru API Resolver.
  * **V.32 (v2.4.0)**:
    * Native `.mp4` Ugoira Video Conversion Engine (OpenCV `mp4v`/`avc1` with per-frame timing & Unicode path staging).
    * Windows DPAPI Credential Encryption (`CryptProtectData`), Thread-Safe Atomic Config (`update_app_config`), and Ugoira Zip Bomb Protection.
    * OpenCV Fast-Path RGBA White-Background Compositing (`decode_image_cv2_for_wd14`) & Dynamic Batch Size Selector (`Batch = 4 / 8 / 16 / 32`).
    * Built-in NPU Execution Provider Integration (`OpenVINOExecutionProvider`, `QNNExecutionProvider`) & Hardware Accelerator Discovery (`detect_ai_accelerators`).
    * ⭐ Favorites (`is:fav`), Custom Manual Tags, 🔍 64-bit Perceptual `dHash` Duplicate Finder, and 📦 One-Click LoRA / Kohya_ss Dataset Exporter.
* **Target Hardware**: 
  * **Built-in NPUs**: Intel Core Ultra (Meteor Lake / Arrow Lake / Lunar Lake), AMD Ryzen AI (Phoenix / Hawk Point / Strix Point), Qualcomm Snapdragon X Elite, Windows 11 Copilot+ PCs.
  * **Dedicated M.2 Edge AI Coprocessors**: **Hailo-10H** M.2 PCIe Module (40 TOPS INT4 / 20 TOPS INT8, 4GB–8GB on-module LPDDR4X).

---

## 1. Executive Summary & Vision

PixKura leverages DirectX 12 DirectML, CUDA, OpenVINO, and QNN to accelerate Vision AI tagging on modern GPUs and SoC NPUs, delivering high throughput (~200–290 FPS at Batch=16, FP16).

With the emergence of Neural Processing Units (NPUs) and dedicated M.2 Edge AI accelerators like the **Hailo-10H**, client devices now have access to high-efficiency specialized neural compute units. This document details the architectural roadmap to:
1. **Leverage Built-in & Dedicated NPUs** for ultra-low-power, silent, and battery-friendly background tagging *(✅ Foundation & ONNX NPU Providers completed in V.32)*.
2. **Implement Heterogeneous Hybrid Computing (GPU + NPU / Hailo-10H)** to run dual or tri-engine accelerators concurrently, maximizing throughput while eliminating GPU resource contention.
3. **Enable Universal Edge AI Acceleration** for older desktops, mini-PCs, and NAS servers via plug-and-play M.2 PCIe accelerators.

---

## 2. Key Motivations & Hardware Comparison

| Hardware Platform | Compute Power | Typical TDP | Memory Architecture | Best Use Case in PixKura |
| :--- | :--- | :--- | :--- | :--- |
| **Discrete / Integrated GPU** | Up to 100+ TFLOPS | 30W – 250W+ | Shared System RAM or Dedicated VRAM | High-throughput batch indexing (Batch=4..32, FP16) |
| **Built-in SoC NPU** (Core Ultra / Ryzen AI / Snapdragon X) | 16 – 50 TOPS | 2W – 8W | Unified System Memory | Battery-friendly laptop tagging, zero GPU contention |
| **Hailo-10H M.2 Coprocessor** | 20 TOPS (INT8) / 40 TOPS (INT4) | **~2.5W – 3.5W** | **Dedicated On-Module 4GB/8GB LPDDR4X** | 24/7 Home Server tagging, zero host RAM/VRAM footprint, upgrades any PC with an M.2 slot |

---

## 3. Key Benefits of Dedicated Edge AI (Hailo-10H)

1. **Zero Host RAM & VRAM Contention**:
   * The Hailo-10H contains **4GB or 8GB of dedicated LPDDR4X memory** soldered directly on the M.2 module.
   * Model weights, runtime intermediate activation buffers, and computation graphs reside entirely on the module, consuming **0 MB** of host system RAM and **0 MB** of GPU VRAM.
2. **Universal Compatibility for Legacy & Mini PCs**:
   * Users do not need to upgrade to expensive new CPU platforms (Intel Core Ultra or AMD Ryzen AI).
   * Any desktop, compact SFF PC, or home server with a spare **M.2 Key M (2242 / 2280) PCIe Gen 3.0 x4** slot can immediately gain 20–40 TOPS of dedicated AI compute.
3. **Extreme Energy Efficiency for 24/7 Operations**:
   * Consuming only **~2.5W**, a mini-PC or NAS running PixKura can continuously scan, index, and tag newly downloaded illustrations 24/7 silently with virtually zero impact on the electricity bill.

---

## 4. Core Architectural Strategies

### Strategy A: Dynamic Dual/Tri-Worker Pipeline (Split-Load Concurrent Inference)

A thread-safe in-memory queue feeds work packages dynamically across heterogeneous hardware backends:

```mermaid
graph TD
    A["Disk Scanner & OpenCV Prefetch Pool"] -->|Feed Image Tensors| Q["Thread-Safe In-Memory Queue"]
    
    subgraph Heterogeneous Co-Processing Engine
        Q -->|Pulls Batch=16 FP16| W1["Primary GPU Worker - DirectML / CUDA"]
        Q -->|Pulls Batch=4 INT8| W2["SoC NPU Worker - OpenVINO / QNN"]
        Q -->|Pulls Batch=8 INT8 via PCIe| W3["Hailo-10H M.2 Worker - HailoRT pyhailort"]
    end
    
    W1 -->|~220 FPS| DB[("SQLite Database / Thread-Safe Connection Pool")]
    W2 -->|~70 FPS| DB
    W3 -->|~120 FPS| DB
```

* **GPU Worker**: Focuses on maximum raw throughput (`batch_size=16`, FP16 half-precision) using `DmlExecutionProvider` or `CUDAExecutionProvider`.
* **SoC NPU Worker**: Handles continuous background batches (`batch_size=4`, INT8/FP16) via `OpenVINOExecutionProvider` or `QNNExecutionProvider`.
* **Hailo-10H Worker**: Operates via PCIe DMA using the native `pyhailort` pipeline, processing images directly in its on-module memory.
* **Auto-Load Balancing**: Each worker pulls from the shared queue only when ready, preventing bottlenecks regardless of hardware speed differences.

---

### Strategy B: Model-Level Parallelism for 3-in-1 Ensemble

In the **WD Tagger v3 Ensemble (3-in-1)** mode, all three vision models (`ConvNeXt`, `ViT`, `SwinV2`) can be distributed across physical hardware devices:

* **Primary GPU**: Executes `WD Tagger v3 SwinV2` (heavy transformer attention compute).
* **Hailo-10H M.2**: Executes `WD14 / WD v3 ConvNeXt` (convolution-heavy dataflow architecture achieves near 100% compute efficiency).
* **SoC NPU / CPU**: Executes `WD Tagger v3 ViT`.
* **Outcome**: True simultaneous hardware execution with 0% VRAM bottlenecks and aggregated ensemble soft-voting.

---

### Strategy C: Real-Time Ingestion Tagging (Download-to-Tag Pipeline)

When downloading artworks via the built-in Pixiv Downloader:
* Send newly downloaded images directly to the **Hailo-10H M.2 worker** for immediate tagging.
* The main GPU remains 100% idle and available for games, 3D rendering, or video playback.

---

## 5. Technical Stack & Execution Providers

### 5.1. Built-in SoC NPU Stack (Supported in `tagger.py`)

```
                       ┌──► Windows 11 Copilot+ (DirectML MCDM Driver) ──► DmlExecutionProvider
                       │
ONNX Runtime NPU ──────┼──► Intel Core Ultra (Meteor/Arrow/Lunar Lake) ──► OpenVINOExecutionProvider
                       │
                       ├──► AMD Ryzen AI (Phoenix/Hawk/Strix Point) ─────► VitisAIExecutionProvider / DirectML
                       │
                       └──► Qualcomm Snapdragon X Elite ────────────────► QNNExecutionProvider
```

### 5.2. Hailo-10H Dedicated M.2 Stack

Hailo devices utilize a **Dataflow Architecture**. Models are compiled once offline into **Hailo Executable Format (`.hef`)** and executed through **HailoRT**:

```
[WD14 ConvNeXt ONNX Model]
            │
            ▼ (Hailo Dataflow Compiler / DFC on WSL2 or Docker)
[wd14_convnextv2.hef (INT8 / Quantized Dataflow Graph)]
            │
            ▼ (Windows Host Runtime)
[Python tagger.py] ──► [HailoRT / pyhailort API] ──► [PCIe Gen3 x4] ──► [Hailo-10H M.2 Hardware]
```

#### Hailo Python Integration Snippet:
```python
from hailo_platform import VDevice, InferModel, FormatType

class HailoTaggerWorker:
    def __init__(self, hef_path: str):
        # Initialize virtual device targeting the Hailo-10H M.2 module
        self.vdevice = VDevice()
        self.infer_model = self.vdevice.create_infer_model(hef_path)
        self.infer_model.input().set_format_type(FormatType.UINT8)
        self.infer_model.output().set_format_type(FormatType.FLOAT32)
        self.configured_model = self.infer_model.configure()

    def predict_batch(self, image_batch):
        # Zero-copy async DMA transfer over PCIe directly to Hailo on-module RAM
        bindings = self.configured_model.create_bindings()
        bindings.input().set_buffer(image_batch)
        self.configured_model.run([bindings], timeout_ms=1000)
        return bindings.output().get_buffer()
```

---

## 6. Model Optimization & Architecture Compatibility

| Vision Model Architecture | DirectML GPU | Built-in SoC NPU | Hailo-10H M.2 Coprocessor |
| :--- | :---: | :---: | :---: |
| **WD14 ConvNeXt v2** (Pure CNN) | ⭐⭐⭐⭐⭐ (Fastest FP16) | ⭐⭐⭐⭐⭐ (Ideal for INT8) | 👑 **Optimal** (Dataflow CNN mapping, maximum FPS/Watt) |
| **WD Tagger v3 ViT** (Standard Transformer) | ⭐⭐⭐⭐ (High VRAM) | ⭐⭐⭐ (Requires Attention Ops) | ⭐⭐⭐⭐ (Supported with INT8 calibration) |
| **WD Tagger v3 SwinV2** (Shifted Windows) | ⭐⭐⭐⭐⭐ (DirectML) | ⭐⭐ (Complex dynamic ops) | ⭐⭐⭐ (Compiles via DFC with static shape) |

---

## 7. Implementation Roadmap & Current Progress

### Phase 1: Hardware Discovery, Provider Integration & Dynamic Batching — ✅ COMPLETED (V.32 / v2.4.0)
- [x] Implement NPU & AI accelerator detection in `utils.py` (`detect_ai_accelerators()` checking DirectML, CUDA, OpenVINO NPU, QNN NPU, and Hailo SDK availability).
- [x] Integrate `OpenVINOExecutionProvider` and `QNNExecutionProvider` into `WD14Tagger.get_available_providers()` and `WD14Tagger.load_model()` (`tagger.py`).
- [x] Display detected hardware accelerators (DirectML / CUDA / OpenVINO / QNN) and add configurable Batch Size selector (`Batch = 4 / 8 / 16 / 32`) in `AiTagDialog` (`ai_tag_dialog.py`).
- [x] Upgrade OpenCV fast-path tensor decoder (`decode_image_cv2_for_wd14` in `workers.py`) to handle dynamic NCHW/NHWC input layouts, FP16/FP32 dtypes, and alpha-composited white backgrounds.

### Phase 2: Standalone Hailo-10H Proof-of-Concept — ⏳ PENDING HARDWARE
- [ ] Export static INT8 calibrated ONNX model for `WD14 ConvNeXt v2`.
- [ ] Compile `wd14_convnextv2.hef` using the Hailo Dataflow Compiler (DFC).
- [ ] Benchmark standalone inference FPS and power draw across GPU vs SoC NPU vs Hailo-10H.

### Phase 3: Heterogeneous Multi-Device Co-Processing Engine — ⏳ PLANNED (V.33+)
- [x] Implement thread-safe producer-consumer prefetch queue (`prefetch_queue` & `idx_queue`) in `workers.py`.
- [ ] Support simultaneous multi-session worker pairing (`GPU Session + NPU/Hailo Session` pulling from the same `prefetch_queue` concurrently).
- [ ] Measure aggregated FPS scaling across dual accelerators.

### Phase 4: Hybrid Mode UI & Telemetry — ⏳ PLANNED (V.33+)
- [x] Add GPU / CPU-Eco accelerator selector and dynamic batch size controls in `ai_tag_dialog.py`.
- [ ] Add dedicated `Hailo-10H (M.2 Edge AI - 2.5W)` and `⚡ Hybrid Co-Processing (GPU + NPU / Hailo)` modes once Phase 2 `.hef` runtime is validated.
- [ ] Add per-device FPS telemetry split (`GPU FPS` vs `NPU/Hailo FPS`).
