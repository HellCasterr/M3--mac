from __future__ import annotations

import logging
import webbrowser
from threading import Timer

import uvicorn

from trade_m2.config import get_settings


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    url = f"http://127.0.0.1:{settings.port}"
    Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(
        "trade_m2.app:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
