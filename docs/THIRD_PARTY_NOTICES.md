# 第三方依赖

Render Desk 原创代码采用 MIT，第三方组件按各自许可分发。3.0 桌面使用 pywebview + Windows WebView2，不再打包 Qt / PySide6。

- pywebview 6.2.1：BSD-3-Clause。
- Flask 3.1.3、Werkzeug、Jinja2、ItsDangerous、Click、Blinker、MarkupSafe：各项目 BSD 许可。
- Waitress 3.0.2：ZPL-2.1。
- psutil 7.2.2：BSD-3-Clause。
- pythonnet、clr_loader、cffi、pycparser、proxy_tools、bottle、typing_extensions：以 licenses/ 中原始许可及包元数据为准。
- Python：PSF 许可与其随附声明。
- PyInstaller：GPL 及其分发例外，见对应 COPYING。
- WebView2 Runtime 是系统外部运行时，由 Microsoft 提供；不将整个 Edge 浏览器打包进应用。pywebview 所附桥接文件按其上游许可分发。

licenses/ 保存已安装依赖的许可文件和完整 METADATA（含版本、来源及许可声明）。Blender 需用户自行安装，不包含在便携包中。

Microsoft.Web.WebView2 1.0.3856.49 SDK 桥接 DLL 所对应的 LICENSE 与 NOTICE 从 Microsoft 官方 NuGet 包取得，保存为 licenses/WebView2-LICENSE.txt 与 WebView2-NOTICE.txt。

## 3.1 additions
- watchdog 6.0.0 — Apache-2.0; file notifications. License in licenses/.
- Pillow 12.3.0 — HPND / bundled image codec licenses; previews. License in licenses/.
- frpc 0.71.0 Windows amd64 — official unmodified frp client; Apache-2.0. License and source/checksum in vendor/frpc/.
