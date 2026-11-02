# Changelog - PixKura (蔵)

All notable changes to the PixKura project will be documented in this file.

---

## [Version 2.4.0-V.32] - 2026-11-02

### Added (Security Hardening, Native `.mp4` Ugoira, Favorites, Duplicate Finder & Dataset Exporter)
* **Native `.mp4` Ugoira Video Conversion (`workers.py`)**:
  * Upgraded `PixivDownloaderWorker.process_ugoira` (and pre-emptive skip checks across `ILLUST`, `ARTIST`, and `BOOKMARK` modes) to compile downloaded Pixiv Ugoira ZIP frames and variable per-frame delays directly into high-efficiency `.mp4` video files via OpenCV (`convert_frames_to_mp4`).
  * Eliminates 256-color GIF quantization artifacts, preserves full 24-bit color fidelity, reduces output file sizes by 70–85%, supports Unicode destination paths on Windows via temporary staging, and automatically pads odd dimensions `(w + w % 2, h + h % 2)` for H.264/MPEG-4 hardware decoder compatibility (with automatic fallback to `.gif` if OpenCV is unavailable).
* **Security & Privacy Hardening (`config.py`, `utils.py`, `tagger.py`, `lightbox_viewer.py`, `ai_tag_dialog.py`, `main.py`)**:
  * **Windows DPAPI Credential Encryption**: Implemented `encrypt_secret()` and `decrypt_secret()` in `config.py` using native Windows `CryptProtectData` / `CryptUnprotectData` (`crypt32.dll`). Sensitive credentials (`pixiv_cookie` and `danbooru_api_key`) are now automatically encrypted at rest in `config.json`.
  * **Thread-Safe Atomic Configuration (`load_app_config`, `update_app_config`)**: All reads and writes to `config.json` are now synchronized with a re-entrant lock and written atomically via `.tmp` + `os.replace`, preventing lost settings or JSON corruption during concurrent UI/worker updates.
  * **Ugoira Zip Bomb & Memory Exhaustion Shield**: Enforced `MAX_UGOIRA_FRAME_BYTES` (35 MB/frame), `MAX_UGOIRA_TOTAL_BYTES` (350 MB/archive), and `MAX_UGOIRA_FRAMES` (2,000 frames) across `PixivDownloaderWorker`, `LightboxViewerDialog.load_ugoira_frames`, and `utils.load_media_thumbnail`.
  * **HTML Injection Protection (`html.escape`)**: Escaped untrusted filenames, artist names, and tag names before rendering in `QLabel` and `QTextEdit` across `main.py`, `lightbox_viewer.py`, and `ai_tag_dialog.py`.
  * **Model Download Size Verification**: Added strict `downloaded == total_size` verification in `WD14Tagger.download_models()` before renaming `.download` temporary files to prevent corrupted ONNX model loads on dropped connections.
* **⭐ Favorites & Custom Manual Tagging System (`database.py`, `lightbox_viewer.py`, `main.py`, `query_parser.py`)**:
  * Added `toggle_file_favorite()`, `is_file_favorite()`, `add_custom_file_tag()`, and `remove_file_tag()` in `database.py` (using `category = 2` for custom tags and `__favorite__` marker, preserved across AI Tagger Full Re-scans).
  * Added `⭐ Favorite` toggle button (shortcut `S`) and custom tag input box (`+ tag_name`) in `LightboxViewerDialog`, plus right-click context menu favorite toggling in gallery views and a top toolbar `⭐ Favorites` quick filter button.
  * Added `is:fav` / `is:favorite` support in `query_parser.py` and `StreamScanner`.
* **🔍 Duplicate & Similar Artwork Finder (`utils.py`, `workers.py`, `components.py`, `dashboard_dialog.py`, `main.py`)**:
  * Added 64-bit Difference Perceptual Hash (`compute_dhash`, `hamming_distance_hex`) in `utils.py` and `dhash` column migration on `file_index` in `database.py`.
  * Added `DuplicateScannerWorker` and `DuplicateFinderDialog` to scan the library for exact and near-duplicate artworks (configurable Hamming distance threshold `0–10`) with one-click filter preview in the main gallery.
* **📦 One-Click LoRA / Kohya_ss Dataset Exporter (`workers.py`, `components.py`, `dashboard_dialog.py`, `main.py`)**:
  * Added `DatasetExportWorker` and `DatasetExporterDialog` to export any search query (e.g. `char:hatsune_miku rating:sfw`) into a destination directory with copied images and matching `.txt` caption files (`activation_prefix, character_tags, series_tags, general_tags`) and optional Strict SFW safety filtering.
* **⚡ AI Hardware Accelerator Expansion & Dynamic Batch Selector (`tagger.py`, `ai_tag_dialog.py`, `workers.py`, `utils.py`)**:
  * Added `OpenVINOExecutionProvider` and `QNNExecutionProvider` detection alongside `DmlExecutionProvider` and `CUDAExecutionProvider` in `WD14Tagger`, plus NPU hardware detection in `utils.detect_ai_accelerators()`.
  * Added configurable Batch Size selector (`Batch = 4 / 8 / 16 / 32`) in `AiTagDialog` to scale from 2 GB integrated GPUs to 24 GB flagship GPUs.

### Fixed & Optimized
* **OpenCV Alpha Channel Black-Background Bug in AI Tagger (`workers.py`)**:
  * Fixed critical accuracy bug in `AiTaggingWorker` where `cv2.imdecode(..., cv2.IMREAD_COLOR)` stripped the alpha channel of transparent PNGs and turned transparent regions pitch black `(0, 0, 0)` instead of white `(255, 255, 255)`, causing WD14/WDv3 models to mispredict `black_background` / `dark` tags and miss character features. Implemented `decode_image_cv2_for_wd14()` using `IMREAD_UNCHANGED` with fast vectorized white-background alpha compositing, 2D grayscale handling, and dynamic `input_dtype`/`input_shape` support.
* **Qt Deleted C++ Object `RuntimeError` Crash Fix (`utils.py`, `workers.py`, `main.py`, `lightbox_viewer.py`)**:
  * Eliminated `RuntimeError: wrapped C/C++ object of type QPushButton / WorkerSignals has been deleted` (logged in `error.log`) by introducing `safe_single_shot()` in `utils.py` and `_safe_emit()` in `workers.py`.
* **`artist:` Search Query Resolution & B-Tree Index Optimization (`query_parser.py`, `workers.py`)**:
  * Fixed bug where `artist:name` tokens were parsed into `SearchQuery` but never resolved against the `artists` table or `file_index`. Implemented `_resolve_artist_paths()` in `query_parser.py` and `StreamScanner`.
  * Optimized `_query_tag_paths_smart()` in `query_parser.py` to perform an O(log N) exact B-Tree index seek (`WHERE tag_name = ?`) on `idx_tag_name` first before falling back to `LIKE` substring matching, dramatically accelerating multi-tag queries on 7.7M+ tag rows.
* **Orphaned Database Rows & >100% Dashboard Coverage Fix (`database.py`, `workers.py`)**:
  * Added `cleanup_orphaned_records()` to purge orphaned rows in `file_tags` and `file_thumbnails` when files/folders are deleted during `FolderCacheScanner` Quick Sync or `DatabaseOptimizerWorker` (`VACUUM`).
  * Bounded `tagged_files` and `nsfw_count` in `get_dashboard_analytics()` against active `file_index` paths so AI Tagged coverage never exceeds 100%.
* **Detail View (`page_detail`) Viewport Lazy Loading & LRU Cache (`main.py`)**:
  * Replaced eager full-directory thumbnail loading in `load_detail_images()` with viewport-based lazy loading (`load_visible_detail_thumbnails`, `detail_scroll_timer`) and a bounded `detail_icon_cache` (`MAX_DETAIL_CACHE = 300`), preventing UI freezes when opening artist folders containing 1,000+ illustrations.
* **Clean SQLite WAL Shutdown & Console Stderr Preservation (`workers.py`, `main.py`)**:
  * Added `close_all_thread_local_conns()` to track and close all worker thread-local SQLite connections prior to `PRAGMA wal_checkpoint(TRUNCATE)` on shutdown and `VACUUM`.
  * Made C-level `stderr` redirection conditional on running `main.py` directly without `--console`/`--debug`, preserving console output during CLI debugging and `unittest` execution.
* **Test Suite Reorganization & Bootstrapper Upgrade (`tests/`, `run.bat`, `requirements.txt`)**:
  * Relocated all automated unit tests and test media (`test_ai_tagger.py`, `test_query_parser.py`, `test_ui_dialogs.py`, `test_video.mp4`) into a dedicated `tests/` directory (`42/42` tests passing).
  * Upgraded `run.bat` with UTF-8 codepage (`chcp 65001`), `.venv`/`venv` awareness in `--console` mode, and a new `run.bat --test` (`-t`) command to execute the entire unit test suite.

---

## [Version 2.3.4-V.31.2] - 2026-10-09

### Added & Improved (Danbooru API Engine & Artist Resolver Overhaul)
* **Full Danbooru API Integration (Powered by DanbuDL Engine) (`danbooru_api.py`, `workers.py`)**:
  * Adapted high-efficiency REST client from sibling project `DanbuDownloader` (`DanbuDL`).
  * **Standardized User-Agent**: Configured official User-Agent format `PixKura/{VERSION} (DanbuDL-Engine; ...)` adhering to Danbooru API guidelines.
  * **Automatic Credential Discovery**: Seamlessly detects and applies Danbooru API credentials from local `config.json` or fallback to sibling `DanbuDownloader/config.json`.
  * **Thread-Safe Rate Limiting & Connection Pooling**: Utilizes shared `requests.Session()` with mutex-locked polite pacing (~0.8s), preventing concurrent threads from triggering HTTP 429 throttling.
  * **Exponential Backoff**: Resilient retry loop parsing Danbooru's `Retry-After` header and handling transient 5xx server errors.
  * **Exact URL Boundary Matching**: Enforces strict regex validation on returned artist profile URLs (`pixiv.net/(?:users|fanbox/creator|u)/{aid}`) to prevent ID prefix collisions (e.g. short ID 11 matching 115734584).
  * **Optimized Payload**: Queries minimal attributes (`only=id,name,other_names,urls`) to drastically cut payload size and response latency.

* **Permanent Resolution for Deleted Accounts & Name Update Loop (`workers.py`, `main.py`)**:
  * Solved the perpetual 88-artist update loop: Detects when accounts have officially left Pixiv (HTTP 404 / `"User has left pixiv"`) with no Booru records, saving them as `[Deleted User]` in `pixiv_artists.db`.
  * Subsequent scans skip deleted accounts, allowing "Update Names" to reach 100% completion cleanly.
  * Real-time UI updates: Displays `[Deleted User] (ID)` immediately in the folder list view.

* **High-Resolution Artist Avatar Auto-Fetch (`workers.py`, `main.py`)**:
  * Automatically retrieves artist profile avatars (`imageBig`) directly from Pixiv during name resolution, storing the image blob into `artists.profile_blob` and overlaying it on folder cover collages.

* **Single-Folder Context Menu Name Updater (`main.py`)**:
  * Added `🔄 Update Artist Name (ค้นหาชื่อศิลปิน)` to the folder right-click context menu, enabling instant individual name and avatar refreshes.

* **Updated Configuration Template (`config.json.example`)**:
  * Added `lightbox_bg_mode`, `danbooru_username`, and `danbooru_api_key` to the configuration template for full out-of-the-box reference.

---

## [Version 2.3.3-V.31.1] - 2026-10-08

### Added & Improved (Transparent PNG & Line Art Rendering Overhaul)
* **Smart Luminance-Based Background Engine (`lightbox_viewer.py`)**:
  * Fixed critical usability issue where black line art and sketch illustrations on transparent backgrounds (e.g. `70151194_p11 - Wendy Marvell 147.png`) were virtually invisible when rendered against the viewer's dark gray (`#0c0c0e`) background.
  * Added **Streamlined Background Canvas Selector** (`combo_bg`) in Lightbox Viewer with 3 intuitive modes:
    * `✨ BG: Auto`: **Smart AI Luminance Analysis**. Analyzes visible pixels in milliseconds — automatically chooses a pristine **White Canvas** for dark/black line drawings and manga pages, and retains **Dark Canvas** for opaque illustrations and bright/white artworks.
    * `⚪ BG: White`: Clean pure white canvas (`#ffffff`), rendering sketches and manga line arts like real printed paper.
    * `⬛ BG: Dark`: Classic dark canvas (`#0c0c0e`).
  * **Interactive Keyboard Shortcut (`B` / `Ctrl+B`)**: Instant one-key cycling between `Auto` ➔ `White Canvas` ➔ `Dark Canvas` with real-time floating on-screen HUD toast badge feedback.
  * **Preference Persistence**: User's chosen background mode is automatically saved in `config.json` (`lightbox_bg_mode`).
  * **Artwork Frame Boundary**: Added subtle 1px border framing around the active artwork canvas so users can clearly distinguish the artwork edges from the application window.

* **Alpha-Composited Thumbnail Generation (`utils.py`, `workers.py`)**:
  * Implemented `make_thumbnail_rgb()` in `utils.py`: Safely composites transparent images (RGBA, LA, paletted with alpha) onto a clean White (or Dark for bright lines) background before converting to RGB JPEG for SQLite caching.
  * Completely eliminates the bug where Pillow's default `.convert('RGB')` dropped the alpha channel and turned transparent line art images into completely pitch black squares in the gallery grid.
  * Integrated across `ForegroundThumbnailWorker`, `BackgroundThumbnailPreloader`, and folder collage compositing.
* **Instant AI Tagger Dialog Startup Optimization (`ai_tag_dialog.py`, `utils.py`, `database.py`)**:
  * **12x Speedup (from ~4.0s delay down to ~0.3s instant pop-up)** when opening the AI Tagger dialog from the main window.
  * **Direct Win32 API Hardware Drive Query (`utils.py`)**: Replaced slow synchronous PowerShell subprocesses (`Get-Partition` & `Get-PhysicalDisk` taking ~2,000 ms) with native Windows `DeviceIoControl` (`StorageDeviceSeekPenaltyProperty` via `ctypes` taking < 0.1 ms).
  * **Asynchronous Database Aggregation (`ai_tag_dialog.py`, `database.py`)**: Moved heavy `COUNT(DISTINCT)` tag statistics queries across 7.7+ million database rows to background thread pool (`TagStatsWorker` via `QRunnable`) with a 60-second in-memory cache, completely removing UI thread freeze upon dialog initialization.


---

## [Version 2.3.2-V.31] - 2026-10-04

### Added
* **Project Rebranding to PixKura (蔵) by Kurito**:
  * Officially rebranded the project from 'Pixiv Manager' to **PixKura (蔵)** to establish a unique identity, celebrate the author **Kurito**, align with the **P** & **K** icon monogram, and ensure full trademark safety on GitHub.
* **Universal Minimizable Dialogs Architecture (`MinimizableDialog`)**:
  * Introduced base class [MinimizableDialog](file:///d:/MyProject/test_pixiv/components.py) in `components.py` equipped with `WindowMinimizeButtonHint` and `WindowMaximizeButtonHint`.
  * Applied across all 5 application dialogs:
    * `PixivDownloadDialog` ([download_dialog.py](file:///d:/MyProject/test_pixiv/download_dialog.py))
    * `AiTagDialog` ([ai_tag_dialog.py](file:///d:/MyProject/test_pixiv/ai_tag_dialog.py))
    * `DashboardDialog` ([dashboard_dialog.py](file:///d:/MyProject/test_pixiv/dashboard_dialog.py))
    * `SearchHelpDialog` ([components.py](file:///d:/MyProject/test_pixiv/components.py))
    * `LightboxViewerDialog` ([lightbox_viewer.py](file:///d:/MyProject/test_pixiv/lightbox_viewer.py))
  * **Two-Way Taskbar Synchronization**:
    * Clicking the **Minimize `[-]`** button on any dialog immediately minimizes both the dialog and the parent `QMainWindow`, cleanly sending the entire application to the Windows Taskbar so users can freely work on other tasks while long downloads or AI taggings run.
    * Restoring the app from the Windows Taskbar automatically restores the main window and re-activates the active dialog directly in front.

### Fixed & Improved (Pixiv Download Manager)
* **Accurate Cancel State & Lifecycle Management**:
  * Fixed critical bug where cancelling a download in Illust, Artist, or Bookmark mode incorrectly reported `"Download completed successfully!"` and completed the progress bar.
  * Now properly displays `"Download Cancelled"` information dialog and preserves download status without false success popups.
* **Corrupt/Partial File Prevention**:
  * Re-engineered `download_image` to automatically delete incomplete 0-byte or partially downloaded image files if interrupted or cancelled by the user.
* **Instant Cancellation Response (`interruptible_sleep`)**:
  * Replaced blocking `time.sleep` calls with `interruptible_sleep`, allowing Cancel requests to abort immediately (<0.3s) even during 15–30s human-like session breaks.
* **Pixiv URL Auto-Extraction & Smart Mode Detection**:
  * `Target ID` now accepts full Pixiv URLs (e.g. `https://www.pixiv.net/artworks/76691698`, `https://www.pixiv.net/users/7819951`), automatically parsing the numeric ID and switching between Illust and Artist modes seamlessly.
* **UI Controls Locking During Download**:
  * Automatically locks mode radio buttons, ID inputs, bookmark range, and browse controls during active downloads to prevent inconsistent UI states, restoring them cleanly upon finish or cancel.
* **Private Bookmarks Pre-validation**:
  * Prevents starting Private Bookmarks extraction without a `PHPSESSID` cookie, alerting the user immediately instead of failing halfway with Pixiv API errors.
* **R-18 Detection & Skip Logging in Gallery/Bookmark Modes**:
  * Added explicit log messages when R-18 artworks are skipped due to missing `PHPSESSID` cookie, eliminating confusion about missing files.
* **Artist Mode Descending Sort**:
  * Sorted artist works descending by ID (newest to oldest) so max download limits capture the artist's latest creations first.
* **R-18 Ugoira Animation 404 Fix**:
  * Fixed 404 error when downloading R-18 animated illustrations by injecting session cookie headers into metadata queries, enabling seamless ugoira zip retrieval and GIF conversion.
* **Cookie Input UI & Auto-Persistence**:
  * Enabled the `PHPSESSID` cookie input across Illust and Artist modes for restricted content access, and automatically saves the cookie into `config.json` upon initiating downloads.
* **Network Resilience**:
  * Increased API call timeouts from 5s to 15s across all metadata endpoints.
* **GitHub Repository Preparation & Cross-Platform Support**:
  * Created `config.json.example` template and updated `.gitignore` to safeguard against committing the 5.8 GB database, large AI models, and sensitive personal cookies.
  * Added non-Windows fallback for `onnxruntime` in `requirements.txt` to support Linux and macOS environments.

---

## [Version 2.3.1-V.30] - 2026-09-27

### Added
* **Full Boolean Logic Query Engine (`AND`, `OR`, `NOT`, `NOR`)**:
  * **Conjunction Operators (`AND`, `&&`)**: Full support for explicit `AND` and `&&` keywords (e.g. `tag:crossed_legs AND tag:toes`, `1girl && barefoot`) alongside default whitespace conjunction, preventing `and` from being misinterpreted as a plain keyword.
  * **Exclusion & Negation (`NOT`, `!`, `-`)**: Full support for standalone `NOT` (e.g. `tag:swimsuit NOT tag:glasses`, `solo NOT glasses`) and exclamation mark `!` prefix (e.g. `solo !glasses`), complementing the existing `-` negation syntax.
  * **Neither / Nor Operator (`NOR`)**: Native support for `NOR` boolean logic ($A \text{ NOR } B \equiv \text{NOT } A \land \text{NOT } B$, e.g. `cat_ears NOR dog_ears` or `tag:a NOR tag:b`), automatically excluding both tags simultaneously.
  * **Advanced Multi-Group `OR` & `||`**: Enhanced `OR` operator to properly strip prefixes (`tag:cat_ears OR tag:dog_ears`), chain multi-term alternatives (`a OR b OR c`), and support `||` syntax.
  * **Standalone Pure Negation Queries**: Fixed database path resolution when searching only exclusion tags (e.g. `-glasses`, `NOT swimsuit`, `cat_ears NOR dog_ears`), accurately subtracting exclusions from all scanned files without returning empty sets.
* **Lightbox Viewer Sizing Modes & Stability Overhaul (`lightbox_viewer.py`)**:
  * **Zero Window Overflow (`paintEvent` Architecture)**: Re-engineered `ZoomableImageLabel` to inherit from `QWidget` using custom `paintEvent` (`QPainter`) with a fixed `sizeHint`, completely eliminating the critical bug where `QLabel.setPixmap` expanded the parent `QDialog` beyond screen boundaries and locked zoom out.
  * **Sizing Mode ComboBox (`combo_scale`)**: Added top-bar dropdown selector supporting 3 viewing modes:
    * `🔍 Fit to Window`: Scales image to fit cleanly within the viewport while preserving full aspect ratio.
    * `📐 Fill / Cover`: Expands image to cover the entire viewport without letterboxing, centered with pan capability.
    * `🎯 Actual Size (1:1)`: Maps 1 image pixel to 1 screen pixel for 100% native clarity.
  * **Live Zoom Synchronization**: Automatically updates the dropdown to `🔍 Zoom: XX%` during manual wheel zooming.
  * **Persistent View Mode**: Remembers the selected sizing mode (`Fit`, `Fill`, or `Actual Size`) when navigating through images with `⟨` Prev / `⟩` Next.
  * **Smooth Pan & Cursor-Anchored Wheel Zoom**: Smooth mouse wheel zooming anchored directly to the cursor coordinate, with viewport bounds clamping and draggable panning.
  * **Quick Keyboard Shortcuts**: Added `1` (Fit), `2` (Fill), `3` (Actual Size), `+`/`=` (Zoom in), `-` (Zoom out), `0`/`R` (Reset to Fit), and `F` (Fullscreen).
* **Automated Test Suites**:
  * Expanded `test_query_parser.py`: Comprehensive test cases for `AND`, `OR`, `NOT`, and `NOR` operators (8/8 tests passing).
  * Expanded `test_ui_dialogs.py`: Automated tests for `combo_scale` view modes, fixed sizeHint stability, and zoom calculations (4/4 tests passing).

---

## [Version 2.3.0-V.29] - 2026-09-26

### Added
* **Danbooru Boolean Multi-Tag Query Engine (`query_parser.py`)**:
  * Full Danbooru-style boolean syntax support: `AND` (spaces), `OR` operators, and `-negation` (e.g. `solo 1girl swimsuit -glasses`, `cat_ears OR dog_ears`).
  * Prefix search syntax: `char:`, `series:`, `artist:`, `tag:`, `type:` (`gif`, `video`, `png`, `ugoira`), and `size:` (`>5mb`, `<1mb`).
  * Dual keyword matching: Plain keywords seamlessly match against both image filenames AND AI Danbooru tags.
  * Ultra-fast sub-second index query execution (`resolve_matching_paths`) across 2.45+ million database rows (< 10ms).
* **Strict Content Safety & Rating Filter (Colab-Proof)**:
  * Dedicated toolbar Safety dropdown: `All (ทั้งหมด)`, `🟢 SFW Only (ปลอดภัย / ไม่โป๊)`, `🔞 NSFW Only (18+ / ผู้ใหญ่)`, `⚠️ Sensitive / Ecchi`, `🔞 Questionable`, and `🔞 Explicit`.
  * Hybrid classification algorithm cross-referencing AI tagger rating categories (5, 9) and explicit NSFW anatomical keywords (`nude`, `nipples`, `sex`, etc.) with verified 0% overlap between SFW and NSFW, strictly preventing 18+ images from leaking into Google Colab LoRA training datasets.
  * Direct query syntax integration: `rating:sfw`, `rating:nsfw`, `is:sfw`, `is:ecchi`.
* **Search Syntax Guide & Quick-Try Modal (`❓ Help`)**:
  * Added `SearchHelpDialog` in `components.py` with modern dark-mode layout, comprehensive syntax documentation, and operator cheat sheets.
  * Interactive clickable quick-try example pills (`🛡️ SFW Only`, `🔞 NSFW Only`, `👗 Swimsuit -Glasses`, `👤 Miku SFW`, `🎬 GIF Only`, `📦 Large (>5MB)`) that immediately populate the search bar and filter results.
* **Visual Dashboard & Interactive Tag Cloud (`dashboard_dialog.py`)**:
  * New `📊 Dashboard` button on the top toolbar styled with indigo accent theme.
  * Library Overview KPI Cards: Total Artworks, Artist Folders, AI Tagged coverage %, SFW safe count, and NSFW adult count.
  * Color-coded Safety Ratio visual progress bar (Green for SFW vs Red for NSFW).
  * 25-character Leaderboard with relative frequency progress bars and 1-click search filtering.
  * Top Series & Franchises ranking with instant filter clicks.
  * Interactive Tag Cloud displaying 60 top Danbooru tags with hot tags highlighted in vibrant amber/red gradients.
* **In-App Lightbox / Fast Image Viewer (`lightbox_viewer.py`)**:
  * Instant double-click preview directly inside the application with 0ms lag, replacing slow external viewer launches.
  * Smooth zoom up to 500% via mouse scroll wheel, click-and-drag panning, and 100% zoom toggle.
  * Keyboard rapid photo cycling using `Left`/`Right` arrow keys, `A`/`D` keys, and `Space`.
  * Collapsible AI Metadata sidebar showing file dimensions, file size, rating badges, detected characters (with confidence %), series, tags, and one-click `📋 Copy All Tags` action for LoRA training prompts.
  * Quick action buttons for `📂 Open in Explorer` and fallback to external Windows Photos.
* **Automated Test Suites**:
  * Added `test_query_parser.py`: Unit tests for boolean parsing, prefixes, and zero-overlap SFW/NSFW database queries.
  * Added `test_ui_dialogs.py`: Unit tests for Dashboard analytics calculation, Lightbox viewer, and Search Help dialogs. (Total 28/28 tests passing).

---

## [Version 2.2.0-V.28] - 2026-08-23
* Integrated local anime vision AI tagger supporting WD14 Tagger (v1.4) and WD Tagger v3 Series (ConvNeXt, ViT, SwinV2) by SmilingWolf with DirectML GPU, FP16, and Batch=16 inference.

## [Version 2.1.0-V.27] - 2026-08-17
* Implemented Viewport-Based Icon Management (92% RAM reduction), Smooth Lookahead Overscan Buffer, and Bounded LRU Caches.

## [Version 2.0.0-V.26] - 2026-07-09
* Replaced `QListWidget` with MVC `QListView` + `ThumbnailListModel` and added thread-local SQLite connection pooling.

## [Version 1.0.0-V.25] - 2026-07-01
* Added Account-Safe Stealth Downloader Tool, Dark Mode QSS Stylesheet, and Parallel Background Preloading.

## [Version 0.9.0-V.24] - Historical Baseline
* Initial implementation of the Pixiv Manager app.
