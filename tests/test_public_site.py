"""Public discovery is accessible anonymously without publishing private resources."""
import re
from xml.etree import ElementTree

import pytest

import app as app_mod
import public_site
import site_stats_store


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    monkeypatch.setitem(app_mod.app.config, "TESTING", True)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://histopilot.cn")
    return app_mod.app.test_client()


def test_anonymous_map_sitemap_and_llm_index_agree(client):
    xml = client.get("/sitemap.xml", headers={"Host": "untrusted.example"})
    assert xml.status_code == 200
    urls = [n.text for n in ElementTree.fromstring(xml.data).findall("{*}url/{*}loc")]
    assert urls == ["https://histopilot.cn" + p.path for p in public_site.public_pages()]
    assert "untrusted.example" not in xml.text
    for url in urls:
        path = url.removeprefix("https://histopilot.cn")
        assert client.get(path).status_code == 200, path
    html = client.get("/site-map").text
    for page in public_site.public_pages():
        assert 'href="%s"' % page.path in html
    llms = client.get("/llms.txt").text
    local_links = re.findall(r"\]\(https://histopilot.cn([^)]*)\)", llms)
    assert local_links
    for path in local_links:
        assert client.get(path).status_code == 200, path
    assert "untrusted.example" not in llms


@pytest.mark.parametrize("path", ["/robots.txt", "/sitemap.xml", "/llms.txt",
                                  "/llms-full.txt", "/guide.md", "/guide/conversion.md"])
def test_discovery_head_matches_get_and_is_read_only(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert client.head(path).status_code == 200
    assert not client.head(path).data
    # 匿名写方法可能先被认证/CSRF 守卫拒绝，或到路由层返回 405。
    assert client.post(path).status_code in (302, 400, 403, 405)
    assert "csrf_token" not in response.text
    assert "password" not in response.text
    assert not response.headers.getlist("Set-Cookie")
    with client.session_transaction() as session:
        session["csrf_token"] = "existing-user-token"
    cached = client.get(path)
    assert not cached.headers.getlist("Set-Cookie")
    assert "existing-user-token" not in cached.text


def test_crawler_access_does_not_bypass_authentication(client):
    ua = {"User-Agent": "ChatGPT-User/1.0"}
    robots = client.get("/robots.txt", headers=ua).text
    assert "Disallow: /\n" in robots
    assert "Allow: /$" in robots
    assert "Allow: /llms.txt$" in robots
    assert "Sitemap: https://histopilot.cn/sitemap.xml" in robots
    assert "Disallow: /api/" in robots
    assert client.get("/app", headers=ua).status_code == 302
    assert client.get("/api/slides", headers=ua).status_code == 401
    assert client.get("/guide/does-not-exist", headers=ua).status_code == 404
    assert "noindex" in client.get("/app", headers=ua).headers["X-Robots-Tag"]


def test_full_documentation_describes_conversion_and_permissions(client):
    body = client.get("/llms-full.txt").text
    for text in ("OME-TIFF", "MRXS", "VMS", "QuPath", "OpenSlide", "完整", "登录", "权限"):
        assert text in body
    assert "不自动" in body
    assert "切片转换" in client.get("/").text
    assert '</llms.txt>; rel="describedby"' in client.get("/").headers["Link"]


@pytest.mark.parametrize("name", ["GPTBot", "OAI-SearchBot", "ChatGPT-User", "ClaudeBot",
                                 "Claude-SearchBot", "Claude-User", "PerplexityBot", "Perplexity-User"])
def test_ai_crawlers_are_observed_as_bots(name):
    assert site_stats_store._classify_user_agent("Mozilla/5.0 %s/1.0" % name) == name
