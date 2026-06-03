"""Apify Actor entry point. Kicks off the asyncio event loop."""
import asyncio

from .main import main


if __name__ == '__main__':
    asyncio.run(main())
