"""Short-lived signed URLs for the audio stream.

An <audio> element cannot send an Authorization header, and the whole frontend
authenticates with one. That leaves three ways to let a browser play a file and
two of them are bad: a cookie adds a CSRF surface to a service listening on
0.0.0.0, and fetching the track into a Blob throws away seeking. The third is a
signed URL, which is what this is.

What travels in the address is not the API token. It is a signature over one
path and one expiry, so intercepting it buys a single track until it expires
and nothing else. The signing key is derived from API_TOKEN rather than
configured separately: a second mandatory variable would follow config.py's
fail-closed rule and take CI down with it, since the workflow sets only
API_TOKEN.
"""

import hashlib
import hmac
import time
from urllib.parse import quote

from . import config

_INFO = b"local-Spotify/stream-url/v1"

# Long enough to cover the whole track plus a pause, because <audio> opens a
# fresh connection for every seek and every resume after a stall. A one-minute
# link would expire mid-track and turn a scrub into a 403 on everything longer
# than a minute -- which is everything.
GRACE_SECONDS = 300
MIN_LIFETIME = 600


def _key() -> bytes:
    """Derive the signing key from the API token.

    HMAC with a fixed info string, in the shape of an HKDF expand step: the
    token is already high-entropy, and this keeps a leaked stream signature
    from being reversible into the token itself.
    """
    return hmac.new(config.API_TOKEN.encode("utf-8"), _INFO, hashlib.sha256).digest()


def sign(path: str, expires_at: int) -> str:
    """Sign a library-relative path together with its expiry.

    The path is signed decoded: percent-encoding is a property of the URL, not
    of the file, and signing the encoded form would make the same track hash
    differently depending on how the client happened to spell it.
    """
    payload = f"{path}\n{expires_at}".encode()
    return hmac.new(_key(), payload, hashlib.sha256).hexdigest()[:32]


def lifetime_for(duration: float | None) -> int:
    seconds = int(duration or 0) + GRACE_SECONDS
    return max(seconds, MIN_LIFETIME)


def stream_url(path: str, duration: float | None = None, now: float | None = None) -> dict:
    """Build a playable URL for one track."""
    expires_at = int((now if now is not None else time.time()) + lifetime_for(duration))
    return {
        "url": f"/api/stream?path={quote(path)}&exp={expires_at}&sig={sign(path, expires_at)}",
        "expires_at": expires_at,
    }


def verify(path: str, expires_at: str | int, signature: str, now: float | None = None) -> bool:
    """True when the signature matches and has not expired."""
    try:
        expiry = int(expires_at)
    except (TypeError, ValueError):
        return False
    if expiry < (now if now is not None else time.time()):
        return False
    # compare_digest, not ==, so a wrong signature cannot be discovered one
    # character at a time by timing the answer.
    return hmac.compare_digest(sign(path, expiry), signature or "")
