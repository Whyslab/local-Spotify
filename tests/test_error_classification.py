"""
Tests for classify_error(), which decides whether a failed task is retried.

The bug that prompted these: the original classifier matched a bare "url"
anywhere in the message and returned "invalid_url" — not a retryable type — so
any transient failure whose text happened to mention the URL was treated as
permanent and never retried.
"""

import importlib.util
import os
from pathlib import Path

import pytest

from adder import ingest

os.environ.setdefault("API_TOKEN", "test-token")

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("adder.app", ROOT / "adder" / "app.py")
app_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_module)

classify_error = ingest.classify_error
RETRYABLE_ERRORS = ingest.RETRYABLE_ERRORS


class TestTransientErrorsAreRetryable:
    @pytest.mark.parametrize(
        "message",
        [
            "Read timed out while fetching https://www.youtube.com/watch?v=abc",
            "Connection reset by peer",
            "Network is unreachable",
            "HTTP Error 503: Service Unavailable",
            "The service is temporarily overloaded, try again",
        ],
    )
    def test_network_failures_are_retried(self, message):
        assert classify_error(message) == "network_error"
        assert classify_error(message) in RETRYABLE_ERRORS

    def test_a_timeout_mentioning_the_url_is_still_retryable(self):
        # The original classifier returned "invalid_url" here, which is not
        # retryable, so a recoverable timeout was abandoned on the first try.
        message = "ERROR: unable to download video data: timeout for url https://youtu.be/xyz"
        assert classify_error(message) == "network_error"
        assert classify_error(message) in RETRYABLE_ERRORS

    def test_a_download_failure_mentioning_the_url_is_retryable(self):
        message = "Download failed for url https://www.youtube.com/watch?v=abc"
        assert classify_error(message) in RETRYABLE_ERRORS


class TestPermanentErrorsAreNotRetried:
    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("Invalid URL: not a YouTube link", "invalid_url"),
            ("Unsupported URL: https://vimeo.com/1", "invalid_url"),
            ("ERROR: Video unavailable", "youtube_not_found"),
            ("This is a private video", "youtube_not_found"),
            ("Requested format not found", "youtube_not_found"),
        ],
    )
    def test_classification(self, message, expected):
        assert classify_error(message) == expected

    @pytest.mark.parametrize(
        "message",
        [
            "Invalid URL: not a YouTube link",
            "ERROR: Video unavailable",
            "No space left on device",
            "database is locked (sqlite3)",
        ],
    )
    def test_not_retryable(self, message):
        assert classify_error(message) not in RETRYABLE_ERRORS


class TestResourceErrors:
    def test_disk_space(self):
        assert classify_error("No space left on device") == "filesystem_error"

    def test_database(self):
        assert classify_error("sqlite3.OperationalError: database is locked") == "database_error"


class TestPipelineStages:
    def test_artwork(self):
        assert classify_error("Could not fetch artwork from iTunes") == "artwork_error"

    def test_metadata(self):
        assert classify_error("Failed to write metadata tags") == "metadata_error"

    def test_unknown_falls_through(self):
        assert classify_error("something nobody anticipated") == "unknown_error"

    def test_empty_message(self):
        assert classify_error("") == "unknown_error"


class TestRetryableSetIsCoherent:
    def test_every_retryable_type_is_reachable(self):
        produced = {
            classify_error(m)
            for m in [
                "connection reset",
                "Download failed",
                "Could not fetch artwork",
            ]
        }
        assert produced >= RETRYABLE_ERRORS, "a retryable type no classifier branch can return"
