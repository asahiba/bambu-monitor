"""一次性脚本：给安卓构建接入固定签名（见 docs/PACKAGING.md 的说明）。

## 为什么必须做

一直用 debug 签名发布会有两个问题：
1. **每台机器/每次 CI 的 debug keystore 都不同**，同一个包名用不同签名安装时
   系统会拒绝覆盖（用户必须卸载重装、丢配置）；
2. Release 构建压根没配签名，出的是未签名包。

所以：生成一个**固定的发布 keystore**，debug 与 release 都用它签，
签名信息放在仓库外（本地 `.android-signing/`，CI 从 GitHub Secrets 还原）。

用法：python tools/add_signing_config.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GRADLE = ROOT / "android" / "app" / "build.gradle"

#: 插在 android { ... } 开头：读取签名配置（可选，读不到就退回 debug 签名）
SIGNING_BLOCK = """
    // ------------------------------------------------------------------ 签名
    //
    // ⚠️ 为什么要有这一段：**一直用 debug 签名会让用户无法覆盖安装**。
    // debug keystore 是每台机器/每次 CI 各自生成的，同一个包名换签名安装时
    // 系统会直接拒绝（"应用未安装"），用户只能卸载重装 —— 配置全丢。
    //
    // 所以这里读一个**固定的发布 keystore**，并让 debug 构建也用它签名：
    //   * 本地：android/keystore.properties（由 build-apk.ps1 从未入库的
    //     .android-signing/ 目录复制过来）
    //   * CI：工作流从 GitHub Secrets 还原 keystore 与口令后写出同一个文件
    //   * 两处都没有时**退回 debug 签名**并打印提示 —— 保证新克隆的仓库
    //     仍能直接构建，不至于因为缺密钥而卡住
    def signingProps = new Properties()
    def signingFile = rootProject.file("keystore.properties")
    if (signingFile.exists()) {
        signingFile.withInputStream { signingProps.load(it) }
    }
    def hasReleaseSigning = signingProps.getProperty("storeFile") != null

    signingConfigs {
        release {
            if (hasReleaseSigning) {
                storeFile rootProject.file(signingProps.getProperty("storeFile"))
                storePassword signingProps.getProperty("storePassword")
                keyAlias signingProps.getProperty("keyAlias")
                keyPassword signingProps.getProperty("keyPassword")
            }
        }
    }
"""

BUILD_TYPES_OLD = """    buildTypes {
        release {
            minifyEnabled false
            proguardFiles getDefaultProguardFile('proguard-android-optimize.txt'), 'proguard-rules.pro'
        }
    }"""

BUILD_TYPES_NEW = """    buildTypes {
        release {
            minifyEnabled false
            proguardFiles getDefaultProguardFile('proguard-android-optimize.txt'), 'proguard-rules.pro'
            if (hasReleaseSigning) {
                signingConfig signingConfigs.release
            }
        }
        debug {
            // debug 也用固定签名：否则用户在真机上装过一版之后就再也覆盖不上了
            if (hasReleaseSigning) {
                signingConfig signingConfigs.release
            }
        }
    }
"""


def main() -> int:
    raw = GRADLE.read_bytes()
    had_bom = raw.startswith(b"\xef\xbb\xbf")
    crlf = b"\r\n" in raw
    text = raw.decode("utf-8-sig").replace("\r\n", "\n")

    if "signingConfigs" in text:
        print("已经接入签名配置，无需改动。")
        return 0

    anchor = "android {\n"
    if anchor not in text:
        print("[失败] 找不到 android { 块", file=sys.stderr)
        return 1
    text = text.replace(anchor, anchor + SIGNING_BLOCK, 1)

    if BUILD_TYPES_OLD not in text:
        print("[失败] 找不到 buildTypes 块", file=sys.stderr)
        return 1
    text = text.replace(BUILD_TYPES_OLD, BUILD_TYPES_NEW, 1)

    if crlf:
        text = text.replace("\n", "\r\n")
    GRADLE.write_bytes(text.encode("utf-8-sig" if had_bom else "utf-8"))
    print("已接入固定签名（release 与 debug 共用），缺密钥时自动退回 debug 签名")
    print(f"（BOM={had_bom}，CRLF={crlf}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
