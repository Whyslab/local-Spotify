"""Finding cover art for a new track: the right image, and only real images.

requests.get is replaced by a fake that answers by URL; nothing leaves the
machine.
"""

import pytest

from adder import ingest

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 "
HTML = b"<html>rate limited</html>"


class Response:
    def __init__(self, content=b"", payload=None, status=200):
        self.content = content
        self._payload = payload
        self.ok = status < 400

    def json(self):
        return self._payload


@pytest.fixture()
def web(monkeypatch):
    """Map of URL prefix -> Response; records every requested URL."""
    routes = {}
    requested = []

    def get(url, params=None, timeout=None):
        requested.append(url)
        for prefix, response in routes.items():
            if url.startswith(prefix):
                return response
        raise OSError(f"unexpected URL {url}")

    monkeypatch.setattr(ingest.requests, "get", get)
    routes["_requested"] = requested
    return routes


def itunes(*results):
    return Response(payload={"resultCount": len(results), "results": list(results)})


def hit(artist, track, art="https://art/100x100bb.jpg"):
    return {"artistName": artist, "trackName": track, "artworkUrl100": art}


@pytest.mark.parametrize(
    ("data", "fmt"),
    [(JPEG, "jpg"), (PNG, "png"), (WEBP, None), (HTML, None), (b"", None), (None, None)],
)
def test_image_format_trusts_the_bytes_not_the_url(data, fmt):
    assert ingest.image_format(data) == fmt


def test_itunes_skips_a_top_hit_that_is_another_song(web):
    web["https://itunes.apple.com/search"] = itunes(
        hit("Other Artist", "Song", "https://wrong/100x100bb.jpg"),
        hit("Artist", "Song (feat. X)", "https://right/100x100bb.jpg"),
    )
    web["https://right/3000x3000bb.jpg"] = Response(JPEG)

    assert ingest.get_hd_cover("Artist", "Song") == (JPEG, "jpg")
    assert "https://wrong/3000x3000bb.jpg" not in web["_requested"]


def test_itunes_without_a_matching_song_gives_no_cover(web):
    web["https://itunes.apple.com/search"] = itunes(hit("Someone", "Something Else"))

    assert ingest.get_hd_cover("Artist", "Song") == (None, None)


def test_an_html_answer_is_not_embedded_as_a_cover(web):
    web["https://deezer/cover.jpg"] = Response(HTML)

    assert ingest.fetch_cover_url("https://deezer/cover.jpg") == (None, None)


def test_a_png_cover_keeps_its_format(web):
    web["https://deezer/cover"] = Response(PNG)

    assert ingest.fetch_cover_url("https://deezer/cover") == (PNG, "png")


def test_webp_thumbnail_is_fetched_as_jpeg(web):
    web["https://itunes.apple.com/search"] = itunes()
    web["https://i.ytimg.com/vi/abc/maxresdefault.jpg"] = Response(JPEG)

    cover = ingest.fetch_cover("A", "B", "https://i.ytimg.com/vi_webp/abc/maxresdefault.webp")

    assert cover == (JPEG, "jpg")


def test_a_thumbnail_that_is_not_an_image_gives_no_cover(web):
    web["https://itunes.apple.com/search"] = itunes()
    web["https://i.ytimg.com/vi/abc/hq.jpg"] = Response(WEBP)

    assert ingest.fetch_cover("A", "B", "https://i.ytimg.com/vi/abc/hq.jpg") == (None, None)


def test_network_failure_gives_no_cover(web):
    # No routes at all: every request raises, as a dead network would.
    assert ingest.fetch_cover("A", "B", "https://i.ytimg.com/vi/abc/hq.jpg") == (None, None)
