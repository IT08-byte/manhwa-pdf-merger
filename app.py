import io
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, render_template, request, send_file
from PIL import Image
from werkzeug.utils import secure_filename

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

PARALLEL_CHAPTERS = 4   # concurrent gallery-dl subprocesses
PARALLEL_IMAGES = 8     # concurrent image HTTP fetches (MangaDex)
PARALLEL_CONVERT = 4    # concurrent JPEG conversion workers

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

jobs: Dict[str, dict] = {}
jobs_lock = threading.Lock()

JOB_TTL = 7200  # seconds — jobs older than 2h are auto-cleaned


ALLOWED_DOMAINS = {
    "mangadex.org", "mangafire.to", "comick.io", "webtoons.com",
    "toonily.com", "asurascans.com", "asuracomic.net", "manhwa18.cc",
    "manganato.com", "chapmanganato.to", "mangakakalot.com",
    "manhuafast.com", "manhuascan.io", "topmanhua.com",
    "reaperscans.com", "flamecomics.xyz", "luminousscans.com",
}

MAX_CONCURRENT_JOBS = 5
MAX_PDF_BYTES = 500 * 1024 * 1024  # 500 MB


def _validate_url(url: str) -> Optional[str]:
    """Return an error string if the URL domain is not allowed, or None if safe."""
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower().removeprefix("www.")
        if not hostname:
            return "Invalid URL: no hostname."
        # Check exact match or subdomain match against allowlist
        if not any(hostname == d or hostname.endswith("." + d) for d in ALLOWED_DOMAINS):
            return (
                f"Domain '{hostname}' is not supported. "
                "Supported: MangaDex, MangaFire, Comick, Webtoons, and other major manga sites."
            )
    except Exception:
        return "Invalid URL."
    return None


def _cleanup_old_jobs() -> None:
    """Daemon thread: remove jobs older than JOB_TTL every 10 minutes."""
    while True:
        time.sleep(600)
        cutoff = time.time() - JOB_TTL
        with jobs_lock:
            stale = [jid for jid, j in jobs.items() if j.get("created_at", 0) < cutoff]
            for jid in stale:
                jobs.pop(jid, None)


threading.Thread(target=_cleanup_old_jobs, daemon=True).start()


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


def _run_gallery_dl(
    gdl: str,
    chap_url: str,
    chap_dir: str,
    cancel_event: Optional[threading.Event] = None,
) -> Tuple[List[str], str]:
    """Run gallery-dl for one chapter; return (sorted_image_paths, stderr_text)."""
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
        time.sleep(0.25)

    imgs = sorted([
        os.path.join(root, f)
        for root, _, files in os.walk(chap_dir)
        for f in files
        if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
    ])
    stderr = (proc.stderr.read() if proc.stderr else b"").decode(errors="replace")
    return imgs, stderr


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
    results: List[Optional[List[str]]] = [None] * len(selected)

    def _fetch(idx: int) -> List[str]:
        chap_num, href = selected[idx]
        chap_url = f"https://mangafire.to{href}" if href.startswith("/") else href
        chap_dir = os.path.join(tmp_dir, f"ch_{chap_num}")
        imgs, stderr = _run_gallery_dl(gdl, chap_url, chap_dir, cancel_event)
        if not imgs:
            raise RuntimeError(f"gallery-dl failed for chapter {chap_num}:\n{stderr[-400:]}")
        return imgs

    with ThreadPoolExecutor(max_workers=PARALLEL_CHAPTERS) as ex:
        future_map = {ex.submit(_fetch, i): i for i in range(len(selected))}
        for fut in as_completed(future_map):
            if cancel_event and cancel_event.is_set():
                raise InterruptedError("Cancelled by user.")
            idx = future_map[fut]
            results[idx] = fut.result()  # raises on error
            if progress_cb:
                progress_cb()

    return [img for imgs in results if imgs for img in imgs]


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
    results: List[Optional[List[str]]] = [None] * len(chapter_urls)

    def _fetch(idx: int) -> List[str]:
        chap_dir = os.path.join(tmp_dir, f"ch_{idx+1:04d}")
        imgs, stderr = _run_gallery_dl(gdl, chapter_urls[idx], chap_dir, cancel_event)
        if not imgs:
            if idx == 0:
                raise RuntimeError(f"Failed to download from Comick.io:\n{stderr[-400:]}")
            return []  # later chapters may not exist yet
        return imgs

    with ThreadPoolExecutor(max_workers=PARALLEL_CHAPTERS) as ex:
        future_map = {ex.submit(_fetch, i): i for i in range(len(chapter_urls))}
        for fut in as_completed(future_map):
            if cancel_event and cancel_event.is_set():
                raise InterruptedError("Cancelled by user.")
            idx = future_map[fut]
            results[idx] = fut.result()
            if progress_cb and results[idx]:
                progress_cb()

    # Trim trailing missing chapters (gaps shouldn't happen but be safe)
    all_images: List[str] = []
    for imgs in results:
        if not imgs:
            break
        all_images.extend(imgs)
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

    # Probe chapter 0 first to detect HTML-scrape fallback sites
    chap_url_0 = _increment_chapter_url(url, 0)
    if chap_url_0 is None:
        raise ValueError(
            "Could not detect a chapter number in this URL. "
            "Make sure it contains a pattern like /chapter-1 or /ch-1."
        )
    chap_dir_0 = os.path.join(tmp_dir, "ch_0001")
    imgs_0, stderr_0 = _run_gallery_dl(gdl, chap_url_0, chap_dir_0, cancel_event)

    if not imgs_0 and "Unsupported URL" in stderr_0:
        # HTML scrape path — parallelize individual image downloads per chapter
        all_images: List[str] = []
        for i in range(count):
            if cancel_event and cancel_event.is_set():
                raise InterruptedError("Cancelled by user.")
            chap_url = _increment_chapter_url(url, i)
            if chap_url is None:
                break
            chap_dir = os.path.join(tmp_dir, f"ch_{i+1:04d}")
            os.makedirs(chap_dir, exist_ok=True)
            scraped_urls = html_scrape_chapter_images(chap_url)
            if not scraped_urls:
                if i == 0:
                    raise RuntimeError(
                        "This site isn't supported by gallery-dl and no images "
                        "could be found in the page HTML either.\n"
                        "Try a URL from MangaFire, MangaDex, Comick, MangaReader, "
                        "Asura Scans, or another well-known site."
                    )
                break

            # Download each chapter's images in parallel
            page_results: List[Optional[bytes]] = [None] * len(scraped_urls)

            def _fetch_img(j: int, img_url: str = scraped_urls[j]) -> bytes:  # type: ignore[misc]
                return download_image_as_jpeg(img_url, cancel_event)

            with ThreadPoolExecutor(max_workers=PARALLEL_IMAGES) as ex:
                img_futures = {ex.submit(_fetch_img, j): j for j in range(len(scraped_urls))}
                for fut in as_completed(img_futures):
                    page_results[img_futures[fut]] = fut.result()

            chapter_imgs: List[str] = []
            for j, data in enumerate(page_results):
                if data:
                    img_path = os.path.join(chap_dir, f"page_{j+1:04d}.jpg")
                    with open(img_path, "wb") as fh:
                        fh.write(data)
                    chapter_imgs.append(img_path)
            all_images.extend(chapter_imgs)
            if progress_cb:
                progress_cb()

        if not all_images:
            raise RuntimeError("No images downloaded. Check the URL and try again.")
        return all_images

    elif not imgs_0:
        raise RuntimeError(
            f"gallery-dl could not download from this URL.\n"
            f"Details: {stderr_0[-400:]}"
        )

    # gallery-dl works — download all chapters in parallel
    results: List[Optional[List[str]]] = [None] * count
    results[0] = imgs_0
    if progress_cb:
        progress_cb()

    def _fetch_chapter(i: int) -> List[str]:
        chap_url = _increment_chapter_url(url, i)
        if chap_url is None:
            return []
        chap_dir = os.path.join(tmp_dir, f"ch_{i+1:04d}")
        imgs, _ = _run_gallery_dl(gdl, chap_url, chap_dir, cancel_event)
        return imgs

    if count > 1:
        with ThreadPoolExecutor(max_workers=PARALLEL_CHAPTERS) as ex:
            future_map = {ex.submit(_fetch_chapter, i): i for i in range(1, count)}
            for fut in as_completed(future_map):
                if cancel_event and cancel_event.is_set():
                    raise InterruptedError("Cancelled by user.")
                idx = future_map[fut]
                results[idx] = fut.result()
                if progress_cb and results[idx]:
                    progress_cb()

    all_images = []
    for imgs in results:
        if not imgs:
            break
        all_images.extend(imgs)

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

def build_pdf(jpegs: List[bytes], chapter_page_map: Optional[List[Tuple[str, int]]] = None) -> bytes:
    """
    Build a PDF from a list of JPEG bytes.
    chapter_page_map: list of (chapter_label, 1-based page index) marking chapter starts.
    These become PDF outline bookmarks visible in any PDF viewer's sidebar.
    """
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader

    buf = io.BytesIO()
    c = canvas.Canvas(buf)

    # Map from 1-based page number → bookmark key
    bookmark_pages: dict = {}
    if chapter_page_map:
        for label, page_1based in chapter_page_map:
            key = f"ch_{page_1based}"
            bookmark_pages[page_1based] = (key, label)

    for i, jpeg_bytes in enumerate(jpegs):
        page_num = i + 1
        img = Image.open(io.BytesIO(jpeg_bytes))
        w, h = img.size
        c.setPageSize((w, h))
        c.drawImage(ImageReader(io.BytesIO(jpeg_bytes)), 0, 0, w, h)

        # Add a named destination at the top of this page
        if page_num in bookmark_pages:
            key, label = bookmark_pages[page_num]
            c.bookmarkPage(key, fit="XYZ", top=h)

        c.showPage()

    # Add outline entries after all pages are drawn
    if chapter_page_map:
        for label, page_1based in chapter_page_map:
            key = f"ch_{page_1based}"
            c.addOutlineEntry(label, key, level=0, closed=False)

    c.save()
    buf.seek(0)
    return buf.read()


# ── Background worker ─────────────────────────────────────────────────────────

def merge_job(job_id: str, chapter_url: str, chapter_count: int) -> None:
    with jobs_lock:
        cancel_event = jobs[job_id]["cancel_event"]
        jobs[job_id]["started_at"] = time.time()

    def update(status=None, progress=None, total=None, error=None, pdf=None):
        with jobs_lock:
            j = jobs[job_id]
            if status is not None:
                j["status"] = status
            if progress is not None:
                j["progress"] = progress
                # Recompute ETA whenever progress advances
                started = j.get("started_at")
                if started and progress > 0 and j["total"] > 0:
                    elapsed = time.time() - started
                    remaining = j["total"] - progress
                    j["eta_seconds"] = int((elapsed / progress) * remaining)
                else:
                    j["eta_seconds"] = None
            if total is not None:
                j["total"] = total
            if error is not None:
                j["error"] = error
            if pdf is not None:
                j["pdf_bytes"] = pdf
                j["pdf_size"] = len(pdf)
                j["eta_seconds"] = None

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

        # chapter_page_map: [(label, 1-based page index)] for PDF bookmarks
        chapter_page_map: List[Tuple[str, int]] = []

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
            jpegs_map: List[Optional[bytes]] = [None] * len(image_urls)
            done_count = [0]

            def _fetch_md(i: int) -> bytes:
                return download_image_as_jpeg(image_urls[i], cancel_event)

            with ThreadPoolExecutor(max_workers=PARALLEL_IMAGES) as ex:
                fut_map = {ex.submit(_fetch_md, i): i for i in range(len(image_urls))}
                for fut in as_completed(fut_map):
                    if cancel_event and cancel_event.is_set():
                        raise InterruptedError("Cancelled by user.")
                    jpegs_map[fut_map[fut]] = fut.result()
                    done_count[0] += 1
                    update(progress=done_count[0])

            jpegs: List[bytes] = [b for b in jpegs_map if b is not None]
            # MangaDex returns all images flat — we track chapter starts by
            # re-fetching metadata so we can label them
            try:
                chapter_id = parse_mangadex_url(chapter_url)
                if not chapter_id:
                    raise ValueError("No chapter ID")
                meta = md_chapter_meta(chapter_id)
                ch_ids = md_next_chapters(meta["manga_id"], meta["chapter"], meta["lang"], chapter_count)
                page_cursor = 1
                for idx, cid in enumerate(ch_ids):
                    ch_num = float(meta["chapter"]) + idx
                    label = f"Chapter {int(ch_num) if ch_num == int(ch_num) else ch_num}"
                    chapter_page_map.append((label, page_cursor))
                    page_cursor += len(md_chapter_images(cid))
            except Exception:
                pass  # bookmarks are best-effort

        elif "mangafire.to" in chapter_url:
            update(status="fetching_meta")
            update(status="downloading", progress=0, total=chapter_count)
            parsed_mf = parse_mangafire_url(chapter_url)
            start_num_mf = float(parsed_mf[3]) if parsed_mf else 1.0
            img_paths = mangafire_download_images(
                chapter_url, chapter_count, tmp_dir, on_chapter_done, cancel_event
            )
            update(status="building_pdf")
            jpeg_map: List[Optional[bytes]] = [None] * len(img_paths)
            with ThreadPoolExecutor(max_workers=PARALLEL_CONVERT) as ex:
                futs = {ex.submit(file_to_jpeg, img_paths[i]): i for i in range(len(img_paths))}
                for fut in as_completed(futs):
                    jpeg_map[futs[fut]] = fut.result()
            jpegs = [b for b in jpeg_map if b is not None]
            # Build chapter map from directory structure (each ch_X.X dir is one chapter)
            page_cursor = 1
            for i in range(chapter_count):
                ch_num = start_num_mf + i
                label = f"Chapter {int(ch_num) if ch_num == int(ch_num) else ch_num}"
                chap_dir = os.path.join(tmp_dir, f"ch_{start_num_mf + i}")
                # Count how many images were in this chapter dir
                ch_imgs = sorted([
                    f for root, _, files in os.walk(chap_dir)
                    for f in files if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
                ])
                if not ch_imgs:
                    break
                chapter_page_map.append((label, page_cursor))
                page_cursor += len(ch_imgs)

        elif "comick.io" in chapter_url:
            update(status="fetching_meta")
            update(status="downloading", progress=0, total=chapter_count)
            parsed_ck = parse_comick_url(chapter_url)
            start_num_ck = float(parsed_ck[2]) if parsed_ck else 1.0
            img_paths = comick_download_images(
                chapter_url, chapter_count, tmp_dir, on_chapter_done, cancel_event
            )
            update(status="building_pdf")
            jpeg_map_ck: List[Optional[bytes]] = [None] * len(img_paths)
            with ThreadPoolExecutor(max_workers=PARALLEL_CONVERT) as ex:
                futs = {ex.submit(file_to_jpeg, img_paths[i]): i for i in range(len(img_paths))}
                for fut in as_completed(futs):
                    jpeg_map_ck[futs[fut]] = fut.result()
            jpegs = [b for b in jpeg_map_ck if b is not None]
            page_cursor = 1
            for i in range(chapter_count):
                ch_num = start_num_ck + i
                label = f"Chapter {int(ch_num) if ch_num == int(ch_num) else ch_num}"
                chap_dir = os.path.join(tmp_dir, f"ch_{i+1:04d}")
                ch_imgs = sorted([
                    f for root, _, files in os.walk(chap_dir)
                    for f in files if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
                ])
                if not ch_imgs:
                    break
                chapter_page_map.append((label, page_cursor))
                page_cursor += len(ch_imgs)

        else:
            # Generic: gallery-dl with URL increment, HTML scrape fallback
            update(status="fetching_meta")
            update(status="downloading", progress=0, total=chapter_count)
            # Detect start chapter for labeling
            m_gen = re.search(r'(?:chapter|ch|c|ep)[-/.]?(\d+(?:\.\d+)?)', chapter_url, re.I)
            start_num_gen = float(m_gen.group(1)) if m_gen else 1.0
            img_paths = generic_download_images(
                chapter_url, chapter_count, tmp_dir, on_chapter_done, cancel_event
            )
            update(status="building_pdf")
            jpeg_map_gen: List[Optional[bytes]] = [None] * len(img_paths)
            with ThreadPoolExecutor(max_workers=PARALLEL_CONVERT) as ex:
                futs = {ex.submit(file_to_jpeg, img_paths[i]): i for i in range(len(img_paths))}
                for fut in as_completed(futs):
                    jpeg_map_gen[futs[fut]] = fut.result()
            jpegs = [b for b in jpeg_map_gen if b is not None]
            page_cursor = 1
            for i in range(chapter_count):
                ch_num = start_num_gen + i
                label = f"Chapter {int(ch_num) if ch_num == int(ch_num) else ch_num}"
                chap_dir = os.path.join(tmp_dir, f"ch_{i+1:04d}")
                ch_imgs = sorted([
                    f for root, _, files in os.walk(chap_dir)
                    for f in files if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
                ])
                if not ch_imgs:
                    break
                chapter_page_map.append((label, page_cursor))
                page_cursor += len(ch_imgs)

        update(status="building_pdf")
        pdf = build_pdf(jpegs, chapter_page_map if chapter_page_map else None)
        if len(pdf) > MAX_PDF_BYTES:
            update(status="error", error="Generated PDF exceeds 500 MB size limit.")
            return
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
    if not url:
        return jsonify({"error": "URL is required"}), 400

    # Accept either end_chapter (new UI) or chapters (legacy count)
    end_chapter = body.get("end_chapter")
    start_chapter = body.get("start_chapter")
    if end_chapter is not None and start_chapter is not None:
        try:
            end_ch = float(end_chapter)
            start_ch = float(start_chapter)
            count = max(1, int(end_ch - start_ch) + 1)
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid chapter numbers"}), 400
    else:
        count = int(body.get("chapters", 5))

    if count < 1 or count > 500:
        return jsonify({"error": "Chapter range must result in 1–500 chapters"}), 400

    url_err = _validate_url(url)
    if url_err:
        return jsonify({"error": url_err}), 400

    # Cap concurrent active jobs to prevent resource exhaustion
    with jobs_lock:
        active = sum(1 for j in jobs.values() if j["status"] not in ("done", "error", "cancelled"))
    if active >= MAX_CONCURRENT_JOBS:
        return jsonify({"error": "Too many active jobs. Please wait for one to finish."}), 429

    # Determine start chapter number for progress labeling
    detected_start = start_chapter if start_chapter is not None else None
    if detected_start is None:
        m = re.search(r'(?:chapter|ch|c|ep)[-/.]?(\d+(?:\.\d+)?)', url, re.I)
        detected_start = float(m.group(1)) if m else 1.0

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {
            "status": "queued", "progress": 0, "total": 0,
            "error": None, "pdf_bytes": None, "pdf_size": 0,
            "suggested_filename": "manhwa.pdf",
            "cancel_event": threading.Event(),
            "created_at": time.time(),
            "started_at": None,
            "start_chapter": float(detected_start),
            "chapter_count": count,
            "eta_seconds": None,
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
        "start_chapter": job.get("start_chapter", 1),
        "chapter_count": job.get("chapter_count", 0),
        "eta_seconds": job.get("eta_seconds"),
    })


@app.route("/api/download/<job_id>")
def download_pdf(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "PDF not ready"}), 404
    raw_name = request.args.get("name", job["suggested_filename"]) or "manhwa.pdf"
    filename = secure_filename(raw_name) or "manhwa.pdf"
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
