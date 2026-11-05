"""Sentinel Trade backend. Run: uvicorn main:app --reload --host 127.0.0.1 --port 8000

Login exchanges api_key + api_secret + request_token for an access token, kept in a local file; neither the token nor the
secret is ever returned to the browser.

  core.py        the app object, Kite session and shared Kite helpers
  routes/        HTTP routes, one module per feature
  strategies/    ORB, momentum sweep, market regime, paper desks, improvement loop (paper only)
  terminal/      market terminal: Nifty 500 metrics, calendar, fundamentals, deals
"""
from core import app  # noqa: F401 - uvicorn loads main:app
import routes.auth  # noqa: F401 - registers its routes
import routes.market  # noqa: F401 - registers its routes
import routes.options  # noqa: F401 - registers its routes
import routes.ticks  # noqa: F401 - registers its routes
import routes.orders  # noqa: F401 - registers its routes
import routes.darvas  # noqa: F401 - registers its routes
import routes.strategies  # noqa: F401 - registers its routes
import routes.news  # noqa: F401 - registers its routes
import routes.paper  # noqa: F401 - registers its routes
import routes.alerts  # noqa: F401 - registers its routes
import routes.terminal_mount  # noqa: F401 - registers its routes

