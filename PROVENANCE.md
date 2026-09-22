# 实现来源与独立性记录

本版本是 BlenderRenderDesk 2.0.0 的独立重写，目标是提供完整公开源码、可自行构建、不设置试用期或付费功能门槛的本地渲染管理器。

开发前检查过 Batch Render Creator 的公开文档、作者公开的问题报告，以及合法取得的试用包所带的 Blender 插件和可阅读脚本。因此不将本工作描述为“从未接触过参考实现”的严格净室开发。

重写采用的输入是用户需求与行为层面的结论：后台启动 Blender、逐帧保存后暂停、持久化任务状态、按变化刷新界面、读取日志和资源统计。这些功能由本项目自己的模块实现。

## 本项目的实现选择

- 自行编写 Qt Widgets 桌面界面，采用独立的布局、配色、标识和文案。
- 自行定义 SQLite 任务表及任务状态文件协议；不读取或转换 BRC 的数据库。
- 自行编写逐帧渲染执行器：临时输出、文件摘要、独占提交、完成回执、协作暂停。
- 自行编写进程识别、外部 PNG 序列观察、日志增量读取与 Windows PDH 资源采集代码。
- 仅为本项目 1.x 数据格式保留兼容导入。

## 没有纳入的材料

发布包不包含 BRC EXE、提取的源码、其网页资产、图标、品牌素材、授权信息或任何试用限制绕过代码。参考下载目录独立于本项目，不参与构建和源码打包。

本项目不声称拥有 Blender、Qt 或其他依赖的版权。项目原创代码采用 MIT 许可；第三方运行库继续适用自己的许可，详见 THIRD_PARTY_NOTICES.md。这里记录了开发措施与来源，不作绝对无侵权的法律保证。

## 可核查的参考来源

- Blender 命令行说明：https://docs.blender.org/manual/en/latest/advanced/command_line/arguments.html
- Blender Python API：https://docs.blender.org/api/current/bpy.ops.render.html
- Qt for Python：https://doc.qt.io/qtforpython-6/
- Python sqlite3：https://docs.python.org/3/library/sqlite3.html
- psutil：https://psutil.readthedocs.io/
- Microsoft PDH：https://learn.microsoft.com/en-us/windows/win32/perfctrs/using-the-pdh-functions-to-consume-counter-data
- BRC 功能文档：https://brc.wanderson3d.com/

第三方名称仅用于准确说明来源，不表示赞助、授权合作或关联关系。
