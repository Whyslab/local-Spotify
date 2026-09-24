"""Which YouTube link forms /api/add accepts, and what they become."""

import pytest

from adder import ingest


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/shorts/abc123",
        "https://youtube.com/shorts/abc123?feature=share",
        "https://www.youtube.com/live/abc123",
        "https://www.youtube.com/embed/abc123",
        "https://m.youtube.com/shorts/abc123/",
    ],
)
def test_shorts_live_and_embed_links_are_accepted(url):
    assert ingest.canonicalize_youtube_url(url) == "https://www.youtube.com/watch?v=abc123"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/playlist?list=PL123",
        "https://www.youtube.com/@channel",
        "https://www.youtube.com/shorts/",
        "https://www.youtube.com/shorts/abc/extra",
    ],
)
def test_other_youtube_pages_are_still_refused(url):
    with pytest.raises(ValueError):
        ingest.canonicalize_youtube_url(url)
