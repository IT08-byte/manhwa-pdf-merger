# Changelog

All versions are available as GitHub Releases. See **[How to download a specific version](#how-to-download-a-specific-version)** below.

---

## [v2.0.0] — 2026-06-27

### What's new

#### PDF Viewer companion app (`viewer/`)
A new standalone reader app that opens your merged PDFs with a manga-optimised interface.

- Chapter sidebar built from the bookmarks embedded in the PDF
- Windowed rendering — only ~5 pages live in memory at once, even for 800-page PDFs
- Arrow / PageUp / PageDown keyboard navigation
- Fit / 125% / 150% zoom presets
- Click the page counter to jump to any page
- Remembers your scroll position per file (localStorage)

Run it with:
```bash
cd viewer
bash start.sh        # Mac / Linux
# then open http://localhost:5056
```

#### PDF chapter bookmarks
Merged PDFs now contain a clickable chapter outline. Any PDF viewer (Preview, Acrobat, the companion Viewer app) shows a sidebar where you can jump directly to a chapter.

#### End-chapter input
The UI now asks for a **start chapter** (auto-detected from the URL) and an **end chapter** instead of a raw chapter count. Paste a chapter URL and type `50` to download everything up to chapter 50.

#### Faster downloads (parallel)
Chapters now download in parallel instead of one at a time:

| What | Before | After |
|------|--------|-------|
| Gallery-dl chapters | Sequential | 4 concurrent subprocesses |
| MangaDex page images | Sequential | 8 concurrent HTTP fetches |
| JPEG conversion | Sequential | 4 concurrent workers |

For 50 chapters this is roughly **3–4× faster** than v1.

#### Progress display improvements
- Shows the actual chapter number being downloaded (e.g. "Downloading Chapter 23 of 50…")
- ETA estimate after the first chapter completes
- "Open in Viewer" button appears after a merge finishes

#### Security hardening
- Domain allowlist replaces the old IP-range SSRF guard (more robust, no DNS-rebinding bypass)
- Maximum 5 concurrent active jobs — prevents resource exhaustion
- Generated PDFs over 500 MB are rejected before storing in memory
- Download `?name=` parameter is sanitised via `werkzeug.secure_filename`
- Gallery-dl stderr is logged server-side only — never sent to the browser
- Viewer: Content-Length checked before reading upload body
- Viewer: PDF magic bytes (`%PDF-`) validated before accepting file
- Viewer: UUID format validated on all `pdf_id` path parameters

---

## [v1.0.0] — 2026-06-26

Initial release.

- Merge multiple chapters into a single PDF
- Full API support: MangaFire, MangaDex, Comick.io, Webtoon
- Generic gallery-dl support for hundreds of other sites
- HTML image scraper fallback for unsupported sites
- Live progress bar with cancel support
- Dark browser UI at `http://localhost:5055`
- Editable filename before download
- PDF size preview

---

## How to download a specific version

### Option A — GitHub Releases page (easiest)

1. Go to **[Releases](https://github.com/IT08-byte/manhwa-pdf-merger/releases)**
2. Find the version you want
3. Click **Source code (zip)** or **Source code (tar.gz)** under Assets
4. Unzip and run `start.sh` (Mac/Linux) or `start.bat` (Windows)

### Option B — Git tag (command line)

```bash
# Clone the repo (skip if you already have it)
git clone https://github.com/IT08-byte/manhwa-pdf-merger.git
cd manhwa-pdf-merger

# List all available versions
git tag

# Switch to a specific version
git checkout v1.0.0   # or v2.0.0, etc.

# Run it
./start.sh
```

To go back to the latest version:
```bash
git checkout main
```

### Option C — Direct ZIP download for a specific tag

Replace `TAG` with the version you want (e.g. `v1.0.0`):

```
https://github.com/IT08-byte/manhwa-pdf-merger/archive/refs/tags/TAG.zip
```

Example:
```
https://github.com/IT08-byte/manhwa-pdf-merger/archive/refs/tags/v1.0.0.zip
https://github.com/IT08-byte/manhwa-pdf-merger/archive/refs/tags/v2.0.0.zip
```
