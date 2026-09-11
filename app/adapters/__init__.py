"""第三方设备族适配器。

每个子包对应一个**协议生态**（而不是一个品牌）—— 这样一处适配就能覆盖一批机器：

* `moonraker/` —— Klipper 生态（Voron、RatOS、刷 Klipper 的 Creality/Elegoo/Anycubic、
  Snapmaker U1）。接入依据见 `docs/FIELD_NOTES.md`。
* （规划中）`octoprint/` —— OctoPrint 生态。
* （规划中）`prusalink/` —— Prusa 全系（注意：只有静态快照，没有视频流）。

各适配器都应继承 `app.core.adapter.PollingDeviceSession`，并在实现完成后
到 `app.core.registry` 登记自己的 `FamilyDescriptor`（**不要提前登记**：
那会让界面出现一个选了也没用的选项）。
"""
