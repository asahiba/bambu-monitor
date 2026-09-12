"""Linux / Docker 打包入口（PyInstaller 用）。"""

import sys

from app.headless import main

if __name__ == "__main__":
    sys.exit(main())
