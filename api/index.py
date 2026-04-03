"""
Vercel serverless entry point.
Wraps the FastAPI app from voice_mcp.py for deployment.

When deployed to Vercel, this file serves as the handler.
Locally, you still run `python voice_mcp.py` directly.
"""

import sys
import os

# Add project root to path so imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_mcp import app

# Vercel looks for an `app` variable (ASGI/WSGI) or a `handler` function.
# FastAPI is ASGI-compatible, so exporting `app` is sufficient.
