# Manhwa PDF Merger

Merge multiple manhwa / manga chapters into a single continuous PDF in seconds.  
Paste a chapter URL, pick how many chapters, download one clean PDF. Runs 100% on your own computer — no accounts, no cloud, no ads.

![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Platform](https://img.shields.io/badge/platform-Mac%20%7C%20Linux%20%7C%20Windows-lightgrey)

---

## Features

- **One-click merging** — paste a chapter URL, set the count, done
- **Auto chapter navigation** — finds the next chapters automatically
- **Live progress** — percentage bar with per-chapter updates
- **Cancel anytime** — stop mid-download without restarting
- **Inline retry** — recovers from errors without a page reload
- **Editable filename** — rename before saving
- **Native Save As dialog** — pick exactly where the PDF lands
- **PDF size preview** — see how big the file is before downloading
- **Dark UI** — clean browser interface at `http://localhost:5055`

---

## Compatible Sites

### Full support (dedicated API integration)

| Site | URL |
|------|-----|
| MangaFire | https://mangafire.to |
| MangaDex | https://mangadex.org |
| Comick.io | https://comick.io |
| Webtoon | https://webtoons.com |

### Generic support

Works on any site where the chapter URL contains a number like `/chapter-1`, `/ch-1`, or `/ep-1`. The tool increments this number to navigate chapters, and uses [gallery-dl](https://github.com/mikf/gallery-dl) to download images.

| Site | Example URL pattern |
|------|-------------------|
| Asura Scans | `/series/title/chapter/1` |
| Flame Comics | `/manga/title/chapter-1` |
| Reaper Scans | `/series/title/chapter-1` |
| MangaReader | `/read/title/en/chapter-1` |
| MangaHere | `/manga/title/c001/` |
| Toonily | `/webtoon/title/chapter-1/` |
| Dynasty Scans | `/chapters/title-1` |
| + hundreds more | Any gallery-dl supported site |

### HTML fallback

For sites not supported by gallery-dl, the tool scrapes images directly from the page HTML. Works on many smaller aggregator and reader sites automatically.

### Not supported

| Site | Reason |
|------|--------|
| Bato.to | Chapter IDs are non-sequential and require login |
| MangaPlus | Proprietary image encoding |
| Lezhin / Tappytoon | Paywall / DRM |

---

## Installation

### Option 1 — One-liner (Mac/Linux, easiest)

```bash
curl -fsSL https://raw.githubusercontent.com/IT08-byte/manhwa-pdf-merger/main/install.sh | bash
```

This checks for Python, downloads the project, installs dependencies, and drops an alias so you can just type `manhwa-merger` anytime.

---

### Option 2 — Download ZIP (no git required)

1. Click **Code → Download ZIP** at the top of this page
2. Unzip it anywhere on your computer
3. **Mac/Linux:** double-click `start.sh`, or run it in Terminal:
   ```bash
   cd manhwa-pdf-merger
   ./start.sh
   ```
4. **Windows:** double-click `start.bat`
5. Open **http://localhost:5055** in your browser

---

### Option 3 — Git clone

```bash
git clone https://github.com/IT08-byte/manhwa-pdf-merger.git
cd manhwa-pdf-merger

# Mac / Linux
./start.sh

# Windows
start.bat
```

---

### Requirements

- **Python 3.9 or higher** — check with `python3 --version`
  - Mac: install from [python.org](https://python.org) or `brew install python`
  - Windows: install from [python.org](https://python.org) (check "Add to PATH" during install)
  - Linux: `sudo apt install python3 python3-pip`
- All other dependencies install automatically when you run the start script

---

## Usage

1. Go to your reading site and open **chapter 1** (or whichever chapter you want to start from)
2. Copy the URL from your browser's address bar
3. Open **http://localhost:5055**
4. Paste the URL, set the chapter count, click **Merge chapters**
5. Wait for the progress bar — then edit the filename and click **Save PDF**

### Example URLs

```
https://mangafire.to/read/nano-machine.j1234/en/chapter-1
https://mangadex.org/chapter/abc-uuid-here
https://comick.io/comic/nano-machine/xyz123-chapter-1-en
https://www.webtoons.com/en/action/title/ep-1/viewer?title_no=123&episode_no=1
https://mangareader.to/read/title-222/en/chapter-1
https://asuracomic.net/series/some-title-12345/chapter/1
```

---

## How It Works

The app runs a [Flask](https://flask.palletsprojects.com/) web server locally. When you submit a URL:

1. It detects which site you're on and picks the best download method
2. Downloads images using [gallery-dl](https://github.com/mikf/gallery-dl) or a direct scraper
3. Converts pages to JPEG and stitches them into a PDF using [ReportLab](https://www.reportlab.com/)
4. Serves the PDF for download — nothing is stored permanently on your machine

Everything happens locally. The only outbound connections are to the manga sites themselves.

---

## Project Structure

```
manhwa-pdf-merger/
├── app.py              # Flask backend — all site logic and PDF generation
├── templates/
│   └── index.html      # Single-page frontend
├── requirements.txt    # Python dependencies
├── start.sh            # Mac/Linux launcher
├── start.bat           # Windows launcher
└── install.sh          # One-liner installer (Mac/Linux)
```

---

## Contributing

Pull requests are welcome. The most useful contributions:

- **New site support** — add a handler in `app.py` following the pattern of `comick_download_images()`, then add it to the routing block in `merge_job()`
- **Better chapter navigation for Bato.to** — needs someone to reverse their GraphQL API
- **Bug reports** — open an issue with the site URL and the error you got

---

## Disclaimer

This tool is for **personal, offline use only**. Only use it for content you already have legal access to. Do not use it to distribute or redistribute copyrighted material. The authors are not responsible for misuse.

---

## License

MIT — see [LICENSE](LICENSE)
