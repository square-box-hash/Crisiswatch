"""Feed parsing — pure functions, no network."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sources import (  # noqa: E402
    Candidate,
    parse_ecdc,
    parse_promed,
    parse_reliefweb,
    parse_rss,
    parse_who_don,
    slug_hints,
)

WHO_HTML = """
<html><body>
<nav><a href="/emergencies">Emergencies</a></nav>
<ul>
  <li><a href="/emergencies/disease-outbreak-news/item/2026-DON600">Cholera – Zambia</a></li>
  <li><a href="/emergencies/disease-outbreak-news/item/2026-DON599">Mpox – DRC</a></li>
  <li><a href="/emergencies/disease-outbreak-news/item/2026-DON600">Cholera – Zambia</a></li>
  <li><a href="/emergencies/disease-outbreak-news">Listing</a></li>
</ul>
</body></html>
"""

CDC_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Travel Notices</title>
  <item>
    <title>Yellow Fever in Ghana</title>
    <link>https://wwwnc.cdc.gov/travel/notices/level2/yellow-fever-ghana</link>
    <pubDate>Wed, 18 Nov 2026 05:00:00 GMT</pubDate>
    <description>&lt;p&gt;Yellow fever transmission is ongoing in Ghana.&lt;/p&gt;</description>
  </item>
  <item>
    <title>Ebola in Kenya</title>
    <link>https://wwwnc.cdc.gov/travel/notices/level1/ebola-kenya</link>
    <pubDate>Mon, 09 Nov 2026 05:00:00 GMT</pubDate>
    <description>&lt;p&gt;Ebola cases reported.&lt;/p&gt;</description>
  </item>
</channel></rss>
"""

ECDC_HTML = """
<html><body>
<a href="/en/threats-and-outbreaks">Threats and outbreaks</a>
<a href="/en/disease-outbreaks/measles-europe">Measles in Europe</a>
<a href="/en/news-events/cholera-haiti">Cholera in Haiti</a>
<a href="/en/about-us">About</a>
</body></html>
"""

RELIEFWEB_JSON = {
    "data": [
        {
            "id": "123",
            "fields": {
                "title": "Cholera outbreak in Haiti - Situation Report",
                "url": "https://reliefweb.int/report/haiti/cholera-outbreak",
                "date": {"created": "2026-11-10T00:00:00+00:00"},
                "country": [{"name": "Haiti"}],
                "source": [{"name": "PAHO"}],
                "body": "<div><p>As of November, 1,240 suspected cases and 31 deaths "
                        "have been reported across the country.</p></div>",
            },
        }
    ]
}

CFG = {"url": "https://example.org", "reputation": 0.9, "tier": "HIGH"}


def test_who_listing_extracts_item_links_and_dedupes():
    found = parse_who_don(WHO_HTML, {**CFG, "url": "https://www.who.int/emergencies/disease-outbreak-news"}, 10)

    assert [c.url for c in found] == [
        "https://www.who.int/emergencies/disease-outbreak-news/item/2026-DON600",
        "https://www.who.int/emergencies/disease-outbreak-news/item/2026-DON599",
    ]
    assert found[0].source_name == "WHO DON"
    assert found[0].reputation == 0.9


def test_rss_parses_title_date_and_summary():
    found = parse_rss(CDC_RSS, {**CFG, "name": "CDC Travel Notices"}, 10)

    assert len(found) == 2
    assert found[0].title == "Yellow Fever in Ghana"
    assert found[0].url.endswith("/yellow-fever-ghana")
    assert found[0].published_at is not None
    assert "Yellow fever transmission" in found[0].inline_text


def test_ecdc_ignores_nav_and_self_links():
    found = parse_ecdc(ECDC_HTML, {**CFG, "url": "https://www.ecdc.europa.eu/en/threats-and-outbreaks"}, 10)

    urls = [c.url for c in found]
    assert "https://www.ecdc.europa.eu/en/disease-outbreaks/measles-europe" in urls
    assert "https://www.ecdc.europa.eu/en/news-events/cholera-haiti" in urls
    assert "https://www.ecdc.europa.eu/en/about-us" not in urls
    assert "https://www.ecdc.europa.eu/en/threats-and-outbreaks" not in urls


def test_reliefweb_uses_inline_body_and_country_hint():
    found = parse_reliefweb(RELIEFWEB_JSON, CFG, 10)

    assert len(found) == 1
    assert found[0].hint_country == "Haiti"
    assert "1,240 suspected cases" in found[0].inline_text
    assert found[0].published_at.year == 2026


def test_promed_falls_back_to_html_links_when_not_a_feed():
    html = b"""
    <html><body>
    <a href="/promed-post/?p=12345">Cholera in Zambia</a>
    <a href="/about">About</a>
    </body></html>
    """
    found = parse_promed(html, {**CFG, "url": "https://promedmail.org/promed-posts/"}, 10)

    assert len(found) == 1
    assert found[0].url == "https://promedmail.org/promed-post/?p=12345"


def test_cdc_slug_gives_disease_and_country_hints():
    disease, country = slug_hints(
        "https://wwwnc.cdc.gov/travel/notices/level2/yellow-fever-ghana"
    )
    assert disease == "Yellow Fever"
    assert country == "Ghana"


def test_candidate_key_ignores_query_string_and_trailing_slash():
    a = Candidate(source_name="x", url="https://a.org/one/?utm=1")
    b = Candidate(source_name="y", url="https://a.org/one")
    assert a.key() == b.key()
