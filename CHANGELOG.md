# Changelog - PixKura (蔵)

All notable changes to the PixKura project will be documented in this file.

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

### Changed
* **WD14 ConvNeXt V2 Upgrade**: Updated AI tagger to ConvNeXt V2 with FP16 half-precision, using `op_block_list=['ReduceSumSquare', 'Sqrt', 'Div', 'ReduceMean', 'GlobalAveragePool']` to eliminate GRN layer FP16 overflow/NaNs.
* **Danbooru API Integration**: Added Danbooru as Source 2 in `BooruNameUpdateWorker` with custom User-Agent `PixivManager/2.2.0` (avoiding Cloudflare 403 blocks), with Safebooru as fallback.
* **Requirements Modernization (`requirements.txt`)**: Reorganized into logical categories with Windows DirectML environment markers (`sys_platform == 'win32'`) and Python 3.12 compatible version baselines.
* **Bootstrapper Upgrade (`run.bat`)**: Added virtual environment (`.venv`/`venv`) auto-detection, standard `start ""` syntax, Windows `pyw` launcher support, dynamic `Python3*` search, and `--console` / `--debug` mode.

### Fixed
* **Cover Collage Randomize [X] Bug**: Fixed issue where closing the randomize dialog via `[X]` or `Esc` returned `None`, triggering an unintended Full Rebuild of all covers.
* **4-File Cover Randomization**: Fixed `composite()` in `workers.py` to shuffle and regenerate collage covers even when `len(files) <= 4`.
* **Rating Tag Category Recognition**: Fixed `tagger.py` line 312 to recognize category 9 tags (`general`, `sensitive`, `questionable`, `explicit`) from SmilingWolf CSV metadata.
* **Missing Logging Import**: Added missing `import logging` in `database.py`.
* **Missing QApplication Import**: Added missing `QApplication` import in `components.py`.

---

## [Version 2.2.0-V.28] - 2026-08-23

### Added
* **AI Character & Tag Classifier (Local DirectML GPU Engine)**: Integrated local anime vision AI tagger supporting WD14 Tagger (v1.4) and the new WD Tagger v3 Series (ConvNeXt, ViT, SwinV2) by SmilingWolf.
  * **Multi-Model Registry & One-Click Downloader**: Built-in multi-model selector in the AI Tag Dialog supporting `WD14 ConvNeXt v2`, `WD Tagger v3 ConvNeXt` (~10,000+ tags), `WD Tagger v3 ViT`, `WD Tagger v3 SwinV2`, and `👑 WD Tagger v3 Ensemble (3-in-1)` with live download progress tracking.
  * **Ultra-Accurate 3-in-1 Ensemble Engine**: Added Soft-Voting Probability Averaging (`probs = mean(p1, p2, p3)`) combining ConvNeXt, ViT, and SwinV2 architectures simultaneously in GPU VRAM to filter out false-positive noise and maximize character identification precision.
  * **DirectML GPU Acceleration**: Native DirectX 12 acceleration on Windows supporting AMD Radeon (RDNA/RDNA2/RDNA3), NVIDIA GeForce RTX/GTX, and Intel Arc GPUs without CUDA lock-in.
  * **FP16 Half-Precision Optimization**: Automated conversion of ONNX models to FP16 half-precision, leveraging AMD RDNA2 Rapid Packed Math (RPM) for 2x mathematical throughput and 50% model size reduction (~185 MB).
  * **Dynamic Batched GPU Inference (Batch=16)**: Patched ONNX model graphs for dynamic batch dimensions, stacking 16 image tensors per single GPU kernel launch to boost raw inference throughput from 32 FPS to ~200–290 FPS.
  * **C++ OpenCV Image Preprocessing**: Integrated `cv2.imdecode` (via `np.fromfile` for 100% Unicode path support on Windows) to release Python's GIL during image decoding, providing up to 1,500+ FPS supply rate across 16 threads.
  * **Adaptive Storage Profiles & Auto-Detect (HDD vs SSD)**: Automatic physical drive hardware detection (`Get-Partition` + `Get-PhysicalDisk`); automatically applies directory-based file sorting (`sort by dirname`) and limits I/O concurrency to 3–4 threads on spinning HDDs to eliminate mechanical head thrashing, while scaling to 16 threads on NVMe/SATA SSDs.
  * **Real-time Stopwatch & Performance Metrics**: Added live running stopwatch timer (`⏱️ ใช้ไป: mm:ss`), auto-freezing upon completion, paired with monotonically stable real-time average FPS and accurate ETA remaining countdown.
  * **Quick Mode vs Full Re-scan Selector**: Prominent scan mode selector in AI Tag Dialog defaulting to **Quick Mode** (scans only untagged files, automatically skipping files that already have tags in SQLite) alongside **Full Re-scan** mode for refreshing existing tags with new models.
  * **Full Test Suite (`test_ai_tagger.py`)**: 20 comprehensive unit tests covering ONNX providers, model conversions, dynamic batching, ensemble soft voting, database indexing, and thread workers.

### Changed
* **SQLite Tagging Schema**: Created `file_tags` table with composite indexes (`idx_file_tags_path_cat`, `idx_file_tags_name`) for instant multi-tag lookup and aggregate statistics (`get_tag_stats`).
* **C-Level Warning Suppression**: Configured `OPENCV_LOG_LEVEL = OFF`, comprehensive `QT_LOGGING_RULES` (font, imageio, icc), and C-runtime stderr redirection in `main.py` to completely silence benign `libpng warning: iCCP/eXIf` console noise while preserving full Python exception logging in `error.log`.

---

## [Version 2.1.0-V.27] - 2026-08-17

### Added
* **Smooth Lookahead Overscan Buffer**: Implemented proactive viewport lookahead that pre-renders 50 items behind and 120 items ahead in Folder View (30 behind and 80 ahead in File View), completely eliminating gray placeholder flashes during scrolling.
* **Bounded LRU Cache Architecture**: Integrated dual `OrderedDict` Least-Recently-Used caches (`MAX_FOLDER_CACHE = 600` and `MAX_FILE_CACHE = 400`) to maintain 0ms instant rendering when scrolling back and forth while strictly capping RAM usage at ~300–450 MB.
* **Window Resize & State Change UI Event Triggers**: Added `resizeEvent` and `changeEvent (WindowStateChange)` handlers in `PixivManagerApp` to automatically recalculate and decode visible thumbnails whenever the window is resized, maximized, or restored.
* **Instant Sort Re-Rendering**: Added immediate post-sort buffer re-triggers in `apply_sort()` to decode newly positioned items instantly upon changing sort modes (Name, Date, File Size, File Count, Random).

### Changed
* **Viewport-Based Icon Management (92% RAM Reduction)**: Replaced full-memory `QIcon`/`QPixmap` instantiation for all 15,744+ folders with in-memory compressed raw JPEG byte storage (~10 KB per folder), slashing application RAM consumption from **3,775 MB to ~303 MB**.
* **Ultra-Low Debounce Timers**: Decreased scrolling debounce latency from 100ms to 15–25ms across folder and file views for near real-time rendering.
* **SQLite Memory & Thread Pool Tuning**: Adjusted global SQLite PRAGMA tuning (`mmap_size` from 2 GB to 128 MB and `cache_size` from 20 MB to 4 MB per connection). Capped `bg_pool` at max 6 threads, `fg_pool` at max 4 threads, and `BackgroundThumbnailPreloader` at max 3 threads to prevent thread-local connection overhead.
* **Immediate Memory Release**: Configured `all_scanned_files` to explicitly clear memory immediately after UI model construction.

### Fixed
* **Back Navigation Placeholder Freeze**: Fixed a bug in `go_back()` where returning to the Files view (`page_files`) did not trigger the file viewport timer, leaving thumbnails as gray placeholders until the user scrolled.
* **Stream Scanner Latency**: Reduced post-scan UI trigger delay in `handle_stream_finished` from 100ms to 25ms for faster file search results.
* **Real-time Cover Randomization Update**: Fixed an issue in `handle_folder_updated` where newly randomized folder covers were not immediately rendered on-screen if not yet registered in active cache.
* **Live Avatar Overlay on Name Updates**: Fixed an issue in `handle_api_update` to ensure newly fetched Pixiv/Booru profile avatars are composited and updated on visible grid items in real-time.

---

## [Version 2.0.0-V.26] - 2026-07-09

### Added
* **MVC Architecture for UI**: Replaced rigid `QListWidget` grids with a high-performance `QListView` and `ThumbnailListModel` data architecture, significantly boosting rendering speed and eliminating UI freezes on large datasets.
* **Dedicated Download Dialog**: Extracted the "Pixiv Download Manager" into a standalone `download_dialog.py` to decouple UI components and improve code organization.
* **Corrupt Image Error Indicator**: Implemented a visual "ERR" badge placeholder for corrupted or 0-byte images instead of rendering blank dark boxes.
* **Truncated Image Recovery**: Enabled Pillow's `LOAD_TRUNCATED_IMAGES` to salvage and display partially downloaded image files.

### Changed
* **SQLite Concurrency Optimization**: Replaced shared SQLite connections with thread-local pooling (`threading.local()`) and injected database rollback mechanisms in `workers.py`, eliminating WAL mode database lockups.
* **Instant Sorting Engine**: Rewrote sorting logic (Size, Date, Natural Name) to run instantly in-memory inside `ThumbnailListModel`'s dictionary array (`O(N log N)`) instead of traversing and sorting UI widgets.
* **Dead Code Pruning**: Removed obsolete UI list queues and deprecated view refresh handlers that were left over from earlier iterations.

---

## [Version 1.0.0-V.25] - 2026-07-01

### Added
* **Stealth Downloader Tool (Account-Safe)**: Added a built-in download tool that uses public AJAX endpoints, bypassing OAuth login and completely eliminating account ban risks.
  * Implemented randomized download delays (2–5 seconds) and automatic session breaks (15–30 seconds after every 10 files) to prevent IP blocking.
  * Added dynamic dialog logs (`QTextEdit`) and progress tracking.
  * Configured automatic caching checks to skip existing files.
  * **Pixiv Bookmark Downloader**: Integrated a safe bookmark extractor that retrieves bookmarked illust IDs (publicly or privately via `PHPSESSID` cookie) and automatically routes them to the anonymous download queue, preventing account correlation.
  * **R-18/Restricted Support**: Enabled the `PHPSESSID` cookie option in all modes (Illust, Artist, Bookmarks) to fetch metadata of restricted/R-18 works, while keeping the actual image downloads 100% anonymous.
  * **Ugoira (Animation) Conversion Support**: Integrated an automated Ugoira engine that downloads animated illustrations as source ZIP files and compiles them into standard animated GIF files using frame-delay metadata.
* **Premium Dark Mode QSS Stylesheet**: Overhauled user interface with dark colors (`#121214`), rounded panels, active glow focus outlines, and customized list view cards.
* **Vector-based Loading Overlay**: Added `LoadingSpinner` and `LoadingOverlay` widgets in [components.py](file:///d:/MyData/Desktop/test_pixiv/components.py) to render smooth animated vectors and overlay status messages while performing scans.
* **Advanced Filter Bar**: Added size ranges and date ranges dropdown lists underneath the text search input box.
* **SQLite-Level Advanced Filtering**: Integrated range-based SQL criteria (`size`, `mtime`, and `filename LIKE`) inside `StreamScanner` to optimize searches in the database.
* **Chrome User-Agent**: Configured the Danbooru API name updater to supply a standard User-Agent header to avoid request blockings (403/429).
* **Custom Application Icon**: Integrated custom window icons using the `icon.ico` resource for the main application and dialog windows.
* **Metadata modification time (mtime)**: Enabled indexing and saving of actual file modification times (`mtime`) in the database.
* **Random Sorting Option**: Added a "Random" sorting option to the Sort dropdown. This shuffles the folders, files, or detail views in place dynamically, allowing users to browse items randomly.
* **Aligned Filters and Search Bar**: Repositioned the "Sort" dropdown next to the "Size" and "Date Modified" filters on the second row, leaving the first row clean for the text search bar and search controls.
* **Dual-Mode Cover Randomization**: Added a "Quick Scan" mode to the "Randomize Covers" action to automatically scan for folders containing incomplete collages (folders with > 4 items showing fewer than 4 thumbnail quadrants) and rebuild only those, alongside the default "Full Rebuild" option.

### Changed
* **Dual-Source Name Update**: Upgraded the artist name resolution system to fetch official nicknames directly from the Pixiv public AJAX profile API first. This retrieves 100% accurate, multi-language artist names (including Japanese characters) anonymously without cookies, and falls back to Safebooru tags if blocked or not found.
* **Folder Cover Randomization**: Added a "Randomize Covers" button and a right-click context menu option to regenerate cover collages. Rebuilding all 10,000+ folder covers at once is resource-heavy due to Disk I/O, so the right-click option allows users to randomize any individual folder collage instantly (<0.1s).
* **Unified Search & Filter Card Panel**: Redesigned the search and filtering layout to group all controls inside a single, styled card container. Advanced file filters (Size and Date Modified) are dynamically hidden in Folders view, and the Sort dropdown updates its options contextually (File Count for Folders, File Size for Files) to avoid redundancy and clutter.
* **Incremental Quick Sync**: Redesigned the rescan system to allow an incremental 'Quick Sync' option. This compares file sizes and modification dates on the fly, skipping unmodified files, adding new ones, and pruning deleted folders and files from SQLite in a fraction of a second without resetting the tables.
* **Real-time SQLite Registration**: Updated the downloader workers to save newly downloaded images and artist profile mappings directly to the local SQLite database in real-time, making folder names and assets instantly visible without rescanning.
* **Bookmark Categorization**: Updated bookmarks downloader to save files under respective numeric Artist ID folders instead of a flat bookmarks directory, ensuring uniform structure compatible with the main gallery scanner.
* **Bookmark Page Ranges**: Integrated page range selection (e.g. from page X to Y) to limit bookmark fetching API queries and allow selective downloading.
* **Pre-emptive Skip Optimization**: Redesigned to perform list filtering *before* the download loop. Already downloaded illustration IDs are parsed from the directory and excluded upfront. This eliminates thousands of redundant 'Skipping' logs, prevents any unnecessary API requests, and scales the progress bar to only show actual pending downloads.
* **Max Download Limit Constraint**: Integrated a "Max Download Limit" parameter to the GUI and worker thread to allow safe batch-by-batch image retrieval.
* **Descriptive Filename Formatting**: Updated downloaded file naming logic in all modes to use the format: `{illust_id}_p{page} - {title}.ext`. Implemented character sanitization for Windows file system safety and limited the title string length to 100 characters to prevent path overflows.
* **Numeric Artist Folder Naming**: Changed folder naming logic in `'ILLUST'` and `'ARTIST'` download modes to create folders named exactly as the numeric Artist ID. This ensures complete compatibility with the app's metadata parser and SQLite cache mapping.
* **Parallel Multi-Core Background Preloading**: Upgraded the background preloader thread (`BackgroundThumbnailPreloader`) to run on multiple concurrent threads using `ThreadPoolExecutor` (utilizing up to 8 CPU threads). Combined this with a start-up O(1) cache set lookup to bypass SQLite query loops, speeding up background caching by 10x while keeping database writes thread-safe and sequential.
* **Global SQLite High-Performance Patch**: Injected an override to `sqlite3.connect` to automatically apply performance-tuning PRAGMAs (Memory-Mapped I/O, Memory Temp Store, larger Cache Size, and Normal Synchronous mode) to all database connections globally. This drastically accelerates disk reads/writes and memory throughput.
* **Offloaded Database Writes**: Relocated all database insert and commit queries from the main GUI thread (`main.py`) to background worker threads (`workers.py`).
* **Optimized Lazy Loading Grid Scan**: Replaced coordinate grid checks from 100+ slow `itemAt` checks to ~25 exact cell center lookups, freeing CPU cycles during scrolling.
* **Reverted Infinite Scroll**: Reverted the lazy list loading (infinite scroll) approach to restore QListWidget sorting integrity across all items, while keeping the UI loading overlay and updates-disabled optimizations for speed.
* **Optional OpenCV (`cv2`) Import**: Reconfigured the import of OpenCV to fail gracefully so that the app still boots if `cv2` is not installed on the system.

### Fixed
* **Missing Files on First Scan**: Fixed a severe logic bug in `StreamScanner` where newly discovered files were completely hidden from the UI on the first scan if they weren't already cached in the database. The system now guarantees that all newly detected media files are instantly pushed to the UI, regardless of database cache status, eliminating the bug where "All Animate" would sometimes show nothing or "All Files" would show partial results.
* **Zombie Process Memory Leak**: Resolved a severe resource leak where closing the application left Python hanging in the background (consuming ~3.6GB RAM per instance) due to `ThreadPoolExecutor` and PyQt non-daemon threads blocking the shutdown hook. Implemented a forced `os._exit(0)` cleanup phase on window close to guarantee complete memory release.
* **Stale Worker Thread CPU Lockup**: Patched an issue where switching views rapidly caused 8 concurrent `ImageLoaderWorker` threads to become locked up processing abandoned disk I/O requests. Injected a fast-abort check allowing workers to instantly terminate and return CPU resources if the main UI's `scan_gen_id` changes.
* **Blank Video Thumbnail Fix**: Fixed a bug where heavy video/GIF files (which are skipped by the foreground loader and handled in the background) would remain as completely blank transparent boxes indefinitely. They now correctly render a dark gray placeholder box with their media type indicator (`▶`, `ZIP`) until the background preloader finishes processing them.
* **All Animates Foreground Pool Clogging Fix**: Bypassed decoding of heavy animations/videos (OpenCV/ZIP extraction) on the foreground thread pool if they are uncached. Increased the foreground pool's maximum thread count to avoid UI blocking and queue exhaustion.
* **Resilient Image Loader DB Locks Fix**: Resolved a race condition where database locks from background caching caused `ImageLoaderWorker` threads to silently crash and fail to emit thumbnails, leaving items frozen with default icons.
* **Double-Trigger View Transition Fix**: Resolved a critical race condition where QRadioButton toggled signals fired twice during tab switches. Implemented state tracking via `self.current_view_mode` to ignore duplicate events and prevent thread scanner abortions.
* **Windows Taskbar Icon Fix**: Added `ctypes` AppUserModelID definition at application launch to force Windows to group the process under its custom window icon instead of defaulting to the generic Python shell logo.
* **Downloader Cancel Button Crash**: Patched a critical bug where `PixivDownloaderWorker` lacked a `stop()` method, which threw an `AttributeError` and forced the application to shut down when users cancelled active downloads.
* **Multi-Page Illustration Download Fix**: Resolved a critical issue where the app would only download the first page (`_p0`) of multi-page artworks. Integrated a query to Pixiv's `/pages` endpoint to properly retrieve all original page URLs for sequential downloading.
* **Bookmark API Endpoint Fix**: Corrected the bookmarks list retrieval URL by adding the missing `/illusts` subpath, resolving the HTTP 404 (Not Found) errors when fetching user bookmarks.
* **Deleted Object Crash Fix**: Implemented signal disconnection during dialog close event to avoid the application crashing with C++ runtime `RuntimeError` due to threads emitting to destroyed UI widgets.
* **Auto View Refresh**: Configured parent window to automatically refresh folder/file lists when downloading completes successfully.
* **Avatar Overlay Logic**: Resolved a logic error where profile avatars completely replaced folder cover grids instead of overlaying on the top-left corner.
* **Duplicate Method Declarations**: Deleted multiple duplicate method definitions (`load_visible_thumbnails`, `update_single_file_thumbnail`, and `download_image`) that were overwriting active definitions.
* **Redundant UI Timers**: Cleaned up orphan slots (`handle_file_loaded`, `process_ui_update`) and deactivated a 30ms timer that ran continuously without target callbacks.

---

## [Version 0.9.0-V.24] - Historical Baseline
* Initial implementation of the Pixiv Manager app.
* Support for local folders browsing, basic caching database, and thumbnail generation.
