"""articles（SPEC 8.1）：測試不打網路，用 tests/fixtures 的 article_*.html 和 news_cnyes*.json。"""

import json

import pytest
import requests

from brief import articles, collect, net
from brief.cli import main
from brief.net import Fetcher
from conftest import fake_session, fixture_bytes

UDN_URL = "https://money.udn.com/money/story/5710/9798786"
UDN_CAL_URL = "https://money.udn.com/money/story/5612/9800000"
YAHOO_STOCK_URL = "https://tw.stock.yahoo.com/news/example-3e36d70b.html"
YAHOO_NEWS_URL = "https://tw.news.yahoo.com/example-88fbbab5.html"


def page(name, source):
    return articles.parse_page(source, fixture_bytes(name).decode("utf-8"))


def run_articles(urls, table, cache_dir, capsys):
    """table：網址 → (狀態碼, 內容) 或例外。回傳 (結束碼, 輸出的 JSON, 打出去的網址)。"""

    def handler(req):
        return table.get(req.url, requests.ConnectionError(f"沒準備這個網址：{req.url}"))

    session, adapter = fake_session(handler)
    code = articles.run(urls, fetcher=Fetcher(session, sleep=lambda s: None), cache_dir=cache_dir)
    out, _ = capsys.readouterr()
    return code, json.loads(out), [r.url for r in adapter.requests]


def html_page(paragraphs_html, extra=""):
    return (f'<html><head><meta property="og:title" content="標題"/></head><body>{extra}'
            f'<section class="article-body__editor">{paragraphs_html}</section></body></html>').encode()


# ---------- 網址判斷 ----------


@pytest.mark.parametrize("url, source", [
    (UDN_URL, articles.UDN),
    ("https://money.udn.com/money/story/5710/9798786?from=edn", articles.UDN),
    (YAHOO_STOCK_URL, articles.YAHOO),
    (YAHOO_NEWS_URL, articles.YAHOO),
    ("https://news.cnyes.com/news/id/6624323", articles.CNYES),
    ("https://news.cnyes.com/news/id/6624323?exp=a", articles.CNYES),
    ("https://example.com/news/1", None),
    ("https://money.udn.com/", None),
    ("https://tw.stock.yahoo.com/quote/0050", None),
    ("https://news.cnyes.com/news/cat/tw_stock", None),
    ("ftp://money.udn.com/money/story/1/2", None),
])
def test_detect(url, source):
    assert articles.detect(url) == source


# ---------- 解析 fixture ----------


def test_udn_paragraphs_and_title():
    p = page("article_udn.html", articles.UDN)
    # og:title 去掉後面的「 | 分類 | 經濟日報」；空的 <p> 不算一段；<a> 裡的文字留著
    assert p.title == "範例科技9月營收創新高"
    assert len(p.paragraphs) == 2
    assert p.paragraphs[0].startswith("範例科技（0000）昨（6）日公布9月合併營收100.00億元")
    assert p.paragraphs[1].startswith("範例科技結算，第3季合併營收300.00億元")


def test_udn_heading_counts_as_paragraph():
    # 行事曆的小標是 <p><h2>台灣</h2></p>，也算一段
    p = page("article_udn_calendar.html", articles.UDN)
    assert p.title == "10月7日重要財經行事曆"
    assert p.paragraphs == ["台灣", "主計總處發布9月消費者物價指數"]


@pytest.mark.parametrize("name", ["article_yahoo_news.html", "article_yahoo_stock.html"])
def test_yahoo_header_slogan_skipped(name):
    p = page(name, articles.YAHOO)
    assert len(p.paragraphs) == 2
    assert not any("偏好來源" in x or "首選來源" in x for x in p.paragraphs)


def test_yahoo_titles_and_caption_kept():
    news = page("article_yahoo_news.html", articles.YAHOO)
    stock = page("article_yahoo_stock.html", articles.YAHOO)
    assert news.title == "範例縣政府舉辦徵才活動"
    assert stock.title == "範例電力公司：用電議題未進入政策研議"
    assert news.paragraphs[0].startswith("▲")  # 圖說留著
    assert stock.paragraphs[0].startswith("（測試通訊社台北2026年10月6日電）")


def test_yahoo_slogan_text_filtered_outside_header():
    # 標語不在 <header> 裡、寫法也不同時，用文字比對濾掉；正文照留
    html = ("<html><body><article><p>將 Yahoo 加入 Google 優先推薦來源，搶先看更多報導</p>"
            "<p>正文第一段。</p></article></body></html>")
    assert articles.parse_page(articles.YAHOO, html).paragraphs == ["正文第一段。"]


def test_yahoo_tail_filtered():
    html = ("<html><body><article><p>正文第一段。</p><p>正文第二段。</p><p>更多鉅亨網報導</p>"
            "<p>【看原文連結】</p><p>延伸閱讀：</p><p>某某新聞連結</p></article></body></html>")
    assert articles.parse_page(articles.YAHOO, html).paragraphs == ["正文第一段。", "正文第二段。"]


def test_br_splits_paragraph():
    p = articles.parse_page(articles.UDN, html_page("<p>第一行<br>第二行<br/>第三行</p>").decode())
    assert p.paragraphs == ["第一行", "第二行", "第三行"]


def test_whitespace_and_entities_normalized():
    p = articles.parse_page(articles.UDN, html_page("<p>\n  A&amp;B  C \n</p>").decode())
    assert p.paragraphs == ["A&B C"]


def test_script_and_style_not_text():
    p = articles.parse_page(articles.UDN, html_page("<p>正文<script>var x=1;</script></p><style>p{}</style>").decode())
    assert p.paragraphs == ["正文"]


def test_container_missing_raises():
    with pytest.raises(articles.ArticleError, match="找不到內文容器"):
        articles.parse_page(articles.UDN, "<html><body><p>沒有容器</p></body></html>")


def test_text_outside_container_ignored():
    html = '<body><p>側欄</p><section class="article-body__editor"><p>正文</p></section><p>頁尾</p></body>'
    assert articles.parse_page(articles.UDN, html).paragraphs == ["正文"]


# ---------- 截斷 ----------


def test_truncate_under_limit():
    assert articles.truncate(["甲" * 1000, "乙" * 1000]) == (["甲" * 1000, "乙" * 1000], False)  # 剛好 2,000 不算截斷


def test_truncate_cuts_exactly_at_limit():
    out, truncated = articles.truncate(["甲" * 1500, "乙" * 1000, "丙" * 10])
    assert truncated is True
    assert out == ["甲" * 1500, "乙" * 500]
    assert sum(map(len, out)) == articles.MAX_CHARS


def test_truncate_drops_following_paragraphs_when_exactly_full():
    out, truncated = articles.truncate(["甲" * 2000, "乙"])
    assert (out, truncated) == (["甲" * 2000], True)


# ---------- 鉅亨網：內文、快取 ----------


def test_cnyes_content_unescaped_and_only_p():
    items = json.loads(fixture_bytes("news_cnyes_content.json"))["items"]["data"]
    assert articles.content_paragraphs(items[0]["content"]) == [
        "這是測試用的假新聞第一段，數字都是編的：中位數由1.00元上修至1.10元。", "市場預估EPS"]
    assert len(articles.content_paragraphs(items[1]["content"])) == 2


def test_cnyes_content_table_not_taken():
    content = "&lt;p&gt;正文&lt;/p&gt;&lt;table&gt;&lt;tr&gt;&lt;td&gt;&lt;p&gt;表格裡&lt;/p&gt;&lt;/td&gt;&lt;/tr&gt;&lt;/table&gt;"
    assert articles.content_paragraphs(content) == ["正文"]
    assert articles.content_paragraphs("<p>沒跳脫的 HTML 也行</p>") == ["沒跳脫的 HTML 也行"]
    assert articles.content_paragraphs("") == []


def test_save_cnyes_cache_format(tmp_path):
    saved, missing = articles.save_cnyes_cache(fixture_bytes("news_cnyes_content.json"), tmp_path / "cnyes")
    assert sorted(saved) == ["6624009", "6624323"] and missing == []
    lines = (tmp_path / "cnyes" / "6624009.txt").read_text(encoding="utf-8").split("\n")
    assert lines[0] == "範例論壇聚焦無人機與低軌衛星"  # 第一行標題
    assert lines[1] == ""  # 第二行空白
    assert lines[2:4] == ["面對無人機與低軌衛星的發展，這是測試用的假新聞第一段。", "主辦單位表示，這是測試用的假新聞第二段。"]


def test_save_cnyes_cache_without_content(tmp_path):
    # news_cnyes.json 已經刪掉 content：30 則都記成沒有內文，但目錄照樣建好
    saved, missing = articles.save_cnyes_cache(fixture_bytes("news_cnyes.json"), tmp_path / "cnyes")
    assert saved == [] and len(missing) == 30 and missing[0] == "6623158"
    assert list((tmp_path / "cnyes").iterdir()) == []


def test_save_cnyes_cache_unsafe_news_id_not_used_as_filename(tmp_path):
    body = json.dumps({"items": {"data": [{"newsId": "../evil", "title": "t", "content": "<p>x</p>"}]}}).encode()
    saved, missing = articles.save_cnyes_cache(body, tmp_path / "cnyes")
    assert saved == [] and missing == ["../evil"]
    assert not (tmp_path / "evil.txt").exists()


def test_collect_cache_is_readable_by_articles(tmp_path, capsys):
    cache = tmp_path / "cnyes"
    c = collect.Collector(Fetcher(fake_session(lambda r: (200, b""))[0], sleep=lambda s: None))
    collect._save_cnyes_cache(c, fixture_bytes("news_cnyes_content.json"), cache)
    assert c.warnings == []
    code, out, requested = run_articles(["https://news.cnyes.com/news/id/6624323"], {}, cache, capsys)
    assert code == articles.EXIT_OK
    assert requested == []  # 鉅亨網不抓網頁
    assert out[0]["source"] == "鉅亨網" and out[0]["title"] == "測試速報 - 範例公司EPS預估上修"
    assert out[0]["paragraphs"][1] == "市場預估EPS"
    assert out[0]["chars"] == sum(map(len, out[0]["paragraphs"]))


def test_cnyes_not_in_cache_fails(tmp_path, capsys):
    code, out, requested = run_articles(["https://news.cnyes.com/news/id/1"], {}, tmp_path / "none", capsys)
    assert code == articles.EXIT_PARTIAL and requested == []
    assert out[0]["ok"] is False and "快取" in out[0]["error"]
    assert out[0]["paragraphs"] == [] and out[0]["chars"] == 0 and out[0]["truncated"] is False


def test_cache_dir_default_is_ignored_by_git():
    assert articles.CNYES_CACHE.parts[-2:] == (".cache", "cnyes")


# ---------- run：輸出格式、結束碼 ----------


def test_run_output_fields_and_exit_0(tmp_path, capsys):
    table = {UDN_URL: (200, fixture_bytes("article_udn.html")),
             YAHOO_NEWS_URL: (200, fixture_bytes("article_yahoo_news.html"))}
    code, out, requested = run_articles([UDN_URL, YAHOO_NEWS_URL], table, tmp_path, capsys)
    assert code == 0
    assert requested == [UDN_URL, YAHOO_NEWS_URL]
    assert [set(x) for x in out] == [{"url", "source", "title", "ok", "error", "paragraphs", "chars", "truncated"}] * 2
    first = out[0]
    assert first["url"] == UDN_URL and first["source"] == "經濟日報" and first["ok"] is True
    assert first["error"] is None and first["truncated"] is False
    assert first["chars"] == sum(len(p) for p in first["paragraphs"])
    assert out[1]["source"] == "Yahoo股市"


def test_run_duplicate_urls_fetched_once(tmp_path, capsys):
    table = {UDN_URL: (200, fixture_bytes("article_udn.html"))}
    code, out, requested = run_articles([UDN_URL, UDN_URL, f" {UDN_URL} "], table, tmp_path, capsys)
    assert code == 0 and requested == [UDN_URL] and len(out) == 1


def test_run_truncates_long_article(tmp_path, capsys):
    body = html_page("".join(f"<p>{'字' * 900}</p>" for _ in range(4)))
    code, out, _ = run_articles([UDN_URL], {UDN_URL: (200, body)}, tmp_path, capsys)
    assert code == 0 and out[0]["truncated"] is True and out[0]["chars"] == 2000
    assert [len(p) for p in out[0]["paragraphs"]] == [900, 900, 200]


def test_run_failures_do_not_stop_others(tmp_path, capsys):
    bad = "https://money.udn.com/money/story/1/1"
    err = "https://money.udn.com/money/story/1/2"
    empty = "https://money.udn.com/money/story/1/3"
    nocontainer = "https://money.udn.com/money/story/1/4"
    table = {
        bad: (404, b"not found"),
        err: requests.ConnectTimeout("逾時"),
        empty: (200, html_page("")),
        nocontainer: (200, b"<html><body><p>x</p></body></html>"),
        UDN_URL: (200, fixture_bytes("article_udn.html")),
    }
    urls = [bad, err, empty, nocontainer, "https://example.com/a", UDN_URL]
    code, out, _ = run_articles(urls, table, tmp_path, capsys)
    assert code == articles.EXIT_PARTIAL
    assert [x["ok"] for x in out] == [False, False, False, False, False, True]
    assert out[0]["error"] == "HTTP 404"
    assert "ConnectTimeout" in out[1]["error"]
    assert out[2]["error"] == "內文是空的" and out[2]["title"] == "標題"
    assert out[3]["error"] == "找不到內文容器"
    assert out[4]["source"] is None and "不支援" in out[4]["error"]  # 不支援的網站不會送出請求


def test_run_unsupported_site_makes_no_request(tmp_path, capsys):
    code, out, requested = run_articles(["https://example.com/a"], {}, tmp_path, capsys)
    assert code == 2 and requested == [] and out[0]["ok"] is False


def test_run_retries_once_then_fails(tmp_path, capsys):
    code, out, requested = run_articles([UDN_URL], {UDN_URL: (503, b"")}, tmp_path, capsys)
    assert code == 2 and requested == [UDN_URL, UDN_URL]  # 失敗重試 1 次
    assert out[0]["error"] == "HTTP 503"


def test_run_error_message_is_redacted(tmp_path, capsys):
    net.register_secret("fake-secret-for-articles-tests")
    table = {UDN_URL: requests.ConnectionError("連不上 /botfake-secret-for-articles-tests/x")}
    code, out, _ = run_articles([UDN_URL], table, tmp_path, capsys)
    assert "fake-secret-for-articles-tests" not in out[0]["error"]
    assert "***" in out[0]["error"]


# ---------- 命令列 ----------


def test_cli_articles(tmp_path, capsys, monkeypatch):
    session, _ = fake_session(lambda req: (200, fixture_bytes("article_udn_calendar.html")))
    monkeypatch.setattr(articles, "Fetcher", lambda: Fetcher(session, sleep=lambda s: None))
    assert main(["articles", UDN_CAL_URL]) == 0
    out, err = capsys.readouterr()
    assert json.loads(out)[0]["paragraphs"] == ["台灣", "主計總處發布9月消費者物價指數"]  # 標準輸出只有 JSON


def test_cli_articles_without_url_exits_1(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["articles"])
    assert exc.value.code == 1
