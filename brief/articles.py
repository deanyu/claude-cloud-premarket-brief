"""v2 articles：抓新聞內文，輸出 JSON 到標準輸出。照 docs/SPEC.md 第 8.1 節。

內文有版權：只在執行當下輸出到標準輸出，不寫進 repo。鉅亨網不抓網頁，讀 collect 存的快取（.cache/cnyes/）。
"""

from __future__ import annotations

import html as html_lib
import json
import re
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from .net import Fetcher, log, redact

UDN, YAHOO, CNYES = "經濟日報", "Yahoo股市", "鉅亨網"  # 和 news.SOURCE_NAMES 的來源名稱一致
MAX_CHARS = 2000  # 每篇最多的字數（paragraphs 的字數合計）
CNYES_CACHE = Path(__file__).resolve().parent.parent / ".cache" / "cnyes"

EXIT_OK, EXIT_PARTIAL = 0, 2


class ArticleError(Exception):
    """這一篇抓不到或解析不到內文。訊息會原樣寫進 JSON 的 error。"""


@dataclass
class Page:
    title: str
    paragraphs: list[str] = field(default_factory=list)


# ---------- 網址判斷 ----------

_CNYES_ID_RE = re.compile(r"^/news/id/(\d+)/?$")


def detect(url: str) -> str | None:
    """網址屬於哪個支援的來源；不支援回傳 None。"""
    parts = urlsplit(url.strip())
    host, path = (parts.hostname or "").lower(), parts.path
    if parts.scheme not in ("http", "https"):
        return None
    if host == "money.udn.com" and path.startswith("/money/story/"):
        return UDN
    if host == "tw.stock.yahoo.com" and path.startswith("/news/"):
        return YAHOO
    if host == "tw.news.yahoo.com" and len(path) > 1:
        return YAHOO
    if host == "news.cnyes.com" and _CNYES_ID_RE.match(path):
        return CNYES
    return None


def cnyes_id(url: str) -> str:
    m = _CNYES_ID_RE.match(urlsplit(url.strip()).path)
    if not m:
        raise ArticleError("看不出鉅亨網的 newsId")
    return m.group(1)


# ---------- HTML → 段落 ----------

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def _udn_container(tag: str, attrs: dict[str, str]) -> bool:
    return tag == "section" and "article-body__editor" in attrs.get("class", "").split()


def _yahoo_container(tag: str, attrs: dict[str, str]) -> bool:
    return tag == "article"


class _Extractor(HTMLParser):
    """取容器裡的段落。block_tags 裡的標籤各算一段、`<br>` 也分段；skip_tags 的整塊跳過。

    不套用 HTML 的巢狀規則：經濟日報的 `<p><h2>台灣</h2></p>` 會得到「台灣」一段。
    container 是 None 就取整份內容（鉅亨網的 content 只是片段）。
    """

    def __init__(self, container, block_tags: set[str], skip_tags: set[str]) -> None:
        super().__init__(convert_charrefs=True)
        self.container = container
        self.block_tags = block_tags
        self.skip_tags = skip_tags
        self.found = container is None
        self.done = False
        self.container_tag: str | None = None
        self.container_depth = 0
        self.skip_tag: str | None = None
        self.skip_depth = 0
        self.block_depth = 0
        self.buffer: list[str] = []
        self.paragraphs: list[str] = []

    def _flush(self) -> None:
        text = " ".join("".join(self.buffer).split())
        self.buffer = []
        if text:
            self.paragraphs.append(text)

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        if not self.found:
            if self.container(tag, {k: (v or "") for k, v in attrs}):
                self.found, self.container_tag, self.container_depth = True, tag, 1
            return
        if self.container_tag is not None and tag == self.container_tag:
            self.container_depth += 1
        if self.skip_tag is not None:
            if tag == self.skip_tag:
                self.skip_depth += 1
            return
        if tag in self.skip_tags:
            self.skip_tag, self.skip_depth = tag, 1
            self._flush()
            return
        if tag == "br":
            self._flush()
        elif tag in self.block_tags:
            self._flush()
            self.block_depth += 1

    def handle_startendtag(self, tag, attrs):
        # `<br/>` 這類自己結束的標籤：不會有對應的 end tag
        if tag == "br" and self.found and not self.done and self.skip_tag is None:
            self._flush()

    def handle_endtag(self, tag):
        if self.done or not self.found or tag in _VOID:
            return
        if self.container_tag is not None and tag == self.container_tag:
            self.container_depth -= 1
            if self.container_depth == 0:
                self._flush()
                self.done = True
                return
        if self.skip_tag is not None:
            if tag == self.skip_tag:
                self.skip_depth -= 1
                if self.skip_depth == 0:
                    self.skip_tag = None
            return
        if tag in self.block_tags:
            self._flush()
            self.block_depth = max(0, self.block_depth - 1)

    def handle_data(self, data):
        if self.found and not self.done and self.skip_tag is None and self.block_depth > 0:
            self.buffer.append(data)

    def close(self):
        super().close()
        self._flush()


def _extract(text: str, container, block_tags: set[str], skip_tags: set[str]) -> _Extractor | None:
    p = _Extractor(container, block_tags, skip_tags)
    p.feed(text)
    p.close()
    return p if p.found else None


# ---------- 標題 ----------

def _meta_title(text: str) -> str:
    """og:title，沒有就用 <title>。"""
    for m in re.finditer(r"<meta\b[^>]*>", text, re.I):
        tag = m.group(0)
        if re.search(r"""property\s*=\s*["']og:title["']""", tag, re.I):
            c = re.search(r"""content\s*=\s*(?:"([^"]*)"|'([^']*)')""", tag, re.I)
            if c:
                return " ".join(html_lib.unescape(c.group(1) or c.group(2) or "").split())
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    return " ".join(html_lib.unescape(m.group(1)).split()) if m else ""


_UDN_SUFFIX_RE = re.compile(r"\s\|\s.*經濟日報\s*$")


def _udn_title(title: str) -> str:
    """去掉後面的「 | 分類 | 經濟日報」（分類可能不只一層，從第一個「 | 」切）。"""
    return _UDN_SUFFIX_RE.sub("", title).strip()


# ---------- Yahoo 的標語、結尾 ----------

# 標語的寫法不止一種（見 PITFALLS）：<header> 整塊已經跳過，這裡再用文字比對一次。
_YAHOO_SLOGANS = ("偏好來源", "優先推薦來源", "設為首選來源")
_YAHOO_MORE_RE = re.compile(r"^更多.*報導")


def _clean_yahoo(paragraphs: list[str]) -> list[str]:
    out: list[str] = []
    for p in paragraphs:
        if p.startswith("延伸閱讀"):
            break  # 延伸閱讀是結尾的連結清單，後面都不要
        if len(p) < 80 and any(s in p for s in _YAHOO_SLOGANS):
            continue
        if len(p) < 60 and (_YAHOO_MORE_RE.match(p) or "看原文" in p):
            continue
        out.append(p)
    return out


# ---------- 公開的解析函式 ----------

def parse_page(source: str, html: str) -> Page:
    """經濟日報、Yahoo股市的網頁 → 標題和段落。找不到內文容器丟 ArticleError；容器裡沒有段落不丟（回空清單）。"""
    if source == UDN:
        ex = _extract(html, _udn_container, {"p"} | _HEADINGS, {"script", "style"})
        title = _udn_title(_meta_title(html))
    elif source == YAHOO:
        ex = _extract(html, _yahoo_container, {"p"}, {"header", "script", "style", "dialog"})
        title = _meta_title(html)
    else:
        raise ArticleError(f"不支援的來源：{source}")
    if ex is None:
        raise ArticleError("找不到內文容器")
    paragraphs = _clean_yahoo(ex.paragraphs) if source == YAHOO else ex.paragraphs
    return Page(title, paragraphs)


def content_paragraphs(content: str) -> list[str]:
    """鉅亨網列表 API 的 content → 段落。content 是跳脫過的 HTML（`&lt;p&gt;…`），先還原；只取 `<p>`，表格不取。"""
    if "<" not in content and "&lt;" in content:
        content = html_lib.unescape(content)
    ex = _extract(content, None, {"p"}, {"table", "script", "style"})
    return ex.paragraphs if ex else []


def truncate(paragraphs: list[str], limit: int = MAX_CHARS) -> tuple[list[str], bool]:
    """最多 limit 字。超過的那一段截在剛好 limit 字，後面的整段不要，回傳 truncated: true；剛好 limit 字不算截斷。"""
    out: list[str] = []
    used = 0
    for i, p in enumerate(paragraphs):
        if used + len(p) <= limit:
            out.append(p)
            used += len(p)
            continue
        if limit - used > 0:
            out.append(p[: limit - used])
        return out, True
    return out, False


# ---------- 鉅亨網快取 ----------

_NEWS_ID_RE = re.compile(r"^\d+$")


def save_cnyes_cache(body: bytes, cache_dir: Path) -> tuple[list[str], list[str]]:
    """列表 API 的回應 → 每則存成 <cache_dir>/<newsId>.txt（第一行標題、第二行空白、之後一段一行）。

    回傳 (存了的 newsId, 沒有內文所以沒存的 newsId)。目錄建不了、檔案寫不了會丟例外，由呼叫端記 warning。
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    missing: list[str] = []
    for item in json.loads(body)["items"]["data"]:
        news_id = str(item["newsId"])
        paragraphs = content_paragraphs(item.get("content") or "")
        if not _NEWS_ID_RE.match(news_id) or not paragraphs:  # newsId 要是純數字才拿來當檔名
            missing.append(news_id)
            continue
        title = " ".join(str(item.get("title") or "").split())
        (cache_dir / f"{news_id}.txt").write_text(title + "\n\n" + "\n".join(paragraphs) + "\n", encoding="utf-8")
        saved.append(news_id)
    return saved, missing


def _read_cnyes_cache(url: str, cache_dir: Path) -> Page:
    path = cache_dir / f"{cnyes_id(url)}.txt"
    if not path.is_file():
        raise ArticleError("鉅亨網的內文不在快取裡（collect 沒存到這則）")
    lines = path.read_text(encoding="utf-8").split("\n")
    return Page(lines[0].strip(), [x.strip() for x in lines[2:] if x.strip()])


# ---------- 主程式 ----------

def _result(url: str, source: str | None, title: str = "", error: str | None = None,
            paragraphs: list[str] | None = None, truncated: bool = False) -> dict:
    paragraphs = paragraphs or []
    return {
        "url": url, "source": source, "title": title,
        "ok": error is None, "error": error,
        "paragraphs": paragraphs, "chars": sum(len(p) for p in paragraphs), "truncated": truncated,
    }


def fetch_article(url: str, fetcher: Fetcher, cache_dir: Path = CNYES_CACHE) -> dict:
    source = detect(url)
    if source is None:
        return _result(url, None, error="不支援的網站（支援經濟日報、Yahoo股市、鉅亨網的新聞網址）")
    title = ""
    try:
        if source == CNYES:
            page = _read_cnyes_cache(url, cache_dir)
        else:
            r = fetcher.get(url)
            if not r.ok:
                raise ArticleError(r.error or "抓不到網頁")
            page = parse_page(source, r.response.content.decode("utf-8", errors="replace"))
        title = page.title
        if not page.paragraphs:
            raise ArticleError("內文是空的")
        paragraphs, truncated = truncate(page.paragraphs)
        return _result(url, source, title, paragraphs=paragraphs, truncated=truncated)
    except Exception as e:  # 一篇失敗不中止其他篇；訊息先遮蔽
        msg = str(e) if isinstance(e, ArticleError) else f"{type(e).__name__}: {e}"
        return _result(url, source, title, error=redact(msg))


def run(urls: list[str], *, fetcher: Fetcher | None = None, cache_dir: Path = CNYES_CACHE) -> int:
    fetcher = fetcher or Fetcher()
    results = []
    for url in dict.fromkeys(u.strip() for u in urls):  # 同一個網址只抓一次，順序照第一次出現
        r = fetch_article(url, fetcher, cache_dir)
        if not r["ok"]:
            log(f"articles：{url} 失敗：{r['error']}")
        results.append(r)
    json.dump(results, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return EXIT_OK if all(r["ok"] for r in results) else EXIT_PARTIAL
