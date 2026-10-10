"""KANU Bot - Facebook Messenger bot for manga / manhwa / novels.
Full version with robust local smart dictionary and Mangawy scraper.

Run on Render with:  gunicorn app:app
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import random
import re
import tempfile
import threading
import time
import unicodedata
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Optional, Protocol
from urllib.parse import urljoin, urlparse

import img2pdf
import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request
from PIL import Image


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
)
log = logging.getLogger("kanu")


# =========================================================
# CONFIG (all secrets come from environment variables only)
# =========================================================

BOT_NAME = "KANU Bot"

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")
FACEBOOK_APP_SECRET = os.getenv("FACEBOOK_APP_SECRET")
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v26.0")


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


MAX_MEMORY_MESSAGES = 12
MAX_MEMORY_USERS = 1000
RATE_LIMIT_SECONDS = _env_float("RATE_LIMIT_SECONDS", 2.0)
MAX_TRACKED_IDS = 5000

# Concurrency / resource limits
CHAT_WORKERS = _env_int("CHAT_WORKERS", 4)
MAX_PENDING_CHAT = _env_int("MAX_PENDING_CHAT", 50)
JOB_WORKERS = _env_int("JOB_WORKERS", 1)
MAX_QUEUED_JOBS = _env_int("MAX_QUEUED_JOBS", 5)
MAX_CHAPTERS_PER_REQUEST = _env_int("MAX_CHAPTERS_PER_REQUEST", 10)

# Chapter / PDF limits
MAX_PAGES_PER_CHAPTER = _env_int("MAX_PAGES_PER_CHAPTER", 300)
DOWNLOAD_WORKERS = _env_int("DOWNLOAD_WORKERS", 4)
IMAGE_ATTEMPTS = 3
IMAGE_CONNECT_TIMEOUT = 5
IMAGE_READ_TIMEOUT = 20
IMAGE_TOTAL_SECONDS = 60
MAX_IMAGE_BYTES = _env_int("MAX_IMAGE_MB", 15) * 1024 * 1024
MAX_MISSING_RATIO = 0.10
MAX_PDF_BYTES = _env_int("MAX_PDF_MB", 20) * 1024 * 1024

MESSENGER_TEXT_LIMIT = 1900

# Mangawy source
MANGAWY_BASE_URL = os.getenv("MANGAWY_BASE_URL", "https://mangawy.org")
MANGAWY_ENABLED = os.getenv("MANGAWY_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
MANGAWY_ALLOW_PARTIAL = os.getenv("MANGAWY_ALLOW_PARTIAL", "0").strip().lower() in {"1", "true", "yes", "on"}
SITE_CONNECT_TIMEOUT = 5
SITE_READ_TIMEOUT = 20
SITE_ATTEMPTS = 3

ERROR_MESSAGE = "صار خطأ بسيط 😅 حاول مرة أخرى بعد قليل."


def redact(value: object) -> str:
    text = str(value)
    for secret in (PAGE_ACCESS_TOKEN, FACEBOOK_APP_SECRET):
        if secret:
            text = text.replace(secret, "***")
    return text


def user_tag(user_id: str) -> str:
    return f"u…{user_id[-4:]}"


# =========================================================
# SMALL THREAD-SAFE HELPERS
# =========================================================

class SeenSet:
    def __init__(self, max_size: int) -> None:
        self._items: OrderedDict[str, None] = OrderedDict()
        self._max = max_size
        self._lock = threading.Lock()

    def add_if_new(self, key: str) -> bool:
        with self._lock:
            if key in self._items:
                return False
            self._items[key] = None
            while len(self._items) > self._max:
                self._items.popitem(last=False)
            return True


class RateLimiter:
    def __init__(self, interval: float, max_users: int) -> None:
        self._interval = interval
        self._max = max_users
        self._last: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, user_id: str) -> bool:
        now = time.monotonic()
        with self._lock:
            last = self._last.get(user_id)
            if last is not None and now - last < self._interval:
                return False
            self._last[user_id] = now
            self._last.move_to_end(user_id)
            while len(self._last) > self._max:
                self._last.popitem(last=False)
            return True


class ConversationMemory:
    def __init__(self, max_messages: int, max_users: int) -> None:
        self._data: OrderedDict[str, deque] = OrderedDict()
        self._max_messages = max_messages
        self._max_users = max_users
        self._lock = threading.Lock()

    def snapshot(self, user_id: str) -> list[dict[str, str]]:
        with self._lock:
            items = list(self._data.get(user_id, ()))
        while items and items[0]["role"] != "user":
            items.pop(0)
        return items

    def append_turn(self, user_id: str, user_text: str, assistant_text: str) -> None:
        with self._lock:
            history = self._data.setdefault(user_id, deque(maxlen=self._max_messages))
            history.append({"role": "user", "content": user_text})
            history.append({"role": "assistant", "content": assistant_text})
            self._data.move_to_end(user_id)
            while len(self._data) > self._max_users:
                self._data.popitem(last=False)


class JobTracker:
    def __init__(self, max_jobs: int) -> None:
        self._active: set[str] = set()
        self._max = max_jobs
        self._lock = threading.Lock()

    def try_acquire(self, user_id: str) -> Optional[str]:
        with self._lock:
            if user_id in self._active:
                return "busy"
            if len(self._active) >= self._max:
                return "full"
            self._active.add(user_id)
            return None

    def release(self, user_id: str) -> None:
        with self._lock:
            self._active.discard(user_id)


MEMORY = ConversationMemory(MAX_MEMORY_MESSAGES, MAX_MEMORY_USERS)
RATE_LIMITER = RateLimiter(RATE_LIMIT_SECONDS, MAX_TRACKED_IDS)
SEEN_MESSAGE_IDS = SeenSet(MAX_TRACKED_IDS)
JOBS = JobTracker(MAX_QUEUED_JOBS)

CHAT_EXECUTOR = ThreadPoolExecutor(max_workers=CHAT_WORKERS, thread_name_prefix="chat")
JOB_EXECUTOR = ThreadPoolExecutor(max_workers=JOB_WORKERS, thread_name_prefix="job")
_chat_slots = threading.BoundedSemaphore(MAX_PENDING_CHAT)

HTTP = requests.Session()


# =========================================================
# DUA DATABASE
# =========================================================

DUAS = [
    "أستغفر الله العظيم وأتوب إليه",
    "أستغفر الله الذي لا إله إلا هو الحي القيوم وأتوب إليه",
    "رب اغفر لي وتب علي إنك أنت التواب الرحيم",
    "لا إله إلا أنت سبحانك إني كنت من الظالمين",
    "رب اغفر وارحم وأنت خير الراحمين",
    "اللهم اغفر لي ولوالدي وللمؤمنين يوم يقوم الحساب",
]

DUA_PROBABILITY = 0.30


def add_dua(reply: str) -> str:
    if random.random() >= DUA_PROBABILITY:
        return reply
    return (
        f"{reply}\n\n"
        "╭───────────────╮\n"
        "   🤍 تذكير جميل\n"
        "╰───────────────╯\n"
        f"﴿ {random.choice(DUAS)} ﴾"
    )


# =========================================================
# MASSIVE LOCAL SMART DICTIONARY (القاموس الشامل لكل شيء)
# =========================================================

def match_local_dictionary(text: str) -> dict[str, Any]:
    t = text.lower().strip()

    # 1. الترحيب والتحيات الشاملة بكل اللهجات
    if any(w in t for w in ["مرحبا", "مرحباً", "السلام", "هلا", "اهلاً", "أهلاً", "صباح", "مساء", "salut", "salam", "hi", "hello", "الو", "سلام عليكم"]):
        replies = [
            "أهلاً بك! أنا KANU Bot، مساعدك الآلي لتحميل وقراءة المانجا والمانهوا 📚. ما هو العمل أو الفصل الذي تبحث عنه؟",
            "هلا والله منور البوت 🤍! اعطني اسم المانهوا ورقم الفصل وأنا أجيبه لك فوراً كملف PDF.",
            "أهلاً وسهلاً! تفضل، اكتب اسم المانجا ورقم الفصل وسأقوم بتجهيزه لك 🚀."
        ]
        return {"kind": "chat", "reply": random.choice(replies)}

    # 2. السؤال عن الحال
    if any(w in t for w in ["كيفك", "شخبارك", "حالك", "عامل ايه", "ايش اخبارك", "طمني عنك", "كيف حالك", "شو اخبارك"]):
        replies = [
            "أنا بخير والحمد لله، جاهز لخدمتك وجلب الفصول التي تحبها! 🌟",
            "كل شيء ممتاز وأنا في خدمتكم دائماً. هل تبحث عن فصل معين اليوم؟",
            "بأفضل حال! تفضل أرسل اسم العمل والفصل لنبدأ العمل 📚."
        ]
        return {"kind": "chat", "reply": random.choice(replies)}

    # 3. الشكر والتقدير
    if any(w in t for w in ["شكرا", "شكراً", "تسلم", "يعطيك العافية", "ممتاز", "رائع", "خرافي", "كفو"]):
        replies = [
            "العفو ولو! أنا هنا لخدمتك دائماً 🤍. هل تحتاج فصلًا آخر؟",
            "تسلم لي! أسعد جداً بخدمتك، أرسل أي فصل تحتاجه في أي وقت 🚀."
        ]
        return {"kind": "chat", "reply": random.choice(replies)}

    # 4. المساعدة والاستفسار
    if any(w in t for w in ["مساعدة", "help", "كيف أستخدم", "طريقة الاستخدام", "وش تسوي", "شو تعمل", "ايش فائدتك", "كيف"]):
        reply = (
            "طريقة استخدام البوت بسيطة جداً:\n"
            "• اكتب اسم المانهوا ورقم الفصل مباشرة (مثل: Nano Machine الفصل 3 أو نانو 3).\n"
            "• سأقوم بتحميله وتحويله إلى ملف PDF وإرساله لك هنا مباشرة! 📄"
        ]
        return {"kind": "chat", "reply": reply}

    # 5. استخراج أرقام الفصول وطلبات المانجا بذكاء مطلق
    match_num = re.search(r"(\d+(?:\.\d+)?)", t)
    has_keyword = any(k in t for k in [
        "فصل", "شابتر", "ch", "chapter", "الفصل", "اريد", "أريد", "ارسل", "أرسل", "هات", "بدي", "عايز", "مانهوا", "مانجا", "nano", "نانو", "ميشل", "ماشين"
    ])

    if match_num and (has_keyword or len(t.split()) <= 5):
        ch_number = float(match_num.group(1))
        if ch_number.is_integer():
            ch_number = int(ch_number)

        title = "Nano Machine"
        if "نانو" in t or "nano" in t or "ميشل" in t or "ماشين" in t:
            title = "Nano Machine"
        else:
            clean = re.sub(r"(فصل|شابتر|ch|chapter|\d+|اريد|أريد|ارسل|أرسل|من|في|لـ|مانهوا|مانجا)", "", t).strip()
            if len(clean) > 2:
                title = clean.title()

        return {
            "kind": "chapter_request",
            "title": title,
            "chapter_start": ch_number,
            "chapter_end": ch_number,
            "reply": ""
        }

    # افتراضي للدردشة العامة لكل الكلمات الأخرى
    return {
        "kind": "chat",
        "reply": f"أهلاً بك! لقد تلقيت رسالتك ({text}). إذا كنت تبحث عن فصل مانجا أو مانهوا، أرسل اسم العمل ورقم الفصل وسأقوم بتجهيزه لك فوراً! 📚"
    }


@dataclass
class Intent:
    kind: str
    reply: str = ""
    title: Optional[str] = None
    start: Optional[int] = None
    end: Optional[int] = None


def interpret_message(user_id: str, text: str) -> Intent:
    res = match_local_dictionary(text)
    intent = Intent(
        kind=res["kind"],
        reply=res.get("reply", ""),
        title=res.get("title"),
        start=res.get("chapter_start"),
        end=res.get("chapter_end")
    )
    
    note = f"(طلب فصل: {intent.title} الفصل {intent.start})" if intent.kind == "chapter_request" else intent.reply
    MEMORY.append_turn(user_id, text, note)
    return intent


def format_range(start: Optional[int], end: Optional[int]) -> str:
    if start is None or end is None or start == end:
        return f"الفصل {start}"
    return f"الفصول من {start} إلى {end}"


# =========================================================
# MESSENGER SEND API
# =========================================================

GRAPH_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}/me/messages"


def _graph_post(label: str, **kwargs: Any) -> bool:
    if not PAGE_ACCESS_TOKEN:
        log.error("PAGE_ACCESS_TOKEN is not set; cannot send %s", label)
        return False

    for attempt in (1, 2):
        try:
            response = HTTP.post(
                GRAPH_URL,
                headers={"Authorization": f"Bearer {PAGE_ACCESS_TOKEN}"},
                **kwargs,
            )
        except requests.RequestException as exc:
            log.warning("Messenger %s network error (attempt %d): %s",
                        label, attempt, redact(type(exc).__name__))
            continue

        if response.status_code == 200:
            return True

        log.error("Messenger %s failed: HTTP %d %s",
                  label, response.status_code, redact(response.text[:300]))
        if response.status_code < 500:
            return False
    return False


def split_text(text: str, limit: int = MESSENGER_TEXT_LIMIT) -> list[str]:
    chunks: list[str] = []
    remaining = text.strip()
    while len(remaining) > limit:
        cut = max(remaining.rfind("\n", 0, limit), remaining.rfind(" ", 0, limit))
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def send_text(recipient_id: str, text: str) -> bool:
    ok = True
    for chunk in split_text(text):
        sent = _graph_post(
            "text",
            json={
                "messaging_type": "RESPONSE",
                "recipient": {"id": recipient_id},
                "message": {"text": chunk},
            },
            timeout=(10, 30),
        )
        ok = ok and sent
    return ok


def send_file(recipient_id: str, path: Path) -> bool:
    with path.open("rb") as handle:
        return _graph_post(
            f"file {path.name}",
            data={
                "messaging_type": "RESPONSE",
                "recipient": json.dumps({"id": recipient_id}),
                "message": json.dumps(
                    {"attachment": {"type": "file", "payload": {"is_reusable": False}}}
                ),
            },
            files={"filedata": (path.name, handle, "application/pdf")},
            timeout=(10, 180),
        )


# =========================================================
# MANGAWY CHAPTER SOURCE
# =========================================================

class ChapterError(Exception):
    pass

class ChapterNotFound(ChapterError):
    pass

class SourceError(ChapterError):
    pass

class DownloadError(ChapterError):
    pass

class PdfBuildError(ChapterError):
    pass

class DeliveryError(ChapterError):
    pass

class SeriesNotFound(ChapterNotFound):
    pass

class AmbiguousSeries(ChapterError):
    def __init__(self, candidates: list[str]) -> None:
        super().__init__("ambiguous title: " + " | ".join(candidates))
        self.candidates = candidates

class ChapterMissing(ChapterNotFound):
    def __init__(self, message: str, latest: Optional[float] = None) -> None:
        super().__init__(message)
        self.latest = latest

class NoImagesFound(ChapterError):
    pass

class SourceUnreachable(SourceError):
    pass


@dataclass(frozen=True)
class ChapterPages:
    urls: list[str]
    headers: dict[str, str] = field(default_factory=dict)
    title: Optional[str] = None


@dataclass
class SeriesEntry:
    url: str
    names: list[str]

    @property
    def title(self) -> str:
        return self.names[0] if self.names else self.url.rsplit("/", 1)[-1]


_STATUS_OR_COUNT = re.compile(r"مستمر|مكتمل|معلق|متوقف|ملغي|ملغى|\d+\s*فصل")
_SERIES_PATH = re.compile(r"/series/[^/]+/?")
_CHAPTER_PATH = re.compile(r"/series/[^/]+/chapter/[^/]+/?")
_CHAPTER_LABEL = re.compile(r"(?:\bchapter|\bch\b\.?|ف\.?|الفصل)\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_CHAPTER_HREF_NUMBER = re.compile(r"/chapter/(\d+(?:\.\d+)?)-")
_EXPECTED_PAGES = re.compile(r"(\d+)\s*صفحة")
_IMAGE_EXT = r"\.(?:webp|jpe?g|png|gif|avif)"
_IMG_ATTRS = ("data-src", "data-lazy-src", "data-original", "data-url", "src")
_JUNK_IMAGE = re.compile(
    r"logo|favicon|avatar|icon|sprite|banner|advert|/ads?/|cover|thumb|emoji|placeholder|loading|profile",
    re.IGNORECASE,
)


def _normalise_title(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"[’'`\"]", "", text)
    text = re.sub(r"[\W_]+", " ", text)
    return " ".join(text.split())


def _title_score(query: str, candidate: str) -> float:
    if not query or not candidate:
        return 0.0
    if query == candidate or query.replace(" ", "") == candidate.replace(" ", ""):
        return 1.0
    extra = min(0.1, 0.002 * abs(len(candidate) - len(query)))
    if candidate.startswith(query):
        return 0.92 - extra
    if set(query.split()) <= set(candidate.split()):
        return 0.88 - extra
    return SequenceMatcher(None, query, candidate).ratio()


def _srcset_best(value: str) -> Optional[str]:
    candidates = [part.strip().split()[0] for part in value.split(",") if part.strip()]
    return candidates[-1] if candidates else None


def _natural_key(url: str) -> list[Any]:
    name = urlparse(url).path.rsplit("/", 1)[-1]
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name)]


def parse_series_listing(html: str, page_url: str) -> list[SeriesEntry]:
    soup = BeautifulSoup(html, "html.parser")
    found: OrderedDict[str, list[str]] = OrderedDict()
    for anchor in soup.find_all("a", href=True):
        url = urljoin(page_url, anchor["href"]).split("#")[0].split("?")[0].rstrip("/")
        if not _SERIES_PATH.fullmatch(urlparse(url).path):
            continue
        names = found.setdefault(url, [])
        pieces = list(anchor.stripped_strings)
        for img in anchor.find_all("img"):
            pieces.append(re.sub(r"^\s*غلاف\s+", "", img.get("alt", "")))
        for piece in pieces:
            for part in _STATUS_OR_COUNT.split(piece):
                part = part.strip()
                if part and _normalise_title(part) not in {_normalise_title(n) for n in names}:
                    names.append(part)

    entries = []
    for url, names in found.items():
        slug = urlparse(url).path.rsplit("/", 1)[-1]
        slug_name = re.sub(r"-[a-z0-9]{8}$", "", slug).replace("-", " ").strip()
        if len(slug_name) >= 4 and _normalise_title(slug_name) not in {_normalise_title(n) for n in names}:
            names.append(slug_name)
        entries.append(SeriesEntry(url=url, names=names))
    return entries


def parse_chapter_links(html: str, page_url: str) -> dict[float, str]:
    soup = BeautifulSoup(html, "html.parser")
    chapters: dict[float, str] = {}
    for anchor in soup.find_all("a", href=True):
        url = urljoin(page_url, anchor["href"]).split("#")[0].split("?")[0]
        path = urlparse(url).path
        if not _CHAPTER_PATH.fullmatch(path):
            continue
        label = _CHAPTER_LABEL.search(" ".join(anchor.stripped_strings))
        number_text = label.group(1) if label else None
        if number_text is None:
            from_href = _CHAPTER_HREF_NUMBER.search(path)
            number_text = from_href.group(1) if from_href else None
        if number_text is not None:
            chapters.setdefault(float(number_text), url)
    return chapters


def extract_chapter_images(html: str, page_url: str, allowed_host: str) -> tuple[list[str], Optional[int]]:
    def same_site(url: str) -> bool:
        host = urlparse(url).hostname or ""
        return host == allowed_host or host.endswith("." + allowed_host)

    soup = BeautifulSoup(html, "html.parser")
    expected: Optional[int] = None
    for meta in soup.find_all("meta"):
        if meta.get("name") == "description" or meta.get("property") == "og:description":
            match = _EXPECTED_PAGES.search(meta.get("content", ""))
            if match:
                expected = int(match.group(1))
                break

    og = soup.find("meta", attrs={"property": "og:image"})
    og_url = urljoin(page_url, og["content"]) if og and og.get("content") else ""
    prefix = og_url.rsplit("/", 1)[0] + "/" if og_url and same_site(og_url) else ""

    def tag_urls(tag: Any) -> list[str]:
        urls = [tag.get(attr) for attr in _IMG_ATTRS if tag.get(attr)]
        for attr in ("data-srcset", "srcset"):
            if tag.get(attr):
                urls.append(_srcset_best(tag[attr]))
        return [urljoin(page_url, u.strip()) for u in urls if u and not u.strip().startswith("data:")]

    ordered: OrderedDict[str, None] = OrderedDict()
    if prefix:
        for tag in soup.find_all("img"):
            for url in tag_urls(tag):
                if url.startswith(prefix):
                    ordered.setdefault(url, None)
        unescaped = html.replace("\\/", "/")
        for url in re.findall(re.escape(prefix) + r"[^\s\"'<>)\\]+?" + _IMAGE_EXT, unescaped, re.IGNORECASE):
            ordered.setdefault(url, None)
        if ordered:
            return sorted(ordered, key=_natural_key), expected

    for tag in soup.find_all("img"):
        if tag.find_parent(["header", "footer", "nav", "aside"]):
            continue
        hints = " ".join([str(tag.get("alt", "")), " ".join(tag.get("class", [])), str(tag.get("id", ""))])
        for url in tag_urls(tag):
            if same_site(url) and not _JUNK_IMAGE.search(url + " " + hints):
                ordered.setdefault(url, None)
    return list(ordered), expected


class MangawyChapterSource:
    CATALOG_TTL = 30 * 60
    SERIES_TTL = 10 * 60
    FORCED_REFRESH_MIN_AGE = 120
    MAX_LISTING_PAGES = 15
    MAX_SERIES_CACHE = 100

    def __init__(self, base_url: str = MANGAWY_BASE_URL) -> None:
        self._base = base_url.rstrip("/")
        self._host = urlparse(self._base).hostname or ""
        self._headers = {
            "User-Agent": "Mozilla/5.0 (compatible; KANUBot/1.0)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.5",
            "Accept-Language": "ar,en;q=0.8",
        }
        self._lock = threading.Lock()
        self._catalog: list[SeriesEntry] = []
        self._catalog_at = 0.0
        self._series_cache: OrderedDict[str, tuple[float, dict[float, str]]] = OrderedDict()

    def is_configured(self) -> bool:
        return bool(self._host)

    def _get(self, url: str, *, allow_404: bool = False) -> Optional[str]:
        if urlparse(url).hostname != self._host:
            raise SourceError("refusing to fetch a URL outside the source site")
        last_error = "unknown"
        for attempt in range(1, SITE_ATTEMPTS + 1):
            try:
                response = HTTP.get(url, headers=self._headers, timeout=(SITE_CONNECT_TIMEOUT, SITE_READ_TIMEOUT))
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = type(exc).__name__
            except requests.RequestException as exc:
                raise SourceUnreachable(type(exc).__name__) from exc
            else:
                if response.status_code == 200:
                    return response.content.decode("utf-8", errors="replace")
                if response.status_code == 404 and allow_404:
                    return None
                last_error = f"HTTP {response.status_code}"
            if attempt < SITE_ATTEMPTS:
                time.sleep(attempt)
        raise SourceUnreachable(f"{url}: {last_error}")

    def _crawl_catalog(self) -> list[SeriesEntry]:
        merged: OrderedDict[str, SeriesEntry] = OrderedDict()

        def add(entries: list[SeriesEntry]) -> int:
            new = 0
            for entry in entries:
                if entry.url not in merged:
                    merged[entry.url] = entry
                    new += 1
            return new

        first_url = f"{self._base}/browse"
        html = self._get(first_url)
        add(parse_series_listing(html or "", first_url))

        soup = BeautifulSoup(html or "", "html.parser")
        page_links = OrderedDict(
            (urljoin(first_url, a["href"]), None)
            for a in soup.find_all("a", href=True)
            if re.fullmatch(r"\??page=\d+", urlparse(urljoin(first_url, a["href"])).query)
            and urlparse(urljoin(first_url, a["href"])).path.rstrip("/") == "/browse"
        )
        if page_links:
            for url in list(page_links)[: self.MAX_LISTING_PAGES]:
                add(parse_series_listing(self._get(url) or "", url))
        else:
            for number in range(2, self.MAX_LISTING_PAGES + 1):
                url = f"{first_url}?page={number}"
                page = self._get(url, allow_404=True)
                if not page or add(parse_series_listing(page, url)) == 0:
                    break
        return list(merged.values())

    def _get_catalog(self, force: bool = False) -> list[SeriesEntry]:
        with self._lock:
            age = time.monotonic() - self._catalog_at
            fresh = self._catalog and age < self.CATALOG_TTL
            if fresh and not (force and age > self.FORCED_REFRESH_MIN_AGE):
                return self._catalog
            catalog = self._crawl_catalog()
            if not catalog:
                raise SourceError("series listing returned no series")
            self._catalog, self._catalog_at = catalog, time.monotonic()
            return catalog

    @staticmethod
    def _match_series(title: str, catalog: list[SeriesEntry]) -> SeriesEntry:
        query = _normalise_title(title)
        ranked = sorted(
            (
                (max((_title_score(query, _normalise_title(n)) for n in entry.names), default=0.0), entry)
                for entry in catalog
            ),
            key=lambda pair: pair[0],
            reverse=True,
        )
        ranked = [pair for pair in ranked if pair[0] >= 0.35]
        if not ranked:
            raise SeriesNotFound(title)
        return ranked[0][1]

    def _find_series(self, title: str) -> SeriesEntry:
        try:
            return self._match_series(title, self._get_catalog())
        except SeriesNotFound:
            return self._match_series(title, self._get_catalog(force=True))

    def _series_chapters(self, series: SeriesEntry) -> dict[float, str]:
        now = time.monotonic()
        with self._lock:
            cached = self._series_cache.get(series.url)
            if cached and now - cached[0] < self.SERIES_TTL:
                return cached[1]
        html = self._get(series.url)
        chapters = parse_chapter_links(html or "", series.url)
        if not chapters:
            raise SourceError(f"no chapter links found on {series.url}")
        with self._lock:
            self._series_cache[series.url] = (now, chapters)
            self._series_cache.move_to_end(series.url)
            while len(self._series_cache) > self.MAX_SERIES_CACHE:
                self._series_cache.popitem(last=False)
        return chapters

    def fetch_pages(self, title: str, chapter: int) -> ChapterPages:
        series = self._find_series(title)
        chapters = self._series_chapters(series)
        chapter_url = chapters.get(float(chapter))
        if chapter_url is None:
            raise ChapterMissing(f"{series.title} chapter {chapter}", latest=max(chapters))

        html = self._get(chapter_url, allow_404=True)
        if html is None:
            raise ChapterMissing(f"{series.title} chapter {chapter}", latest=max(chapters))

        urls, expected = extract_chapter_images(html, chapter_url, self._host)
        if not urls:
            raise NoImagesFound(f"no page images found on {chapter_url}")
        return ChapterPages(urls=urls, headers={"Referer": self._base + "/"}, title=series.title)


CHAPTER_SOURCE = MangawyChapterSource() if MANGAWY_ENABLED else None


# =========================================================
# DOWNLOAD IMAGES -> PDF
# =========================================================

def safe_filename(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "", text).strip(" .")
    return re.sub(r"\s+", " ", cleaned)[:80] or "chapter"


def _normalise_image(path: Path) -> Path:
    target = path.with_suffix(".jpg")
    with Image.open(path) as img:
        img.load()
        if img.format == "JPEG" and img.mode in ("RGB", "L"):
            if path != target:
                path.rename(target)
            return target
        flat = img.convert("RGB")
        flat.save(target, "JPEG", quality=95)
    if path != target:
        path.unlink(missing_ok=True)
    return target


def _download_image(url: str, dest: Path, headers: dict[str, str]) -> Optional[Path]:
    for attempt in range(1, IMAGE_ATTEMPTS + 1):
        try:
            with requests.get(url, headers=headers, stream=True, timeout=(IMAGE_CONNECT_TIMEOUT, IMAGE_READ_TIMEOUT)) as response:
                if response.status_code != 200:
                    raise ValueError(f"HTTP {response.status_code}")
                with dest.open("wb") as handle:
                    for chunk in response.iter_content(64 * 1024):
                        handle.write(chunk)
            return _normalise_image(dest)
        except Exception:
            time.sleep(0.5)
    dest.unlink(missing_ok=True)
    return None


def download_pages(pages: ChapterPages, workdir: Path) -> tuple[list[Path], int]:
    urls = [u for u in pages.urls if urlparse(u).scheme in ("http", "https")]
    if not urls:
        raise ChapterNotFound("no usable page URLs")
    headers = {"User-Agent": "Mozilla/5.0", **pages.headers}
    jobs = [(url, workdir / f"{index:04d}.img") for index, url in enumerate(urls, 1)]

    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS, thread_name_prefix="img") as pool:
        results = list(pool.map(lambda job: _download_image(job[0], job[1], headers), jobs))

    images = [p for p in results if p is not None]
    missing = len(results) - len(images)
    if not images:
        raise DownloadError("failed to download chapter images")
    return images, missing


def build_pdfs(images: list[Path], workdir: Path, base_name: str) -> list[Path]:
    output = workdir / f"{base_name}.pdf"
    with output.open("wb") as handle:
        img2pdf.convert([str(p) for p in images], outputstream=handle)
    return [output]


# =========================================================
# CHAPTER JOBS (background)
# =========================================================

def process_chapter(user_id: str, title: str, number: int) -> str:
    label = f"{title} — الفصل {number}"
    try:
        pages = CHAPTER_SOURCE.fetch_pages(title, number)
        series_title = pages.title or title
        label = f"{series_title} — الفصل {number}"
        with tempfile.TemporaryDirectory(prefix="kanu_") as tmp:
            workdir = Path(tmp)
            images, missing = download_pages(pages, workdir)
            base = f"{safe_filename(series_title)}_Chapter_{number}"
            pdfs = build_pdfs(images, workdir, base)
            for pdf in pdfs:
                if not send_file(user_id, pdf):
                    raise DeliveryError(pdf.name)

        send_text(user_id, add_dua(f"تفضل 📄 {label} ✅"))
        return "ok"
    except SeriesNotFound:
        send_text(user_id, f"ما لقيت مانهوا بهذا الاسم في Mangawy 🤔 تأكد من الاسم.")
    except ChapterMissing as exc:
        hint = f" آخر فصل متوفر هو {exc.latest:g}." if exc.latest is not None else ""
        send_text(user_id, f"الفصل {number} غير موجود حالياً.{hint}")
    except Exception:
        send_text(user_id, ERROR_MESSAGE)
    return "failed"


def run_chapter_job(user_id: str, title: str, chapters: list[int]) -> None:
    try:
        for number in chapters:
            process_chapter(user_id, title, number)
    finally:
        JOBS.release(user_id)


def start_chapter_request(user_id: str, intent: Intent) -> None:
    title = intent.title or "Nano Machine"
    start = intent.start or 1
    end = intent.end or start

    refusal = JOBS.try_acquire(user_id)
    if refusal == "busy":
        send_text(user_id, "لديك طلب قيد المعالجة حالياً ⏳ انتظر قليلاً.")
        return
    if refusal == "full":
        send_text(user_id, "النظام مشغول حالياً بالطلبات، حاول بعد لحظات 😅")
        return

    send_text(user_id, f"تمام، جاري جلب {title} — الفصل {start} 📚... سأرسله لك حالاً.")
    try:
        JOB_EXECUTOR.submit(run_chapter_job, user_id, title, list(range(start, end + 1)))
    except RuntimeError:
        JOBS.release(user_id)


# =========================================================
# MESSAGE HANDLING
# =========================================================

def handle_text_message(user_id: str, text: str) -> None:
    intent = interpret_message(user_id, text)
    if intent.kind == "chapter_request":
        start_chapter_request(user_id, intent)
    else:
        send_text(user_id, add_dua(intent.reply))


def submit_chat_task(user_id: str, text: str) -> None:
    if not _chat_slots.acquire(blocking=False):
        return
    def task():
        try:
            handle_text_message(user_id, text)
        finally:
            _chat_slots.release()
    CHAT_EXECUTOR.submit(task)


def dispatch_event(event: Any) -> None:
    if not isinstance(event, dict) or "message" not in event:
        return
    message = event["message"]
    if message.get("is_echo"):
        return
    sender_id = event.get("sender", {}).get("id")
    text = message.get("text")
    if not sender_id or not isinstance(text, str) or not text.strip():
        return
    if not SEEN_MESSAGE_IDS.add_if_new(message.get("mid", "")):
        return
    if not RATE_LIMITER.allow(sender_id):
        return
    submit_chat_task(sender_id, text.strip()[:2000])


# =========================================================
# FLASK APP
# =========================================================

app = Flask(__name__)


@app.get("/")
def index():
    return f"{BOT_NAME} is running", 200


@app.get("/health")
def health():
    return jsonify(status="ok"), 200


@app.get("/webhook")
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token", "")
    challenge = request.args.get("hub.challenge", "")
    if mode == "subscribe" and VERIFY_TOKEN and hmac.compare_digest(token.encode(), VERIFY_TOKEN.encode()):
        return challenge, 200
    return "Verification failed", 403


@app.post("/webhook")
def receive_webhook():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or payload.get("object") != "page":
        return "OK", 200
    for entry in payload.get("entry", []):
        for event in entry.get("messaging", []):
            try:
                dispatch_event(event)
            except Exception:
                pass
    return "EVENT_RECEIVED", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))