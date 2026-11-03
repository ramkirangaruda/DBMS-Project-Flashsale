"""
One admin-token check for every operator endpoint (/admin/reset-sale,
/queue/reset, /queue/control/reset, /db-explorer).

Policy is unchanged from the original per-endpoint checks: when ADMIN_TOKEN
is unset the endpoints stay open (the LAN classroom demo), and when it is set
every one of them requires ?token=<value>. Two things are new:

  * the comparison is constant-time (hmac.compare_digest), so the token
    cannot be recovered byte by byte from response timing;
  * ADMIN_TOKEN is read per call, not once at import, so every endpoint sees
    the same value -- app/main.py used to cache it at import time while
    app/dbexplorer.py re-read it, and the two could disagree.

The queue reset endpoints had no check at all before this module existed:
anyone could empty a live sale's waiting room and revoke every outstanding
admission mid-drop.
"""
import hmac
import os


def admin_token_required() -> str:
    return os.getenv("ADMIN_TOKEN", "")


def admin_token_ok(token) -> bool:
    required = admin_token_required()
    if not required:
        return True
    return hmac.compare_digest((token or "").encode("utf-8"), required.encode("utf-8"))
