# 安全策略

## 报告漏洞

请**不要**用公开 issue 报告安全问题。用 GitHub 的
[私密漏洞报告](https://docs.github.com/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
（仓库 Security 标签页 → Report a vulnerability），或者直接开一个
issue 只写「我想报告一个安全问题，请给我联系方式」，正文不要带细节。

我会尽快回复。这是个自用为主的小项目，没有专职安全团队，但会认真对待。

请在报告里尽量给出：受影响版本、复现步骤、影响面（能读到什么/能控制什么）。

## 这个项目的安全模型

理解下面几点，能帮你判断某个现象算不算漏洞。

### 威胁模型：局域网内、可信网络

本项目设计为在**你自己的局域网**里运行，连接**你自己的**打印机。它不是、
也不打算做成一个能安全暴露到公网的服务。

### 网页访问令牌不是身份认证

网页端用一个随机令牌（`web_token`）挡一下，作用是**避免同网段的其它人
随手打开你的监控页**，它：

* 是明文比对、无速率限制、无会话过期；
* 会出现在 URL 查询串里（`/?token=...`），因而可能进入浏览器历史与日志；
* 通过 HTTP 明文传输。

所以**不要把服务端口暴露到公网**。真要远程访问，请自己加一层 VPN 或
带 TLS 与认证的反向代理。`docs/DEPLOY.md` 里有说明。

### 凭据（打印机访问代码）的存储

* **Windows**：用 DPAPI 加密后存进配置文件，绑定当前 Windows 用户。
* **Linux / Docker / 安卓**：**没有 DPAPI，是明文存储**。
  程序会就此给出提示（`AppConfig.warnings`），但确实是明文。
  请自行保证配置文件所在的目录权限（`chmod 600`，别提交进 git）。

安卓版把配置放在应用私有目录（`filesDir`），受 Android 沙箱保护，
但 root 过的设备或备份文件里能看到明文。

### 已知的、不算漏洞的行为

| 现象 | 说明 |
| --- | --- |
| 同网段的人只要拿到令牌就能看画面、控制打印机 | 设计如此；令牌就是唯一门槛 |
| 不带令牌访问 `/` 返回 401 | 预期行为 |
| `/health` 不需要令牌 | 刻意如此，供容器健康检查用；只回 `ok`，不泄露状态 |
| 配置文件里访问代码是明文（Linux/安卓） | 平台限制，已在文档与界面提示 |
| 服务默认绑 `0.0.0.0` | 为了让同网段的手机也能看，见上面的威胁模型 |

### 依赖面

Python 侧只依赖 `paho-mqtt`、`cryptography`、`opencv-python(-headless)`，
以及可选的全部标准库实现（网页服务用 `http.server`）。
安卓版额外用 Chaquopy 内嵌 CPython。

## 支持的版本

项目处于早期（v1.x），**只对最新 release 提供安全修复**。
发现漏洞时请尽量在最新版本上复现。
