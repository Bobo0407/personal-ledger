# ruff: noqa: F811  （共用 test_web_txn_new.py 的 fixture）
"""最小 PWA：manifest 與圖示可公開取得、head 有掛上；刻意不做 service worker／離線。"""

import json
import re
import struct
from pathlib import Path

from app.main import STATIC_DIR
from tests.integration.test_web_txn_new import (  # noqa: F401
    anon,
    client,
    db,
    log_dir,
    session_factory,
    user_id,
)


def _png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def test_manifest_is_public_and_valid(anon):
    r = anon.get("/static/manifest.webmanifest")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/manifest+json")
    m = json.loads(r.text)
    assert m["name"] and m["short_name"]
    assert m["start_url"] == "/" and m["scope"] == "/"
    assert m["display"] == "standalone"
    assert m["theme_color"] and m["background_color"]


def test_manifest_icons_exist_with_declared_sizes(anon):
    m = json.loads(anon.get("/static/manifest.webmanifest").text)
    sizes = {i["sizes"] for i in m["icons"]}
    assert {"192x192", "512x512"} <= sizes
    for icon in m["icons"]:
        assert icon["type"] == "image/png"
        r = anon.get(icon["src"])
        assert r.status_code == 200, icon["src"]
        w, h = (int(x) for x in icon["sizes"].split("x"))
        assert _png_size(r.content) == (w, h)


def _assert_pwa_head(html: str) -> None:
    assert re.search(r'<link rel="manifest" href="/static/manifest\.webmanifest">', html)
    assert 'name="theme-color"' in html
    assert 'rel="apple-touch-icon"' in html


def test_login_page_head_links_manifest_and_icons(anon):
    r = anon.get("/login")
    assert r.status_code == 200
    _assert_pwa_head(r.text)


def test_logged_in_page_head_links_manifest_and_icons(client):
    r = client.get("/")
    assert r.status_code == 200
    _assert_pwa_head(r.text)
    assert _png_size((STATIC_DIR / "icons/apple-touch-icon.png").read_bytes()) == (180, 180)


def test_no_service_worker_or_offline_machinery():
    """範圍決定：選單「安裝應用程式」不需要 service worker，所以不做（避免快取帳務頁）。"""
    assert not list(Path(STATIC_DIR).rglob("*sw*.js"))
    base = (Path(STATIC_DIR).parent / "templates/base.html").read_text(encoding="utf-8")
    assert "serviceWorker" not in base
