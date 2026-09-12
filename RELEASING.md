# 发布流程

面向维护者。用户只需要看 [Releases](https://github.com/asahiba/bambu-monitor/releases) 页面。

## 自动化程度

推一个 `v*` 标签，[`.github/workflows/release.yml`](.github/workflows/release.yml)
会自动在 Windows 与 Linux 两个 runner 上构建全部产物、做冒烟验证、
建 Release 并把单文件挂上去。**不需要手工上传任何东西。**

为什么必须两个 runner：PyInstaller **不能跨平台编译**，
Windows 的 exe 必须在 Windows 上打，Linux 的 ELF 必须在 Linux 上打。
安卓 APK 也放在 Windows runner（`android/build-apk.ps1` 是 PowerShell 脚本，
且 Chaquopy 在 Windows 上的构建路径是验证过的）。

## 发一个版本

```bash
# 1. 改版本号（三处要一致）
#    app/__init__.py            __version__ = "1.1.0"
#    android/app/build.gradle   versionName "1.1.0" / versionCode 2
#    CHANGELOG.md               把「未发布」改成新版本号并补日期

# 2. 提交
git add -A && git commit -m "发布 v1.1.0"

# 3. 打标签并推送（这一步会触发构建与发布）
git tag -a v1.1.0 -m "v1.1.0"
git push origin main
git push origin v1.1.0
```

然后在 Actions 页面看着它跑完（首次约 20-30 分钟，主要是下载依赖与
Qt/Chaquopy/Android SDK）。跑完后 Releases 里就有了。

### 产物命名

工作流会把构建输出重命名成面向用户的名字：

| 构建来源 | Release 里的文件名 |
| --- | --- |
| `dist/BambuMonitor.exe` | `BambuMonitor-windows-x64.exe` |
| `dist-onefile-headless/BambuMonitor-headless` | `BambuMonitor-linux-headless-x64` |
| `dist-onefile-linux/BambuMonitor-linux` | `BambuMonitor-linux-gui-x64` |
| `dist-docker/*.tar.gz` | `BambuMonitor-docker-image.tar.gz` |
| `dist-android/*.apk` | `BambuMonitor-android-arm64.apk` |

同时生成 `SHA256SUMS.txt` 一并附上。

## 本地出包（不发 Release 时）

```powershell
# Windows exe + APK
python -m PyInstaller --noconfirm --clean BambuMonitor-onefile.spec
powershell -ExecutionPolicy Bypass -File android\build-apk.ps1
```

```bash
# Linux（需要 Docker；PyInstaller 不能跨平台编译）
bash linux/build-headless-docker.sh    # 无界面版
bash linux/build-onefile-docker.sh     # 带界面版
```

```powershell
# Docker 镜像（导出成单个 tar.gz）
powershell -ExecutionPolicy Bypass -File build-docker-image.ps1
```

```powershell
# 全部归集到 dist-all/ 并生成 README 与校验清单
powershell -ExecutionPolicy Bypass -File make-bundle.ps1
```

## 发布前检查清单

- [ ] `python -m pytest -q` 全绿（**3.10 与 3.13 都要**，CI 会跑）
- [ ] `python -m pytest -q -m slow` 端到端通过
- [ ] `ruff check .` 无告警
- [ ] `CHANGELOG.md` 已更新，包含「已知限制」
- [ ] 版本号三处一致（`app/__init__.py`、`build.gradle`、CHANGELOG）
- [ ] 产物的冒烟测试都过了（构建脚本里已内置：exe `--core-test`、
      Linux 容器 `/health`、Docker 容器 `/health`）
- [ ] Android 若有改动，确认 `.venv310` 与 Chaquopy 依赖矩阵没变
      （见 `docs/PACKAGING.md` 那节，矩阵一变就得重新选 Python 版本）

## 已知的发布注意点

### APK 是 debug 签名

CI 出的是 debug 签名包，**只能自用安装**。要上架应用商店需要：

1. 生成正式 keystore，放进 repository secrets；
2. 在 `android/app/build.gradle` 里加 `signingConfigs` 并让 release 构建用它；
3. 改工作流构建 `assembleRelease` 而不是 `assembleDebug`。

**keystore 与密码绝不能进仓库**（`.gitignore` 已挡住 `*.keystore` / `*.jks`）。

### Windows exe 可能被杀毒软件误报

PyInstaller 的一次性打包 exe 没有代码签名，误报很常见。
Release 说明里已就此提示用户；要根治只能买代码签名证书。

### Release 体积

五件全发约 490 MB。GitHub 单文件上限 2 GB，单 Release 没硬性总量限制，
但下载体验会随体积下降。如需精简，Docker 镜像与 Linux 带界面版受众最小。

### 校验清单的编码

`SHA256SUMS.txt` 必须是**无 BOM 的 UTF-8** —— 带 BOM 会让第一行哈希前面
多出 `EF BB BF`，Linux 的 `sha256sum -c` 会直接失败。
工作流里用 `sha256sum *` 生成，是纯 ASCII，不存在这个问题；
本地用 `make-bundle.ps1` 生成时已显式指定 `UTF8Encoding($false)`。
