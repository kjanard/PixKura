# PixKura (蔵) v2.4.0 (Milestone V.32)

Release Notes for **PixKura v2.4.0** — Major Architecture Upgrade, Flagship Vision AI Models, Security Hardening, Native MP4 Ugoira Engine & Comprehensive Zero-Leak Hardening.

---

### 🌟 Highlights & New Features

#### 1. 🤖 Flagship AI Tagger Models & Ultra Ensemble (3-in-1)
- **WD v3 EVA02-Large (`SmilingWolf/wd-eva02-large-tagger-v3`)**: Flagship ~300M parameter model recognized as the highest-accuracy anime tagger, excelling in subtle character attributes, rare characters, and complex scenes with zero tag noise.
- **WD v3 ViT-Large (`SmilingWolf/wd-vit-large-tagger-v3`)**: Large Vision Transformer providing rich attention mapping across intricate backgrounds and diverse illustration styles.
- **WD v3 Ultra Ensemble (`wd-v3-ultra-ensemble`)**: A powerhouse 3-in-1 ensemble combining `EVA02-Large`, `SwinV2`, and `ConvNeXt` via soft-voting probability averaging for unmatched reliability.
- **🔍 Re-scan Unknown Characters Option**: Dedicated option allowing users to specifically re-evaluate previously tagged artworks that still lack recognized character tags (`category = 4`), automatically skipping known characters while applying the new AI model to uncover missed characters.
- **Dynamic Batch Selector**: Configurable Batch Size (`Batch = 4 / 8 / 16 / 32`) in `AiTagDialog` to scale smoothly from 2 GB integrated GPUs to 24 GB flagship graphics cards.

#### 2. 🎬 Native `.mp4` Ugoira Video Conversion
- Upgraded `PixivDownloaderWorker.process_ugoira` to compile downloaded Pixiv Ugoira ZIP frames directly into high-efficiency `.mp4` video files via OpenCV (`convert_frames_to_mp4`).
- Eliminates 256-color GIF quantization artifacts, preserves full 24-bit color fidelity, and reduces output file sizes by **70–85%**.
- Full support for Unicode destination paths on Windows via temporary staging, and automatic padding for odd dimensions `(w + w % 2, h + h % 2)` for hardware H.264 decoders.

#### 3. 🔒 Security & Privacy Hardening
- **Windows DPAPI Credential Encryption**: Sensitive credentials (`pixiv_cookie` and `danbooru_api_key`) are automatically encrypted at rest in `config.json` using native Windows `CryptProtectData` (`crypt32.dll`).
- **Thread-Safe Atomic Configuration**: All reads and writes to `config.json` are synchronized with a re-entrant lock and written atomically via `.tmp` + `os.replace`.
- **Ugoira Zip Bomb & Memory Shield**: Enforced strict safety limits (`MAX_UGOIRA_FRAME_BYTES`, `MAX_UGOIRA_TOTAL_BYTES`, `MAX_UGOIRA_FRAMES`) across downloader, viewer, and thumbnail loaders.
- **HTML Injection Protection**: Escaped untrusted filenames, artist names, and tag names (`html.escape`) before rendering in UI labels.

#### 4. ⭐ Favorites & Custom Manual Tagging System
- Added `toggle_file_favorite()`, `is_file_favorite()`, and custom manual tags (`category = 2`), fully preserved across AI Tagger Full Re-scans.
- Quick Favorite toggle button (hotkey `S`) in `LightboxViewerDialog`, right-click context menu toggling, and top toolbar `⭐ Favorites` filter button.
- Native `is:fav` / `is:favorite` boolean search query integration.

#### 5. 🔍 64-bit Perceptual dHash Duplicate Finder
- Fast 64-bit Difference Perceptual Hash (`compute_dhash`) cached in database.
- Dedicated `DuplicateFinderDialog` to scan the library for exact and near-duplicate artworks (configurable Hamming distance threshold `0–10`) with one-click filtering and batch deletion.

#### 6. 📦 One-Click LoRA / Kohya_ss Dataset Exporter
- Export any search query (e.g. `char:hatsune_miku rating:sfw`) into a destination folder with copied images and matching `.txt` caption files (`activation_prefix, character_tags, series_tags, general_tags`).
- Optional Strict SFW safety filtering and sub-25ms instant dialog opening.

---

### 🛡️ Zero-Leak Stability & Performance Hardening

- **SQLite Connection & Handle Leak Elimination**: Fixed Windows handle exhaustion (343,000+ handles and 49.5 GB virtual memory warning). Replaced `threading.local()` connection caching on `QThreadPool` with thread-id keyed persistent caching (`_conns_by_thread`), keeping handles strictly bounded during hours-long unattended operation.
- **HTTP Connection Pooling (Keep-Alive Re-use)**: Converted Pixiv Downloader, Booru Name Updater, and Danbooru API clients to use persistent `requests.Session()` with `HTTPAdapter` connection pools, preventing socket exhaustion (`TIME_WAIT`) and TLS renegotiation latency.
- **UI Memory Capping & Widget Cleanup**:
  - Capped console log buffers to 2,000 blocks in `download_dialog.py` and `ai_tag_dialog.py` to prevent `QTextDocument` memory bloat during 100,000+ item scans.
  - Added `WA_DeleteOnClose` across `MinimizableDialog` to ensure Qt dialog C++ objects are promptly freed on close rather than retained in parent window children.
  - Implemented `_cleanup_resources()` in `LightboxViewerDialog` to release 4K/8K bitmap buffers and path lists upon closing.
  - Added `WD14Tagger.unload()` to release DirectML / ONNX Runtime GPU memory immediately after scanning.
- **Directory Search Handle Cleanup**: Bounded directory scanning using `with os.scandir(...)` to release Windows find-file handles instantly.

---

### 🧪 Automated Verification
- **47/47 Unit Tests Passing** (`run.bat --test`): Covers AI tagging, thread connection reuse, query parser, UI dialogs, MP4 conversions, and leak prevention.
