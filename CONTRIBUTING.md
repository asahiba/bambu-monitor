# 贡献指南

先谢谢你愿意花时间。这个项目是自用的局域网 3D 打印机监控台，欢迎 issue 与 PR。

## 快速开始（不用真打印机）

内置模拟器会把整条链路（MQTT over TLS 遥测、6000 端口鉴权与 JPEG 帧、
SSDP/2021 发现、HMS 错误码）都伪造出来，所以**开发与测试完全不需要真机**：

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows
# source .venv/bin/activate   # Linux / macOS
pip install -r requirements.txt

python -m pytest -q           # 回归套件，完全离线，约 1 分钟
python -m app --sim 4         # 起 4 台模拟打印机 + 图形界面
python -m app.headless --sim 4 --status-interval 0   # 只要网页端
```

网页端默认在 <http://127.0.0.1:8080>。首次启动会在日志里打印带令牌的地址。

## 提交前请务必跑一遍

```bash
python -m pytest -q                 # 必须全绿
python -m pytest -q -m slow         # 慢速端到端（模拟器 + 真实端口）
ruff check .                        # 静态检查
```

CI 会在 Python 3.10 与 3.13 上各跑一遍。**3.10 不是可选项**：
安卓版内嵌的就是 3.10，有些坑只有它会暴露（例如给 `threading.Thread`
子类挂 `self._stop` 会遮蔽 `Thread._stop()`，让 `join()` 抛 `TypeError`；
3.13 改了实现所以看不出来，详见 `docs/PACKAGING.md`）。

## 改代码前值得先读的文档

| 文档 | 什么时候读 |
| --- | --- |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 想知道某个功能在哪个模块；要接入新设备族 |
| [`docs/PROTOCOL.md`](docs/PROTOCOL.md) | 要动机型识别、报文解析、命令下发 |
| [`docs/FIELD_NOTES.md`](docs/FIELD_NOTES.md) | 要改机型差异、签名门禁、A2L 这类新机型行为 |
| [`docs/PACKAGING.md`](docs/PACKAGING.md) | 要改打包（PyInstaller / Docker / 安卓） |
| [`docs/KNOWN_ISSUES.md`](docs/KNOWN_ISSUES.md) | 准备动手修 bug，先看是不是已知的 |
| [`docs/DEVELOPING.md`](docs/DEVELOPING.md) | 环境搭建与工具链 |

## 几条硬性约定

### 1. 加设备支持请走设备族抽象，不要往核心里塞品牌判断

`app/core/` 是与厂商无关的内核，`app/adapters/<family>/` 放具体实现。
新增一个品牌应该是「注册一条数据」，而不是在 `app/web/`、`app/ui/` 里
到处加 `if 品牌 == ...`。见 `docs/ARCHITECTURE.md` 第 4 节。

### 2. 修 bug 请带一个会失败的测试

这个仓库的规矩是**先写出能复现的测试**，再改代码。测试名用中文描述行为
（例如 `test_没有DPAPI时加密只记提示不记错误`），失败信息写清"这说明什么坏了"。

尤其欢迎那种「旧写法会被本测试抓住」的自检式测试 —— 它证明测试真的有效，
而不是恰好通过。

### 3. 诊断信息要分级

**只有「这次操作真的失败了」才叫 error**，其余是提示。
反例（真实踩过的坑）：安卓没有 DPAPI，凭据退回明文保存是**正常降级**，
但被当成 `last_error`，导致「添加成功」被报成「添加失败当前系统没有 DPAPI」。
现在分 `last_error`（阻断）与 `warnings`（提示）两条通道。

### 4. 注释请写「为什么」，尤其是踩过的坑

这个仓库的注释密度偏高是刻意的：**协议层有很多反直觉的实测结论**
（某个字段是代号不是型号名、某机型没有某个通道、签名只挡 print 段命令…），
不写下来下一个人（包括三个月后的你）一定会重新踩一遍。
注释里请带上依据（实测机型/固件版本/官方文档链接）。

### 5. `.ps1` 脚本有两个必须遵守的格式要求

Windows PowerShell 5.1 的两个坑，都有契约测试兜底
（`tests/test_contracts.py`）：

* **含非 ASCII 字符就必须带 UTF-8 BOM** —— 否则按 GBK 解码，中文字符被拆坏后
  连带破坏语法，报一堆 `Unexpected token`，而且**行号对不上真实内容**。
* **含 here-string（`@"` … `"@`）就必须用 CRLF 换行** —— PS 5.1 不把只有 LF
  的换行当作 here-string 结束符，会报 `The string is missing the terminator: "@`。

改完请用 PS 5.1 复核一遍：

```powershell
$e = $null
[void][System.Management.Automation.Language.Parser]::ParseFile("路径.ps1", [ref]$null, [ref]$e)
if ($e) { $e | ForEach-Object { "$($_.Extent.StartLineNumber): $($_.Message)" } }
```

## PR 流程

1. Fork → 从 `main` 开分支（`feat/xxx`、`fix/xxx`）。
2. 一个 PR 只做一件事，改动尽量聚焦。
3. 提交信息说清「为什么」，不只是「改了什么」。
4. 确保 `pytest` 与 `ruff` 都过。
5. 涉及协议解析的改动，请说明**你是怎么验证的**（真机型号 + 固件版本，
   或者引用了哪份文档/抓包）。

## 不接受的内容

* 绕过打印机固件的授权机制、破解访问代码、或帮助绕过 Bambu 的账号体系。
  本项目只做**局域网内、用你自己设备的访问代码**做监控，不碰云端账号。
* 把真实设备的序列号、访问代码、内网 IP、抓包原文提交进仓库。

## 安全问题

请不要在 issue 里公开披露安全漏洞，见 [`SECURITY.md`](SECURITY.md)。
