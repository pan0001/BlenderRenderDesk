# Blender Render Desk 2

**给后台 Blender 渲染一个看得见、能暂停、能继续的工作台。**

你让 GPT 或脚本开始了渲染，却不知道它正在处理哪一帧、已经跑了多久、占用了多少资源？Render Desk 把任务、进度和日志放到一个独立窗口里。

渲染到一半，突然想起原神、星穹铁道的每日还没做，或朋友喊你上号？点击“按帧暂停”，等当前帧保存后退出 Blender，释放渲染进程占用的资源；回来后继续未完成的帧。

作者：[B站 @in_uni](https://space.bilibili.com/1856651886/) · **MIT 开源 · 不设试用期 · 功能不收费解锁**

![界面](preview.png)

## 2.0 重写了什么

- Qt 原生桌面界面，独立设计的深色配色、列表、滚动条和任务详情。
- SQLite 任务目录；任务控制、资源采集、日志读取在后台线程运行。
- 独立的 Blender 执行器，在完整保存一帧后处理暂停并退出进程。
- 完成帧保存 SHA256 摘要；续渲染验证文件，跳过完整帧，修复缺失帧，拒绝覆盖异常文件。
- PNG、OpenEXR；可选场景、相机、CPU 线程数；支持含中文和空格的路径。
- 显示完成帧、当前帧、累计运行时间、基于已完成帧耗时估算的剩余时间。
- 本机 Blender 进程列表，CPU、内存，以及 Windows 提供计数器时的进程 GPU / 显存。
- 增量实时日志、跟随开关、完整日志文件、定时开始与定时按帧暂停。
- 兼容导入 Render Desk 1.x 任务。已运行的 Blender 通过 PID 和创建时间识别。

## 使用

Windows 解压完整便携包后运行 `BlenderRenderDesk.exe`，不要只复制单个 EXE。

1. 点击“新建任务”，选择本机 Blender 程序和已保存的 .blend 工程。
2. 确认帧范围、格式和输出根目录；程序会创建独立子目录。
3. 选择任务，点击“开始 / 继续”。默认不允许工程自动执行 Python，可为可信工程勾选。
4. 需要暂时使用电脑时，点击“按帧暂停”。当前帧可能仍需一段时间才能完成；界面会显示正在等待。

关闭管理器窗口不会结束已启动的 Blender。再次打开同一数据目录即可看到任务。定时开始需要管理器保持打开；由 2.0 执行器启动的任务会自行检查已设的暂停截止时间，但仍会等待当前帧完成。旧版执行器或首次接入的外部进程需要管理器运行才能执行定时暂停。

暂停保存的是已完成的帧和任务状态，**不是把渲染到一半的 GPU 内存保存到磁盘**。恢复会重新加载 .blend，因此有加载开销。不可恢复的模拟状态、依赖预烘焙的场景仍需用户准备好缓存。

## 控制已经存在的进程

本机进程页能显示可访问的 Blender 进程及资源。未接入且没有可读进度来源的进程会显示“未知”，不会伪造百分比。

目前支持接入：使用单个 `--python` 脚本启动、同步逐帧调用 `bpy.ops.render.render(write_still=True)`、输出单视图 PNG 序列的后台 Blender。接入时确认它的实际输出目录、帧范围和文件名模板，例如 `####.png`。

首次暂停通过完整 PNG 保存事件判断停止时机，再结束选定进程，可能丢弃紧接着开始的下一帧。续渲染重新运行原脚本，通过本项目的协作执行器跳过已完成帧，并在后续帧边界暂停。原脚本的初始化和副作用也会重新执行，适合可重跑的渲染脚本。

原进程历史 stdout 无法凭空补接。首次接入显示观察事件，续渲染后保存完整 stdout / stderr。Blender 编辑窗口、直接 `-a` 动画命令、异步脚本、多视图、视频输出等不支持此接入控制，仍可查看资源。

合成器 File Output 节点继续使用工程设置，可能向其他目录写文件；主图像输出之外的文件不在当前逐帧事务与摘要校验范围内。

## 升级与数据

默认数据目录：`%LOCALAPPDATA%/BlenderRenderDesk`。2.0 增加 `renderdesk-v2.sqlite3`，保留 `jobs/<id>/` 下的配置、日志与进度。首次导入不会改写旧任务文件；只有用户启动或控制任务时才更新对应状态。

使用 `--data-dir "完整路径"` 可指定已有工作空间。关闭旧管理器后再打开新版，两个版本不能同时控制同一个数据目录。使用自定义目录的用户升级后也必须指定相同目录。2.0 创建数据库后，同一任务的配置以数据库为准，不支持与 1.x 来回修改并双向同步。

不要修改渲染中的源工程；修改后需要新建任务，避免混合不同版本的输出。进度文件格式错误会报告问题，不会自动删除用户数据。

## 资源与性能读数

CPU 百分比按全部逻辑处理器容量归一化。内存为进程工作集。Windows GPU 数字表示该进程最忙引擎的利用率，显存来自进程专用显存计数器；驱动不支持或没有有效采样时显示“—”。它们不代表跨多 GPU 汇总后的统一负载。

后台渲染使用 Blender 的相同渲染引擎。关闭图形界面通常可减少界面、视口等资源开销，但渲染速度改善取决于场景和系统，不承诺固定百分比。官方支持以命令行在后台渲染：[Blender 命令行文档](https://docs.blender.org/manual/en/latest/advanced/command_line/arguments.html)。

## 源码运行与构建

Python 3.10–3.14；当前 Windows 构建和实测使用 Python 3.13、Blender 5.1.2。

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python app.py
.\.venv\Scripts\python -m unittest -v test_engine.py
.\.venv\Scripts\python test_desktop.py
.\.venv\Scripts\python test_blender_integration.py --blender "C:/Program Files/Blender Foundation/Blender 5.1/blender.exe"
.\build.ps1 -Python .\.venv\Scripts\python.exe
```

`build.ps1` 输出可替换动态库的目录式程序。构建需要 PyInstaller；脚本会安装固定版本。应用不包含 Blender，需要用户自行安装。

模块：`desktop.py` 界面及线程通信，`service.py` 调度和任务控制，`storage.py` SQLite，`processes.py` 资源监测，`runner.py` Blender 执行器，`protocol.py` 文件事务与回执。

## 开源与来源

本项目是独立实现，不属于 Blender 官方，也不属于 Batch Render Creator。没有复制后者的源码、界面资产或授权逻辑，没有试用限制绕过功能。

原创代码使用 [MIT](LICENSE)。参考过程与实现来源见 [PROVENANCE.md](PROVENANCE.md)，Qt/Python 等依赖的权利声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
