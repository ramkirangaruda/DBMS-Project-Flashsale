"""
Vercel's Python entrypoint for the API. Vercel's Python runtime detects an
ASGI-callable named `app` in the module it's pointed at (see vercel.json's
`builds` entry for api/index.py) and wraps it directly -- no uvicorn, no
Mangum-style adapter, the same FastAPI object app/main.py already builds for
local/docker use is served as-is.

The only job here is making `import app` resolvable: Vercel's Python builder
runs the function with this file's own directory as the working context, not
necessarily the project root, so app/ (a sibling of api/, not a child of it)
isn't on sys.path by default. Fixed below before the import, rather than by
moving app/ under api/ -- that would fork the codebase into a
"real" copy and a "Vercel" copy of the same package, which is exactly the
kind of drift that turns into a deploy that works locally and 500s in
production.

See api/requirements.txt for why this function's dependency set is a
trimmed subset of the project's full requirements.txt, and the README's
Deploying to Vercel section for the environment variables this needs
(DATABASE_URL, REDIS_URL, ADMIN_TOKEN, ...).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402  (path fix above must run first)

__all__ = ["app"]
