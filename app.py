import io
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Callable, Dict, List, Optional, Tuple

import requests
from flask import Flask, jsonify, render_template, request, send_file
from PIL import Image

app = Flask(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

jobs: Dict[str, dict] = {}
jobs_lock = threading.Lock()


# ── MangaDex ──────────────────────────────────────────────────────────────────

MANGADEX_API = "https://api.mangadex.org"


def parse_mangadex_url(url: str) -> Optional[str]:
    m = re.search(r"mangadex\.org/chapter/([0-9a-f-]{36})", url)
    return m.group(1) if m else None


def md_get(path: str, **params) -> dict:
    r = requests.get(f"{MANGADEX_API}{path}", params=params, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()


def md_chapter_meta(chapter_id: str) -> dict:
    data = md_get(f"/chapter/{chapter_id}")["data"]
    attrs = data["attributes"]
    manga_id = next(rel["id"] for rel in data["relationships"] if rel["type"] == "manga")
    return {
        "manga_id": manga_id,
        "chapter": attrs.get("chapter") or "0",
        "lang": attrs.get("translatedLanguage", "en"),
    }


def md_next_chapters(manga_id: str, start_chapter: str, lang: str, count: int) -> List[str]:
    results: List[str] = []
    offset = 0
    seen: set = set()
    while len(results) < count:
        data = md_get(
            f"/manga/{manga_id}/feed",
            **{
                "translatedLanguage[]": lang,
                "order[chapter]": "asc",
                "chapter[gte]": start_chapter,
                "limit": min(100, count * 2),
                "offset": offset,
                "contentRating[]": ["safe", "suggestive", "erotica", "pornographic"],
            },
        )
        items = data.get("data", [])
        if not items:
            break
        for item in items:
            key = f"{item['attributes'].get('chapter')}-{item['id']}"
            if key not in seen:
                seen.add(key)
                results.append(item["id"])
            if len(results) >= count:
                break
        offset += len(items)
        if offset >= data.get("total", 0):
            break
    return results[:count]


def md_chapter_images(chapter_id: str) -> List[str]:
    data = md_get(f"/at-home/server/{chapter_id}")
    base = data["baseUrl"]
    h = data["chapter"]["hash"]
    return [f"{base}/data/{h}/{p}" for p in data["chapter"]["data"]]


def mangadex_get_all_image_urls(url: str, count: int) -> List[str]:
    chapter_id = parse_mangadex_url(url)
    if not chapter_id:
        raise ValueError("Invalid MangaDex URL.")
    meta = md_chapter_meta(chapter_id)
    chapter_ids = md_next_chapters(meta["manga_id"], meta["chapter"], meta["lang"], count)
    if not chapter_ids:
        raise ValueError("Could not find chapters.")
    all_urls: List[str] = []
    for cid in chapter_ids:
        all_urls.extend(md_chapter_images(cid))
        time.sleep(0.3)
    return all_urls


# ── MangaFire (via gallery-dl) ────────────────────────────────────────────────

def parse_mangafire_url(url: str) -> Optional[Tuple[str, str, str, str]]:
    """Return (full_slug, manga_id, lang, chapter_num) or None."""
    m = re.search(
        r"mangafire\.to/read/([^/]+\.([a-z0-9]+))/([a-z-]+)/chapter-([0-9.]+)",
        url, re.I,
    )
    if m:
        return m.group(1), m.group(2), m.group(3), m.group(4)
    m = re.search(
        r"mangafire\.to/read/([^/]+\.([a-z0-9]+))/([a-z-]+)(?:/ch)?/?$",
        url, re.I,
    )
    if m:
        return m.group(1), m.group(2), m.group(3), "1"
    return None


def _gallery_dl_path() -> str:
    for candidate in ["gallery-dl", "gallery_dl"]:
        path = shutil.which(candidate)
        if path:
            return path
    for p in [
        os.path.expanduser("~/Library/Python/3.9/bin/gallery-dl"),
        os.path.expanduser("~/.local/bin/gallery-dl"),
        "/usr/local/bin/gallery-dl",
    ]:
        if os.path.isfile(p):
            return p
    raise FileNotFoundError(
        "gallery-dl not found. Run: pip3 install gallery-dl --break-system-packages"
    )


def _suggest_filename(url: str, start_chap: str, count: int) -> str:
    """Generate a human-readable PDF filename from the chapter URL."""
    slug = "manhwa"
    if "mangafire.to" in url:
        m = re.search(r"/read/([^/.]+)\.", url)
        slug = m.group(1) if m else "manhwa"
    elif "mangadex.org" in url:
        slug = "manga"
    elif "comick.io" in url:
        m = re.search(r"comick\.io/comic/([\w-]+)/", url)
        slug = m.group(1) if m else "manhwa"
    elif "webtoons.com" in url:
        m = re.search(r"webtoons\.com/[^/]+/[^/]+/([^/]+)/", url)
        slug = m.group(1) if m else "webtoon"
    else:
        # Try to extract something sensible from any URL
        m = re.search(r"/(?:webtoon|manga|comic|series|read)/([^/?#.]+)", url, re.I)
        if m:
            slug = m.group(1)

    title = slug.replace("-", " ").replace("_", " ").title()
    try:
        start_int = int(float(start_chap))
        end_chap = start_int + count - 1
        if count == 1:
            return f"{title} Ch{start_int}.pdf"
        return f"{title} Ch{start_int}-{end_chap}.pdf"
    except (ValueError, TypeError):
        return f"{title}.pdf"


def mangafire_download_images(
    url: str,
    count: int,
    tmp_dir: str,
    progress_cb: Optional[Callable] = None,
    cancel_event: Optional[threading.Event] = None,
) -> List[str]:
    parsed = parse_mangafire_url(url)
    if not parsed:
        raise ValueError("Invalid MangaFire URL.")
    full_slug, manga_id, lang, start_chap = parsed
    start_num = float(start_chap)

    r = requests.get(
        f"https://mangafire.to/ajax/manga/{manga_id}/chapter/{lang}",
        headers={**HEADERS, "X-Requested-With": "XMLHttpRequest", "Referer": "https://mangafire.to/"},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    html = data.get("result") or data.get("html") or ""
    if isinstance(html, dict):
        html = html.get("result") or html.get("html") or ""

    chapters: List[Tuple[float, str]] = []
    seen_nums: set = set()
    for m in re.finditer(r'href=["\']([^"\']*chapter-([0-9.]+))["\']', html):
        href = m.group(1).replace("\\/", "/")
        num = float(m.group(2))
        if num not in seen_nums:
            seen_nums.add(num)
            chapters.append((num, href))

    chapters.sort(key=lambda x: x[0])
    start_idx = next((i for i, (n, _) in enumerate(chapters) if n >= start_num), None)
    if start_idx is None:
        raise ValueError(f"Chapter {start_chap} not found in chapter list.")

    selected = chapters[start_idx: start_idx + count]
    if not selected:
        raise ValueError("No chapters found to download.")

    gdl = _gallery_dl_path()
    all_images: List[str] = []

    for chap_num, href in selected:
        if cancel_event and cancel_event.is_set():
            raise InterruptedError("Cancelled by user.")

        chap_url = f"https://mangafire.to{href}" if href.startswith("/") else href
        chap_dir = os.path.join(tmp_dir, f"ch_{chap_num}")
        os.makedirs(chap_dir, exist_ok=True)

        # Use Popen so we can kill it on cancel
        proc = subprocess.Popen(
            [gdl, "--dest", chap_dir,
             "--filename", "{chapter:>04}_{page:>04}.{extension}",
             chap_url],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        while proc.poll() is None:
            if cancel_event and cancel_event.is_set():
                proc.terminate()
                raise InterruptedError("Cancelled by user.")
            time.sleep(0.5)

        imgs: List[str] = []
        for root, _, files in os.walk(chap_dir):
            for f in files:
                if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                    imgs.append(os.path.join(root, f))
        imgs.sort()

        if not imgs and proc.returncode != 0:
            stderr = (proc.stderr.read() or b"").decode(errors="replace")
            raise RuntimeError(f"gallery-dl failed for chapter {chap_num}:\n{stderr[-500:]}")

        all_images.extend(imgs)
        if progress_cb:
            progress_cb()
        time.sleep(0.5)

    return all_images


# ── Comick.io ─────────────────────────────────────────────────────────────────

def parse_comick_url(url: str) -> Optional[Tuple[str, str, str, str]]:
    """Return (slug, hid, chapter_num, lang) from a comick.io chapter URL."""
    m = re.search(
        r"comick\.io/comic/([\w-]+)/([\w]+)-chapter-([0-9.]+)-([a-z]{2})",
        url, re.I,
    )
    return (m.group(1), m.group(2), m.group(3), m.group(4)) if m else None


def comick_get_chapter_urls(slug: str, lang: str, start_num: float, count: int) -> List[str]:
    """
    Use the Comick public API to build N chapter URLs starting at start_num.
    Each chapter has a unique 'hid' that changes — can't just increment numbers.
    API: https://api.comick.io/comic/{slug}/chapters?lang=en&limit=300
    """
    COMICK_API = "https://api.comick.io"
    headers = {
        **HEADERS,
        "Origin": "https://comick.io",
        "Referer": "https://comick.io/",
    }

    all_chapters: List[dict] = []
    page = 1
    while True:
        r = requests.get(
            f"{COMICK_API}/comic/{slug}/chapters",
            params={"lang": lang, "limit": "300", "chap-order": "1", "page": str(page)},
            headers=headers,
            timeout=15,
        )
        if r.status_code == 404:
            raise ValueError(f"Manga '{slug}' not found on Comick.io.")
        r.raise_for_status()
        data = r.json()
        chapters = data.get("chapters", [])
        if not chapters:
            break
        all_chapters.extend(chapters)
        total = data.get("total", 0)
        if len(all_chapters) >= total:
            break
        page += 1

    def chap_float(ch: dict) -> float:
        try:
            return float(ch.get("chap") or 0)
        except (ValueError, TypeError):
            return 0.0

    all_chapters.sort(key=chap_float)

    start_idx = next(
        (i for i, ch in enumerate(all_chapters) if chap_float(ch) >= start_num),
        None,
    )
    if start_idx is None:
        raise ValueError(f"Chapter {start_num} not found in Comick chapter list.")

    selected = all_chapters[start_idx: start_idx + count]
    return [
        f"https://comick.io/comic/{slug}/{ch['hid']}-chapter-{ch.get('chap', '0')}-{lang}"
        for ch in selected
    ]


def comick_download_images(
    url: str,
    count: int,
    tmp_dir: str,
    progress_cb: Optional[Callable] = None,
    cancel_event: Optional[threading.Event] = None,
) -> List[str]:
    """Download chapters from Comick.io using the API for chapter navigation."""
    parsed = parse_comick_url(url)
    if not parsed:
        raise ValueError("Invalid Comick.io chapter URL.")
    slug, _hid, chapter_num_str, lang = parsed
    start_num = float(chapter_num_str)

    chapter_urls = comick_get_chapter_urls(slug, lang, start_num, count)
    if not chapter_urls:
        raise ValueError("No chapters found on Comick.io.")

    gdl = _gallery_dl_path()
    all_images: List[str] = []

    for i, chap_url in enumerate(chapter_urls):
        if cancel_event and cancel_event.is_set():
            raise InterruptedError("Cancelled by user.")

        chap_dir = os.path.join(tmp_dir, f"ch_{i+1:04d}")
        os.makedirs(chap_dir, exist_ok=True)

        proc = subprocess.Popen(
            [gdl, "--dest", chap_dir,
             "--filename", "{chapter:>04}_{page:>04}.{extension}",
             chap_url],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        while proc.poll() is None:
            if cancel_event and cancel_event.is_set():
                proc.terminate()
                raise InterruptedError("Cancelled by user.")
            time.sleep(0.5)

        imgs: List[str] = []
        for root, _, files in os.walk(chap_dir):
            for f in files:
                if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                    imgs.append(os.path.join(root, f))
        imgs.sort()

        if not imgs:
            stderr_out = (proc.stderr.read() or b"").decode(errors="replace")
            if i == 0:
                raise RuntimeError(f"Failed to download from Comick.io:\n{stderr_out[-400:]}")
            break  # Later chapters may not exist yet

        all_images.extend(imgs)
        if progress_cb:
            progress_cb()
        time.sleep(0.5)

    return all_images


# ── Generic gallery-dl handler (any supported site) ───────────────────────────

def _increment_chapter_url(url: str, offset: int) -> Optional[str]:
    """
    Given a chapter URL, return the URL offset chapters later.

    Handles:
      - Query params: ?episode_no=N  (Webtoon), ?chapter=N
      - Path segments: /chapter-N, /chapter/N, /ch-N, /ch/N, /c-N, /ep-N, /ep/N
    Returns None if no chapter number can be detected.
    """
    if offset == 0:
        return url

    from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

    parsed = urlparse(url)

    # ── 1. Query-parameter chapter numbers (Webtoon etc.) ──────────────────
    if parsed.query:
        params = parse_qs(parsed.query, keep_blank_values=True)
        for key in ("episode_no", "chapter_no", "chapter", "ep_no"):
            if key in params:
                try:
                    new_val = int(params[key][0]) + offset
                    params[key] = [str(new_val)]
                    new_query = urlencode(params, doseq=True)
                    return urlunparse(parsed._replace(query=new_query))
                except (ValueError, TypeError):
                    pass

    # ── 2. Path-embedded chapter numbers ───────────────────────────────────
    path = parsed.path
    pattern = re.compile(
        r'(?<=/)'
        r'(?:chapter|ch|c|ep|episode)'   # keyword (includes ep for Webtoon)
        r'[-/.]?'
        r'(\d+(?:\.\d+)?)'
        r'(?=[^/]*(?:/|$))',
        re.IGNORECASE,
    )
    m = pattern.search(path)
    if not m:
        return None

    orig = float(m.group(1))
    new = orig + offset
    new_str = str(int(new)) if new == int(new) else str(new)
    new_path = path[:m.start(1)] + new_str + path[m.end(1):]
    return urlunparse(parsed._replace(path=new_path))


def html_scrape_chapter_images(url: str) -> List[str]:
    """
    Fallback scraper for sites whose images are embedded directly in HTML.
    Groups all <img> tags by their directory path and returns the largest
    cluster — which is almost always the chapter pages.
    Works for manhwa18.cc, toonily.com, and many similar WordPress sites.
    """
    from urllib.parse import urlparse
    from collections import Counter

    r = requests.get(url, headers={**HEADERS, "Referer": url}, timeout=20)
    r.raise_for_status()

    # Pull every img src that ends in an image extension
    raw_srcs = re.findall(
        r'<img[^>]+src=["\']([^"\']+\.(?:jpg|jpeg|png|webp|gif))[^"\']*["\']',
        r.text, re.I,
    )
    # Also catch lazy-loaded images stored in data-src / data-lazy-src
    raw_srcs += re.findall(
        r'<img[^>]+data-(?:src|lazy-src)=["\']([^"\']+\.(?:jpg|jpeg|png|webp))["\']',
        r.text, re.I,
    )

    if not raw_srcs:
        return []

    # Normalise relative URLs
    parsed_page = urlparse(url)
    base = f"{parsed_page.scheme}://{parsed_page.netloc}"
    full_srcs: List[str] = []
    for src in raw_srcs:
        if src.startswith("//"):
            full_srcs.append("https:" + src)
        elif src.startswith("/"):
            full_srcs.append(base + src)
        elif src.startswith("http"):
            full_srcs.append(src)

    # Group by directory (everything up to the last /)
    def img_dir(img_url: str) -> str:
        p = urlparse(img_url)
        return p.scheme + "://" + p.netloc + "/".join(p.path.split("/")[:-1]) + "/"

    dir_counts: Counter = Counter(img_dir(u) for u in full_srcs)
    if not dir_counts:
        return []

    # The most common directory is the chapter images
    best_dir, count = dir_counts.most_common(1)[0]
    if count < 3:
        return []  # Too few — probably not a chapter reader page

    # Filter to that directory, deduplicate, preserve order
    seen: set = set()
    chapter_imgs: List[str] = []
    for u in full_srcs:
        if img_dir(u) == best_dir and u not in seen:
            seen.add(u)
            chapter_imgs.append(u)

    return chapter_imgs


def generic_download_images(
    url: str,
    count: int,
    tmp_dir: str,
    progress_cb: Optional[Callable] = None,
    cancel_event: Optional[threading.Event] = None,
) -> List[str]:
    """
    Generic handler: tries gallery-dl first; if it returns 'Unsupported URL'
    falls back to direct HTML scraping. Increments the chapter number in the
    URL to navigate between chapters.
    """
    gdl = _gallery_dl_path()
    all_images: List[str] = []

    for i in range(count):
        if cancel_event and cancel_event.is_set():
            raise InterruptedError("Cancelled by user.")

        chap_url = _increment_chapter_url(url, i)
        if chap_url is None:
            raise ValueError(
                "Could not detect a chapter number in this URL. "
                "Make sure it contains a pattern like /chapter-1 or /ch-1."
            )

        chap_dir = os.path.join(tmp_dir, f"ch_{i+1:04d}")
        os.makedirs(chap_dir, exist_ok=True)

        # ── Try gallery-dl ──────────────────────────────────────────────────
        proc = subprocess.Popen(
            [gdl, "--dest", chap_dir,
             "--filename", "{chapter:>04}_{page:>04}.{extension}",
             chap_url],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        while proc.poll() is None:
            if cancel_event and cancel_event.is_set():
                proc.terminate()
                raise InterruptedError("Cancelled by user.")
            time.sleep(0.5)

        gdl_stderr = (proc.stderr.read() or b"").decode(errors="replace")

        imgs: List[str] = []
        for root, _, files in os.walk(chap_dir):
            for f in files:
                if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                    imgs.append(os.path.join(root, f))
        imgs.sort()

        # ── gallery-dl didn't work — try HTML scraping ──────────────────────
        if not imgs and "Unsupported URL" in gdl_stderr:
            scraped_urls = html_scrape_chapter_images(chap_url)
            if scraped_urls:
                for j, img_url in enumerate(scraped_urls):
                    img_data = download_image_as_jpeg(img_url)
                    img_path = os.path.join(chap_dir, f"page_{j+1:04d}.jpg")
                    with open(img_path, "wb") as fh:
                        fh.write(img_data)
                    imgs.append(img_path)
            elif i == 0:
                raise RuntimeError(
                    f"This site isn't supported by gallery-dl and no images could be "
                    f"found in the page HTML either.\n"
                    f"Try a URL from MangaFire, MangaDex, Comick, MangaReader, Asura Scans, "
                    f"or another well-known site."
                )

        # ── gallery-dl failed for a non-"unsupported" reason ───────────────
        elif not imgs and proc.returncode != 0 and i == 0:
            raise RuntimeError(
                f"gallery-dl could not download from this URL.\n"
                f"Details: {gdl_stderr[-400:]}"
            )

        # ── Chapter just doesn't exist (later in a series) ─────────────────
        elif not imgs:
            break

        all_images.extend(imgs)
        if progress_cb:
            progress_cb()
        time.sleep(0.5)

    if not all_images:
        raise RuntimeError("No images downloaded. Check the URL and try again.")

    return all_images


# ── Image helpers ─────────────────────────────────────────────────────────────

def download_image_as_jpeg(url: str, cancel_event: Optional[threading.Event] = None) -> bytes:
    if cancel_event and cancel_event.is_set():
        raise InterruptedError("Cancelled by user.")
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    img = Image.open(io.BytesIO(r.content))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def file_to_jpeg(path: str) -> bytes:
    img = Image.open(path)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


# ── PDF builder ───────────────────────────────────────────────────────────────

def build_pdf(jpegs: List[bytes]) -> bytes:
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for jpeg_bytes in jpegs:
        img = Image.open(io.BytesIO(jpeg_bytes))
        w, h = img.size
        c.setPageSize((w, h))
        c.drawImage(ImageReader(io.BytesIO(jpeg_bytes)), 0, 0, w, h)
        c.showPage()
    c.save()
    buf.seek(0)
    return buf.read()


# ── Background worker ─────────────────────────────────────────────────────────

def merge_job(job_id: str, chapter_url: str, chapter_count: int) -> None:
    with jobs_lock:
        cancel_event = jobs[job_id]["cancel_event"]

    def update(status=None, progress=None, total=None, error=None, pdf=None):
        with jobs_lock:
            j = jobs[job_id]
            if status is not None:
                j["status"] = status
            if progress is not None:
                j["progress"] = progress
            if total is not None:
                j["total"] = total
            if error is not None:
                j["error"] = error
            if pdf is not None:
                j["pdf_bytes"] = pdf
                j["pdf_size"] = len(pdf)

    tmp_dir = None
    try:
        update(status="detecting")

        # Work out suggested filename + starting chapter
        start_chap = "1"
        if "mangafire.to" in chapter_url:
            parsed_mf = parse_mangafire_url(chapter_url)
            if parsed_mf:
                start_chap = parsed_mf[3]
        elif "comick.io" in chapter_url:
            parsed_ck = parse_comick_url(chapter_url)
            if parsed_ck:
                start_chap = parsed_ck[2]
        else:
            # Try to pull chapter number from URL for filename
            m = re.search(r'(?:chapter|ch|c|ep)[-/.]?(\d+(?:\.\d+)?)', chapter_url, re.I)
            if m:
                start_chap = m.group(1)
            else:
                # Try query param
                m = re.search(r'episode_no=(\d+)', chapter_url)
                if m:
                    start_chap = m.group(1)

        with jobs_lock:
            jobs[job_id]["suggested_filename"] = _suggest_filename(
                chapter_url, start_chap, chapter_count
            )

        # ── Site routing ───────────────────────────────────────────────────
        tmp_dir = tempfile.mkdtemp(prefix="manhwa_")
        downloaded = [0]

        def on_chapter_done() -> None:
            downloaded[0] += 1
            update(progress=downloaded[0])

        if "bato.to" in chapter_url:
            raise ValueError(
                "Bato.to uses non-sequential chapter IDs that can't be "
                "navigated without a login. Try the same manga on MangaDex, "
                "Comick.io, or MangaFire instead."
            )

        elif "mangadex.org" in chapter_url:
            update(status="fetching_meta")
            image_urls = mangadex_get_all_image_urls(chapter_url, chapter_count)
            update(status="downloading", progress=0, total=len(image_urls))
            jpegs: List[bytes] = []
            for i, img_url in enumerate(image_urls):
                jpegs.append(download_image_as_jpeg(img_url, cancel_event))
                update(progress=i + 1)

        elif "mangafire.to" in chapter_url:
            update(status="fetching_meta")
            update(status="downloading", progress=0, total=chapter_count)
            img_paths = mangafire_download_images(
                chapter_url, chapter_count, tmp_dir, on_chapter_done, cancel_event
            )
            jpegs = [file_to_jpeg(p) for p in img_paths]

        elif "comick.io" in chapter_url:
            update(status="fetching_meta")
            update(status="downloading", progress=0, total=chapter_count)
            img_paths = comick_download_images(
                chapter_url, chapter_count, tmp_dir, on_chapter_done, cancel_event
            )
            jpegs = [file_to_jpeg(p) for p in img_paths]

        else:
            # Generic: gallery-dl with URL increment, HTML scrape fallback
            update(status="fetching_meta")
            update(status="downloading", progress=0, total=chapter_count)
            img_paths = generic_download_images(
                chapter_url, chapter_count, tmp_dir, on_chapter_done, cancel_event
            )
            jpegs = [file_to_jpeg(p) for p in img_paths]

        update(status="building_pdf")
        pdf = build_pdf(jpegs)
        update(status="done", pdf=pdf)

    except InterruptedError:
        update(status="cancelled", error="Download cancelled.")
    except Exception as exc:
        update(status="error", error=str(exc))
    finally:
        if tmp_dir and os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/merge", methods=["POST"])
def start_merge():
    body = request.get_json(force=True)
    url = (body.get("url") or "").strip()
    count = int(body.get("chapters", 5))
    if not url:
        return jsonify({"error": "URL is required"}), 400
    if count < 1 or count > 50:
        return jsonify({"error": "chapters must be between 1 and 50"}), 400

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {
            "status": "queued", "progress": 0, "total": 0,
            "error": None, "pdf_bytes": None, "pdf_size": 0,
            "suggested_filename": "manhwa.pdf",
            "cancel_event": threading.Event(),
        }

    threading.Thread(target=merge_job, args=(job_id, url, count), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/cancel/<job_id>", methods=["POST"])
def cancel_job(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    job["cancel_event"].set()
    return jsonify({"ok": True})


@app.route("/api/status/<job_id>")
def job_status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "progress": job["progress"],
        "total": job["total"],
        "error": job["error"],
        "pdf_size": job["pdf_size"],
        "suggested_filename": job["suggested_filename"],
    })


@app.route("/api/download/<job_id>")
def download_pdf(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "PDF not ready"}), 404
    filename = request.args.get("name", job["suggested_filename"]) or "manhwa.pdf"
    if not filename.endswith(".pdf"):
        filename += ".pdf"
    return send_file(
        io.BytesIO(job["pdf_bytes"]),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


if __name__ == "__main__":
    print("\n  Manhwa Merger running at http://localhost:5055\n")
    app.run(port=5055, debug=False)
