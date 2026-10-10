"""入口：python -m gitwarden。单 worker 启动，调度器与 WebUI 同进程。"""

from __future__ import annotations

import sys

import uvicorn

from gitwarden.config import ConfigError, load_config
from gitwarden.web import create_app


def main() -> None:
    try:
        config = load_config()
    except ConfigError as exc:
        sys.exit(f"启动失败: {exc}")
    app = create_app(config)
    uvicorn.run(app, host="0.0.0.0", port=config.port, workers=1)


if __name__ == "__main__":
    main()
