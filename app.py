from __future__ import annotations
import argparse
import ctypes
import datetime as dt
import json
import math
import os
import queue
import subprocess
import threading
import time
import webbrowser
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from core import Manager, LABELS, detect_blender, scan_processes, default_data_dir, read_json, resource
from backend import BackgroundManager

from ui import BG, PANEL, INK, MUTED, ACCENT, BORDER, label, card, button, entry, combo, checkbox, dark_titlebar, PageStack, AutoScrollbar, progress_bar

def timestamp(t):
    return dt.datetime.fromtimestamp(t).strftime('%m-%d %H:%M:%S') if t else '—'

def duration(seconds):
    if seconds is None: return '—'
    n=int(seconds)
    return f'{n//3600:02d}:{n//60%60:02d}:{n%60:02d}'

def memory(mb):
    return '—' if mb is None else (f'{mb/1024:.1f} GB' if mb>=1024 else f'{mb} MB')

def open_path(path):
    if not Path(path).exists():
        raise ValueError('路径尚不存在。任务开始后会生成日志。')
    os.startfile(str(path))

class App:
    def __init__(self, root, manager):
        self.root = root
        self.messages = queue.Queue()
        self.manager = BackgroundManager(manager,self.messages)
        self.last_log = None
        self.poll_time = 0
        self.row_values = {}
        self.proc_values = {}
        self.processes = {}
        self.scanning = False
        self.scan_time = 0
        self.root.title('Blender Render Desk · 本机渲染管理')
        if resource('app.ico').exists():
            self.root.iconbitmap(default=str(resource('app.ico')))
        self.root.geometry('1260x860')
        self.root.minsize(1060, 760)
        self.root.configure(bg=BG)
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI', 10), background=BG, foreground=INK)
        style.layout('Treeview', [('Treeview.treearea', {'sticky': 'nswe'})])
        style.configure('Treeview', background=PANEL, fieldbackground=PANEL, foreground='#d8d8e4',
                        rowheight=44, borderwidth=0, relief='flat')
        style.configure('Treeview.Heading', background='#202028', foreground=MUTED,
                        font=('Microsoft YaHei UI', 9), relief='flat', borderwidth=0, padding=(12, 12))
        style.map('Treeview.Heading', background=[('active', '#282832')])
        style.map('Treeview', background=[('selected', '#38304f')], foreground=[('selected', '#f3eeff')])

        sidebar = tk.Frame(root, bg='#17171d', width=188)
        sidebar.pack(side='left', fill='y')
        sidebar.pack_propagate(False)
        brand = tk.Frame(sidebar, bg='#17171d')
        brand.pack(fill='x', padx=22, pady=(30, 34))
        mark = tk.Canvas(brand, width=34, height=36, bg='#17171d', highlightthickness=0)
        mark.pack(side='left', padx=(0, 10))
        mark.create_polygon(17, 2, 32, 10, 32, 27, 17, 35, 2, 27, 2, 10, fill='#29233d', outline=ACCENT, width=2)
        mark.create_line(2, 10, 17, 19, 32, 10, fill=ACCENT, width=2)
        mark.create_line(17, 19, 17, 35, fill=ACCENT, width=2)
        label(brand, 'Render Desk', 12, bold=True).pack(side='left')
        label(sidebar, '工作空间', 9, '#686879', anchor='w').pack(fill='x', padx=25, pady=(0, 12))
        self.jobs_nav = button(sidebar, text='▦   渲染任务', command=lambda: self.navigate('jobs'), width=156)
        self.jobs_nav.pack(padx=16, pady=4)
        self.proc_nav = button(sidebar, text='◎   本机进程', command=lambda: self.navigate('processes'), width=156, style='Quiet')
        self.proc_nav.pack(padx=16, pady=4)
        bottom = tk.Frame(sidebar, bg='#17171d')
        bottom.pack(side='bottom', fill='x', padx=18, pady=24)
        button(bottom, text='使用指南', style='Quiet', command=self.help_dialog, width=150).pack(fill='x', pady=(0, 22))
        tk.Frame(bottom, bg=BORDER, height=1).pack(fill='x')
        label(bottom, '●  本地运行', 9, '#81cbb0', anchor='w').pack(fill='x', pady=(18, 5))
        label(bottom, 'BLENDER RENDER MANAGER', 7, '#636372', anchor='w').pack(fill='x')
        label(bottom, 'DESKTOP  /  1.3.1', 8, '#636372', anchor='w').pack(fill='x', pady=(4, 0))
        tk.Frame(root, bg='#24242c', width=1).pack(side='left', fill='y')
        main = tk.Frame(root, bg=BG)
        main.pack(side='left', fill='both', expand=True, padx=28)
        header = tk.Frame(main, bg=BG)
        header.pack(fill='x', pady=(25, 21))
        heading = tk.Frame(header, bg=BG)
        heading.pack(side='left')
        label(heading, 'WORKSPACE  /  BLENDER', 8, '#858096', anchor='w').pack(anchor='w')
        self.page_title = label(heading, '渲染工作台', 23, bold=True, anchor='w')
        self.page_title.pack(anchor='w', pady=(5, 4))
        self.subtitle = label(heading, '让每一帧都有进度。', 10, MUTED, anchor='w')
        self.subtitle.pack(anchor='w')
        button(header, text='＋  新建渲染', style='Accent.TButton', command=self.add_dialog, width=142).pack(side='right')
        metrics = tk.Frame(main, bg=BG)
        metrics.pack(fill='x', pady=(0, 20))
        metrics.columnconfigure((0, 1, 2), weight=1, uniform='metric')
        self.stat_values = []
        for i, (title, caption, color) in enumerate([
            ('渲染任务', '保存在本机的项目', INK),
            ('正在运行', '由 Render Desk 管理', ACCENT),
            ('Blender 进程', '自动发现 · 每 2 秒刷新', '#81cbb0')]):
            item = card(metrics, height=94)
            item.grid(row=0, column=i, sticky='ew', padx=(0 if i == 0 else 7, 0 if i == 2 else 7))
            item.grid_propagate(False)
            label(item, title, 9, MUTED).place(x=19, y=14)
            number = label(item, '0', 25, color, bold=True)
            number.place(x=18, y=36)
            label(item, caption, 8, '#777687').place(relx=1, x=-16, y=65, anchor='e')
            self.stat_values.append(number)
        footer = tk.Frame(main, bg=BG, height=31)
        footer.pack(side='bottom', fill='x', pady=(6, 8))
        label(footer, '●', 8, '#81cbb0').pack(side='left', padx=(0, 7))
        self.notice = label(footer, '已就绪  ·  任务和进度自动保存', 8, '#7e7c8e', anchor='w')
        self.credit = button(footer, text='B站：@in_uni ↗', style='Quiet', width=128,
                             command=lambda: webbrowser.open('https://space.bilibili.com/1856651886/'))
        self.credit.pack(side='right', padx=(16, 0))
        self.notice.pack(side='left', fill='x', expand=True)
        self.notebook = PageStack(main)
        self.notebook.pack(fill='both', expand=True)
        self.jobs_tab = tk.Frame(self.notebook, bg=BG)
        self.proc_tab = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(self.jobs_tab)
        self.notebook.add(self.proc_tab)
        self.build_jobs()
        self.build_processes()
        self.root.bind('<Control-n>', lambda e: self.add_dialog())
        self.refresh()
        self.navigate('jobs')
        self.root.after(100, lambda: dark_titlebar(root))
        if manager.errors:
            messagebox.showwarning('部分任务无法读取', '\n'.join(manager.errors), parent=root)

    def navigate(self, page):
        jobs = page == 'jobs'
        self.notebook.select(self.jobs_tab if jobs else self.proc_tab)
        self.jobs_nav.configure(fg_color='#30283f' if jobs else 'transparent', text_color=ACCENT if jobs else MUTED)
        self.proc_nav.configure(fg_color='transparent' if jobs else '#30283f', text_color=MUTED if jobs else ACCENT)
        self.page_title.configure(text='渲染工作台' if jobs else '本机进程')
        self.subtitle.configure(text='让每一帧都有进度。' if jobs else '查看这台电脑上正在运行的 Blender。')

    def guard(self, fn):
        try:
            fn()
        except Exception as e:
            messagebox.showerror('操作未完成', str(e), parent=self.root)

    def selected(self):
        selected = self.tree.selection()
        if not selected:
            raise ValueError('请先选择一个渲染任务。')
        return selected[0]

    def action(self, name):
        def run():
            jid = self.selected()
            self.submit(lambda manager: getattr(manager,name)(jid),selected=jid)
        self.guard(run)

    def submit(self, operation, callback=None, selected=None):
        if self.manager.operations:
            self.notice.configure(text='正在完成上一个操作，界面仍可浏览。')
            return
        self.manager.submit(operation,callback,selected or (self.tree.selection() or [None])[0])
        self.notice.configure(text='正在后台处理…')
        self.update_detail()

    @staticmethod
    def configure_changed(widget, **values):
        changes = {key:value for key,value in values.items() if widget.cget(key) != value}
        if changes: widget.configure(**changes)

    def build_jobs(self):
        toolbar = tk.Frame(self.jobs_tab, bg=BG)
        toolbar.pack(fill='x', pady=(0, 12))
        self.start_button = button(toolbar, text='▶  开始 / 继续', command=lambda: self.action('start'), width=132)
        self.start_button.pack(side='left', padx=(0, 8))
        self.pause_button = button(toolbar, text='Ⅱ  帧后暂停', command=lambda: self.action('pause'), width=120)
        self.pause_button.pack(side='left', padx=(0, 8))
        button(toolbar, text='定时计划', command=lambda: self.guard(self.schedule_dialog), width=100).pack(side='left', padx=(0, 8))
        button(toolbar, text='打开输出', style='Quiet', command=lambda: self.guard(lambda: open_path(self.manager.jobs[self.selected()]['output'])), width=100).pack(side='left')
        button(toolbar, text='暂停全部', style='Quiet', command=self.pause_all, width=94).pack(side='right')
        split = tk.PanedWindow(self.jobs_tab, orient='vertical', bg=BG, sashwidth=12, sashrelief='flat', borderwidth=0, showhandle=False)
        split.pack(fill='both', expand=True)
        table_card = card(split)
        split.add(table_card, minsize=180, stretch='always')
        table = tk.Frame(table_card, bg=PANEL)
        table.pack(fill='both', expand=True, padx=10, pady=10)
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(table, columns=('name', 'state', 'frames', 'elapsed', 'cpu', 'ram', 'schedule'), show='headings', height=4, selectmode='browse')
        for col, title, width in [('name', '   工程名称', 180), ('state', '状态', 165), ('frames', '帧进度', 130), ('elapsed', '本次运行', 95), ('cpu', 'CPU', 65), ('ram', '内存', 90), ('schedule', '下一次计划', 165)]:
            self.tree.heading(col, text=title, anchor='w')
            self.tree.column(col, width=width, minwidth=60, anchor='w')
        self.tree.grid(row=0, column=0, sticky='nsew')
        self.tree.tag_configure('even', background=PANEL)
        self.tree.tag_configure('odd', background='#1e1e26')
        scroll = AutoScrollbar(table, command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        self.tree.configure(yscrollcommand=scroll.set)
        table_x = AutoScrollbar(table, orient='horizontal', command=self.tree.xview)
        table_x.grid(row=1,column=0,sticky='ew')
        self.tree.configure(xscrollcommand=table_x.set)
        self.tree.bind('<<TreeviewSelect>>', lambda e: self.update_detail())
        self.empty = tk.Frame(table, bg=PANEL)
        label(self.empty, '◇', 32, '#9280c4').pack(pady=(0, 4))
        label(self.empty, '从第一个渲染任务开始', 14, bold=True).pack()
        label(self.empty, '添加 .blend 文件，安排时间，随时暂停与继续。', 10, MUTED).pack(pady=8)
        button(self.empty, text='＋  添加工程', style='Accent.TButton', command=self.add_dialog, width=136).pack(pady=10)
        self.empty.place(relx=.5, rely=.5, anchor='center')
        log_card = card(split)
        split.add(log_card, minsize=250, stretch='always')
        top = tk.Frame(log_card, bg=PANEL)
        top.pack(fill='x', padx=20, pady=(15, 7))
        self.detail = label(top, '任务详情', 11, bold=True, anchor='w', justify='left')
        self.detail.pack(side='left', fill='x', expand=True)
        self.percent = label(top, '—', 11, ACCENT, bold=True)
        self.percent.pack(side='right')
        self.detail_meta = label(log_card, '选择一个任务，查看进度和实时日志。', 9, MUTED, anchor='w')
        self.detail_meta.pack(fill='x', padx=20, pady=(0, 10))
        self.progress = progress_bar(log_card)
        self.progress.pack(fill='x', padx=20, pady=(0, 10))
        bar = tk.Frame(log_card, bg=PANEL)
        bar.pack(fill='x', padx=20)
        label(bar, '运行日志', 9, '#c4c1d2', bold=True).pack(side='left')
        label(bar, ' /  LIVE OUTPUT', 8, '#696779').pack(side='left')
        self.follow = tk.BooleanVar(value=True)
        checkbox(bar, text='跟随日志', variable=self.follow, width=96).pack(side='left', padx=20)
        button(bar, text='完整日志 ↗', style='Quiet', command=lambda: self.guard(lambda: open_path(self.manager.directory(self.selected()) / 'render.log')), width=104).pack(side='right')
        logs = tk.Frame(log_card, bg='#15151b')
        logs.pack(fill='both', expand=True, padx=12, pady=(8, 12))
        logs.rowconfigure(0, weight=1)
        logs.columnconfigure(0, weight=1)
        self.log_text = tk.Text(logs, height=7, bg='#15151b', fg='#aaa9bb', insertbackground=INK,
                                font=('Consolas', 10), wrap='none', relief='flat', borderwidth=0,
                                highlightthickness=0, padx=12, pady=10, state='disabled')
        self.log_text.grid(row=0, column=0, sticky='nsew')
        self.log_text.tag_configure('event', foreground='#b9a3ff')
        self.log_text.tag_configure('error', foreground='#ed939d')
        self.log_text.tag_configure('saved', foreground='#81cbb0')
        scrollbar = AutoScrollbar(logs, command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky='ns')
        self.log_text.configure(yscrollcommand=scrollbar.set)
        xbar = AutoScrollbar(logs, orient='horizontal', command=self.log_text.xview)
        xbar.grid(row=1, column=0, sticky='ew')
        self.log_text.configure(xscrollcommand=xbar.set)

    def build_processes(self):
        bar = tk.Frame(self.proc_tab, bg=BG)
        bar.pack(fill='x', pady=(0, 14))
        label(bar, '进程概览', 12, bold=True).pack(side='left')
        button(bar, text='刷新列表', command=self.scan, width=108).pack(side='right')
        shell = card(self.proc_tab)
        shell.pack(fill='both', expand=True)
        inner = tk.Frame(shell, bg=PANEL)
        inner.pack(fill='both', expand=True, padx=12, pady=12)
        inner.rowconfigure(0, weight=1)
        inner.columnconfigure(0, weight=1)
        self.proc_tree = ttk.Treeview(inner, columns=('pid', 'kind', 'file', 'progress', 'age', 'cpu', 'ram'), show='headings', height=6)
        for col, title, width in [('pid', 'PID', 70), ('kind', '控制方式', 115), ('file', '工程 / 进程', 220), ('progress', '帧进度', 120), ('age', '已运行', 95), ('cpu', 'CPU', 65), ('ram', '内存', 90)]:
            self.proc_tree.heading(col, text=title, anchor='w')
            self.proc_tree.column(col, width=width, minwidth=55)
        self.proc_tree.grid(row=0, column=0, sticky='nsew')
        scroll = AutoScrollbar(inner, command=self.proc_tree.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        self.proc_tree.configure(yscrollcommand=scroll.set)
        proc_x = AutoScrollbar(inner,orient='horizontal',command=self.proc_tree.xview)
        proc_x.grid(row=1,column=0,sticky='ew')
        self.proc_tree.configure(xscrollcommand=proc_x.set)
        self.proc_tree.bind('<<TreeviewSelect>>', lambda e: self.process_detail())
        label(self.proc_tab, '进程命令', 10, bold=True, anchor='w').pack(fill='x', pady=(18, 8))
        self.command = tk.Text(self.proc_tab, height=4, bg=PANEL, fg=MUTED, font=('Consolas', 10), wrap='word',
                               relief='flat', borderwidth=0, highlightthickness=0, padx=16, pady=14, state='disabled')
        self.command.pack(fill='x')
        controls = tk.Frame(self.proc_tab, bg=BG)
        controls.pack(fill='x', pady=14)
        button(controls, text='接入选中进程', style='Accent.TButton', command=lambda: self.guard(self.attach_dialog), width=145).pack(side='left')
        button(controls, text='管理此任务', style='Quiet', command=lambda: self.guard(self.goto_managed), width=115).pack(side='left', padx=8)
        button(controls, text='从工程新建', style='Quiet', command=lambda: self.guard(self.import_process), width=115).pack(side='left')
        label(self.proc_tab, 'CPU 按整机算力计。外部脚本接入后显示真实 PNG 帧进度；未识别的进度不作估算。', 9, MUTED, anchor='w').pack(fill='x', pady=(0, 8))

    def attach_dialog(self, row=None, info=None):
        import external
        if row is None:
            sel=self.proc_tree.selection()
            if not sel: raise ValueError('请先选择要接入的 Blender 进程。')
            row=self.processes[int(sel[0])]
            for jid in self.manager.jobs:
                if self.manager.status(jid).get('pid')==row['pid']:
                    self.goto_managed();return
            self.submit(lambda manager:external.suggest(row),lambda result:self.attach_dialog(row,result))
            return
        win=self.dialog('接入已有后台渲染',760)
        wrap=tk.Frame(win,bg=BG);wrap.pack(fill='both',expand=True,padx=24,pady=22)
        label(wrap,'接入正在运行的 Blender',18,bold=True).pack(anchor='w')
        label(wrap,f"PID {row['pid']}  ·  {Path(row['blend']).name}",10,MUTED).pack(anchor='w',pady=12)
        output=tk.StringVar(value=info['output']);pattern=tk.StringVar(value=info['pattern'])
        sv=tk.StringVar(value=str(info['start']));ev=tk.StringVar(value=str(info['end']));step=tk.StringVar(value=str(info['step']))
        label(wrap,'确认此进程的 PNG 输出目录',10).pack(anchor='w')
        line=tk.Frame(wrap,bg=BG);line.pack(fill='x',pady=8)
        entry(line,textvariable=output).pack(side='left',fill='x',expand=True)
        def browse():
            value=filedialog.askdirectory(parent=win)
            if value: output.set(value)
        button(line,text='浏览',command=browse).pack(side='right',padx=(8,0))
        label(wrap,'文件名规则（# 代表帧号，例如 ####.png）',10).pack(anchor='w',pady=(8,6))
        entry(wrap,textvariable=pattern,width=40).pack(anchor='w')
        ranges=tk.Frame(wrap,bg=BG);ranges.pack(fill='x',pady=16)
        for title,var in [('起始帧',sv),('结束帧',ev),('步长',step)]:
            label(ranges,title,10).pack(side='left',padx=(0,6))
            entry(ranges,textvariable=var,width=8).pack(side='left',padx=(0,14))
        label(wrap,'首次暂停：检测到新帧完整保存后结束旧进程。\n检测有短暂延迟，可能丢弃刚开始的下一帧；不会丢弃已完整保存的帧。\n继续：保留原脚本和参数，跳过已完成 PNG；之后支持协作式按帧暂停。\n原进程的 stdout 无法补接，接入期间记录图片保存事件；继续后收集完整日志。\n请确认帧范围、规则和输出目录只对应此进程。',10,MUTED,justify='left').pack(anchor='w',pady=12)
        accepted=tk.BooleanVar(value=False)
        checkbox(wrap,text='已确认输出设置，并理解首次暂停会结束旧进程',variable=accepted).pack(anchor='w',pady=8)
        def apply():
            try:
                if not accepted.get(): raise ValueError('请先确认上方的接入方式和输出设置。')
                args=(row,output.get(),int(sv.get()),int(ev.get()),int(step.get()),pattern.get())
                def attached(job):
                    self.tree.selection_set(job['id']);self.navigate('jobs')
                    if win.winfo_exists(): win.destroy()
                    self.scan()
                self.submit(lambda manager:external.attach(manager,*args),attached)
            except Exception as e: messagebox.showerror('无法接入',str(e),parent=win)
        button(wrap,text='接入并管理',style='Accent.TButton',command=apply,width=140).pack(side='bottom',anchor='e')

    def process_detail(self):
        sel = self.proc_tree.selection()
        row = self.processes.get(int(sel[0])) if sel else None
        text = subprocess.list2cmdline(row['args']) if row else ''
        self.command.configure(state='normal')
        self.command.delete('1.0', 'end')
        self.command.insert('1.0', text)
        self.command.configure(state='disabled')

    def import_process(self):
        sel = self.proc_tree.selection()
        if not sel:
            raise ValueError('请先选择一个进程。')
        row = self.processes[int(sel[0])]
        if not row['blend']:
            raise ValueError('此进程的命令行没有 .blend 路径，请用“添加渲染任务”手动选择已保存的工程。')
        self.add_dialog(row['blend'], row['exe'])

    def goto_managed(self):
        sel = self.proc_tree.selection()
        if not sel:
            raise ValueError('请先选择一个进程。')
        pid = int(sel[0])
        for jid in self.manager.jobs:
            if self.manager.status(jid).get('pid') == pid:
                self.navigate('jobs')
                self.tree.selection_set(jid)
                self.tree.see(jid)
                return
        raise ValueError('请先点击“接入选中进程”，确认输出目录与帧范围。')

    def scan(self):
        if self.scanning:
            return
        self.scanning = True
        self.scan_time = time.time()
        def work():
            try:
                self.messages.put(('processes', scan_processes()))
            except Exception as e:
                self.messages.put(('scan_error', str(e)))
        threading.Thread(target=work, daemon=True).start()

    def render_processes(self, rows):
        self.scanning = False
        self.processes = {row['pid']: row for row in rows}
        managed = {self.manager.status(j).get('pid'): j for j in self.manager.jobs if self.manager.live(j)}
        old = set(self.proc_tree.get_children())
        for row in rows:
            pid = str(row['pid'])
            elapsed = max(0, int(time.time() - (row['created'] or time.time())))
            progress='待接入识别'
            if row['pid'] in managed:
                jid=managed[row['pid']];job=self.manager.jobs[jid];status=self.manager.status(jid)
                total=len(range(job['start'],job['end']+1,job['step']))
                progress=f"{status['done']}/{total} · {status['done']/total:.0%}"
            values = (pid, '已管理' if row['pid'] in managed else '外部 · 未接入',
                      Path(row['blend']).name if row['blend'] else '未提供工程路径',progress,duration(elapsed),
                      f"{row['cpu']:.1f}%" if 'cpu' in row else '—',memory(row['memory']))
            if pid in old:
                if self.proc_values.get(pid) != values:
                    self.proc_tree.item(pid, values=values)
                old.remove(pid)
            else:
                self.proc_tree.insert('', 'end', iid=pid, values=values)
            self.proc_values[pid] = values
        for pid in old:
            self.proc_tree.delete(pid)
            self.proc_values.pop(pid,None)

    def refresh_jobs(self):
        for index, (jid, job) in enumerate(self.manager.jobs.items()):
            s = self.manager.status(jid)
            total = len(range(job['start'], job['end'] + 1, job['step']))
            upcoming = []
            if job['start_at']:
                upcoming.append((job['start_at'], '开始'))
            if job['pause_at']:
                upcoming.append((job['pause_at'], '暂停'))
            schedule = f'{min(upcoming)[1]} {timestamp(min(upcoming)[0])}' if upcoming else '—'
            values = ('   ' + job['name'], '●  ' + LABELS.get(s['state'], s['state']), f"{s['done']}/{total} · {s['done']/total:.0%}",duration(s.get('elapsed')),f"{s['cpu']:.1f}%" if 'cpu' in s else '—',memory(s.get('ram_mb')),schedule)
            if self.tree.exists(jid):
                if self.row_values.get(jid) != values:
                    self.tree.item(jid, values=values)
            else:
                self.tree.insert('', 'end', iid=jid, values=values, tags=('odd' if index % 2 else 'even',))
            self.row_values[jid] = values
        running = sum(self.manager.live(j) for j in self.manager.jobs)
        for widget, number in zip(self.stat_values, (len(self.manager.jobs), running, len(self.processes))):
            self.configure_changed(widget,text=f'{number:02d}')
        if self.manager.jobs:
            self.tree.configure(show='headings')
            self.empty.place_forget()
            if not self.tree.selection():
                self.tree.selection_set(next(iter(self.manager.jobs)))
        else:
            self.tree.configure(show='')
        self.update_detail()

    def update_detail(self):
        if not self.tree.selection():
            self.configure_changed(self.start_button,state='disabled')
            self.configure_changed(self.pause_button,state='disabled')
            return
        jid = self.selected()
        j, s = self.manager.jobs[jid], self.manager.status(jid)
        ready = jid in self.manager.states and not self.manager.operations
        self.configure_changed(self.start_button,state='normal' if ready and not s['running'] else 'disabled')
        self.configure_changed(self.pause_button,state='normal' if ready and s['running'] and s['state'] not in ('pausing', 'finishing', 'watching') else 'disabled')
        self.configure_changed(self.detail,text=j['name'][:65])
        self.configure_changed(self.detail_meta,text=f"{j['start']}–{j['end']} 帧 · {j['format']}   |   当前帧 {s.get('frame','—')}   |   运行 {duration(s.get('elapsed'))}   |   CPU {s.get('cpu',0):.1f}%   |   内存 {memory(s.get('ram_mb'))}")
        percent = s['done'] * 100 / len(range(j['start'], j['end'] + 1, j['step']))
        if self.progress.get() != percent/100: self.progress.set(percent / 100)
        self.configure_changed(self.percent,text=f'{percent:.0f}%')
        chunk = self.manager.take_log(jid)
        changed = jid != self.last_log
        if changed or chunk:
            self.log_text.configure(state='normal')
            if changed or chunk and chunk['reset']:
                self.log_text.delete('1.0', 'end')
            # Batch tagged runs into a single Tcl call instead of one call per line.
            segments = []
            for line in (chunk['text'] if chunk else '').splitlines(keepends=True):
                tag = 'error' if any(x in line.lower() for x in ('error', 'traceback', 'failed')) else ('saved' if 'FRAME_SAVED' in line or 'Saved:' in line else ('event' if '[Render Desk]' in line else ''))
                if segments and segments[-1] == (tag,):
                    segments[-2] += line
                else:
                    segments.extend((line,(tag,)))
            if segments: self.log_text.insert('end',*segments)
            lines = int(self.log_text.index('end-1c').split('.')[0])
            if lines > 1200: self.log_text.delete('1.0',f'{lines-1199}.0')
            chars = (self.log_text.count('1.0','end-1c','chars') or (0,))[0]
            if chars > 100_000: self.log_text.delete('1.0',f'1.0+{chars-100_000}c')
            self.log_text.configure(state='disabled')
            if self.follow.get():
                self.log_text.see('end')
            self.last_log = jid

    def refresh(self):
        try:
            while not self.messages.empty():
                kind, payload = self.messages.get_nowait()
                if kind == 'processes':
                    self.render_processes(payload)
                elif kind == 'scan_error':
                    self.scanning = False
                    self.notice.configure(text='进程扫描失败：' + payload)
                elif kind == 'callback':
                    payload()
                elif kind == 'backend_snapshot':
                    self.manager.refresh_pending = False
                    self.manager.apply(payload)
                    self.refresh_jobs()
                    if payload['notices']: self.notice.configure(text=payload['notices'][-1][:105])
                elif kind == 'backend_error':
                    self.manager.refresh_pending = False
                    self.notice.configure(text=('监测失败：'+payload)[:105])
                elif kind == 'backend_operation':
                    self.manager.operations -= 1
                    snapshot,result,error,callback = payload
                    if snapshot is not None: self.manager.apply(snapshot)
                    self.refresh_jobs()
                    if error:
                        self.notice.configure(text='操作未完成：'+error[:90])
                        messagebox.showerror('操作未完成',error,parent=self.root)
                    else:
                        self.notice.configure(text='操作已完成。')
                        if callback: callback(result)
            if time.time() - self.scan_time > 2:
                self.scan()
            if time.monotonic()-self.poll_time >= .7:
                self.poll_time = time.monotonic()
                self.manager.request_refresh((self.tree.selection() or [None])[0])
        except Exception as e:
            self.notice.configure(text=('刷新失败：' + str(e))[:110])
        self.root.after(80, self.refresh)

    def dialog(self, title, width=720):
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg=BG)
        win.transient(self.root)
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - width) // 2)
        y = self.root.winfo_rooty() + 55
        win.geometry(f'{width}x610+{x}+{y}')
        win.resizable(False, False)
        win.grab_set()
        win.lift()
        win.after(100, lambda: dark_titlebar(win))
        return win

    def add_dialog(self, blend='', blender=''):
        win = self.dialog('添加渲染任务')
        wrap = tk.Frame(win, bg=BG)
        wrap.pack(fill='both', expand=True, padx=24, pady=20)
        label(wrap, '新建后台渲染', 18, bold=True).pack(anchor='w')
        label(wrap, '直接控制 Blender · 暂停时保存当前帧并退出进程', 10, MUTED).pack(anchor='w', pady=(4, 18))
        form = tk.Frame(wrap, bg=BG)
        form.pack(fill='x')
        form.columnconfigure(1, weight=1)
        found = detect_blender()
        bv = tk.StringVar(value=blend)
        ev = tk.StringVar(value=blender or (found[0] if found else ''))
        ov = tk.StringVar(value=str(Path(blend).parent / 'RenderDesk_Output') if blend else str(self.manager.root / 'renders'))
        sv, endv, stepv = tk.StringVar(value='1'), tk.StringVar(value='250'), tk.StringVar(value='1')
        tv, fv = tk.StringVar(value='0'), tk.StringVar(value='PNG')
        av = tk.BooleanVar(value=False)
        def browse(var, kind):
            value = filedialog.askdirectory(parent=win) if kind == 'dir' else filedialog.askopenfilename(parent=win, filetypes=[('Blender 文件', '*.blend')] if kind == 'blend' else [('Blender', '*.exe')])
            if value:
                var.set(value)
                if kind == 'blend':
                    ov.set(str(Path(value).parent / 'RenderDesk_Output'))
        for i, (title, variable, kind) in enumerate([('工程文件', bv, 'blend'), ('Blender 程序', ev, 'exe'), ('输出父目录', ov, 'dir')]):
            label(form, title, 10).grid(row=i, column=0, sticky='w', padx=(0, 12), pady=8)
            entry(form, textvariable=variable).grid(row=i, column=1, sticky='ew', pady=8)
            button(form, text='浏览', command=lambda v=variable, k=kind: browse(v, k)).grid(row=i, column=2, padx=(8, 0))
        rangebox = tk.Frame(wrap, bg=BG)
        rangebox.pack(fill='x', pady=12)
        for title, variable in [('起始帧', sv), ('结束帧', endv), ('步长', stepv)]:
            label(rangebox, title).pack(side='left', padx=(0, 6))
            entry(rangebox, textvariable=variable, width=7).pack(side='left', padx=(0, 14))
        hint = label(wrap, '请确认帧范围；可读取工程中保存的范围。', 9, MUTED, anchor='w')
        def probe():
            if not Path(bv.get()).is_file() or not Path(ev.get()).is_file():
                messagebox.showerror('无法读取', '请先选择工程和 Blender。', parent=win)
                return
            hint.configure(text='正在读取工程范围…')
            probe_button.configure(state='disabled')
            args = [ev.get(), '-b', '--disable-autoexec', bv.get(), '-t', '1', '--python-exit-code', '23', '--python-expr',
                    "import bpy,json; s=bpy.context.scene; print('RENDERDESK_META='+json.dumps([s.frame_start,s.frame_end,s.frame_step]))"]
            def run_probe():
                try:
                    r = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    lines = r.stdout.decode('utf-8', errors='replace').splitlines()
                    line = next(x for x in lines if x.startswith('RENDERDESK_META='))
                    values = json.loads(line.split('=', 1)[1])
                    def done():
                        if win.winfo_exists():
                            for v, n in zip((sv, endv, stepv), values):
                                v.set(str(n))
                            hint.configure(text='已读取工程帧范围。')
                            probe_button.configure(state='normal')
                except Exception as e:
                    msg = str(e) or '无法读取工程，检查路径或手动填写范围。'
                    def done(msg=msg):
                        if win.winfo_exists():
                            hint.configure(text='读取失败：' + msg[:65])
                            probe_button.configure(state='normal')
                self.messages.put(('callback', done))
            threading.Thread(target=run_probe, daemon=True).start()
        probe_button = button(rangebox, text='读取工程范围', command=probe)
        probe_button.pack(side='left')
        hint.pack(fill='x')
        opts = tk.Frame(wrap, bg=BG)
        opts.pack(fill='x', pady=15)
        label(opts, '序列格式').pack(side='left', padx=(0, 8))
        combo(opts, textvariable=fv, values=['PNG', 'OPEN_EXR'], width=12, state='readonly').pack(side='left', padx=(0, 20))
        label(opts, 'CPU 线程数').pack(side='left', padx=(0, 8))
        entry(opts, textvariable=tv, width=6).pack(side='left')
        label(opts, '0 = Blender 默认', 9, MUTED).pack(side='left', padx=10)
        checkbox(wrap, text='允许工程内 Python 脚本（仅用于你信任的工程 / 脚本驱动绑定）', variable=av).pack(anchor='w')
        label(wrap, '使用工程保存的场景、相机、引擎和分辨率。每个任务自动创建独立输出子目录。\n继续渲染会重新载入该 .blend；任务期间请勿修改工程和依赖资源。\n从外部进程导入只读取工程路径，不会接管旧进程或复制其自定义脚本参数。', 9, MUTED, justify='left', anchor='w').pack(fill='x', pady=14)
        def create():
            try:
                if not ov.get().strip():
                    raise ValueError('请选择输出父目录。')
                args = (bv.get(), ev.get(), int(sv.get()), int(endv.get()), int(stepv.get()), ov.get(), fv.get(), int(tv.get()), av.get())
                def created(job):
                    self.tree.selection_set(job['id'])
                    self.tree.see(job['id'])
                    self.navigate('jobs')
                    if win.winfo_exists(): win.destroy()
                self.submit(lambda manager:manager.add(*args),created)
            except Exception as e:
                messagebox.showerror('无法创建任务', str(e), parent=win)
        button(wrap, text='创建任务', style='Accent.TButton', command=create).pack(side='bottom', anchor='e', pady=6)

    def schedule_dialog(self):
        jid = self.selected()
        j = self.manager.jobs[jid]
        win = self.dialog('设置定时 · ' + j['name'], 640)
        win.geometry('640x460')
        wrap = tk.Frame(win, bg=BG)
        wrap.pack(fill='both', expand=True, padx=25, pady=22)
        label(wrap, '安排渲染时间', 18, bold=True).pack(anchor='w')
        label(wrap, '到暂停时间后，保存当前帧，再退出 Blender。', 10, MUTED).pack(anchor='w', pady=(4, 18))
        mode = tk.StringVar(value='多久之后（分钟）')
        combo(wrap, textvariable=mode, values=['多久之后（分钟）', '指定本地时间'], state='readonly', width=24).pack(anchor='w')
        startv, pausev = tk.StringVar(), tk.StringVar()
        for title, var in [('开始 / 继续', startv), ('暂停并退出', pausev)]:
            row = tk.Frame(wrap, bg=BG)
            row.pack(fill='x', pady=10)
            label(row, title, width=13, anchor='w').pack(side='left')
            entry(row, textvariable=var, width=32).pack(side='left')
        label(wrap, '留空 = 不安排该动作。分钟从保存计划时算起，可填小数。\n指定时间格式：2026-09-22 23:30 或 2026-09-22 23:30:00。\n定时需要管理器保持运行，且电脑处于唤醒状态。', 9, MUTED, justify='left').pack(anchor='w', pady=8)
        label(wrap, f"现有计划：开始 {timestamp(j['start_at'])}  /  暂停 {timestamp(j['pause_at'])}", 9, ACCENT).pack(anchor='w', pady=8)
        def save(clear=False):
            try:
                now = time.time()
                def parse(value):
                    value = value.strip()
                    if not value:
                        return None
                    if mode.get().startswith('多久'):
                        n = float(value)
                        if not math.isfinite(n) or n <= 0:
                            raise ValueError('分钟数必须是大于 0 的有限数字。')
                        return now + n * 60
                    return dt.datetime.fromisoformat(value).timestamp()
                start_at = None if clear else parse(startv.get())
                pause_at = None if clear else parse(pausev.get())
                def saved(_):
                    self.notice.configure(text='定时计划已取消。' if clear else '定时计划已保存；请保持管理器运行。')
                    if win.winfo_exists(): win.destroy()
                self.submit(lambda manager:manager.schedule(jid,start_at,pause_at),saved,jid)
            except Exception as e:
                messagebox.showerror('无法设置定时', str(e), parent=win)
        buttons = tk.Frame(wrap, bg=BG)
        buttons.pack(side='bottom', fill='x')
        button(buttons, text='取消已有计划', command=lambda: save(True)).pack(side='left')
        button(buttons, text='保存计划', style='Accent.TButton', command=save).pack(side='right')

    def pause_all(self):
        def run(manager):
            for jid in manager.jobs:
                if manager.live(jid):
                    manager.pause(jid)
        self.submit(run,lambda _:self.notice.configure(text='已请求所有托管任务完成当前帧后暂停。'))

    def help_dialog(self):
        messagebox.showinfo('使用说明',
            '1. 添加已保存的 .blend 文件，确认帧范围，创建任务。\n'
            '2. 选择任务 → 开始 / 继续。Blender 在后台运行。\n'
            '3. 暂停会等当前帧渲染并保存，再退出该后台进程。\n'
            '4. 继续会重新启动 Blender，仅渲染剩余帧。\n'
            '5. 定时可设置延迟分钟数或指定本地时间。\n\n'
            '任务、进度与日志自动保存，可重新打开管理器继续。\n'
            '程序关闭后，已启动的渲染继续运行，但定时不会触发。\n'
            '重新打开时，过期的开始计划会补执行；若暂停时间也已过，则跳过该窗口。\n\n'
            '本机进程页可接入单个 Python 脚本逐帧输出 PNG 的后台进程。\n'
            '首次暂停在检测新帧完整落盘后结束旧进程，可能损失下一帧计算。\n'
            '继续后使用协作式帧暂停；旧进程日志仅记录保存事件。\n'
            '工程主输出为 PNG / EXR 图片序列，不直接输出视频。\n'
            '合成器 File Output 节点保留工程原路径。物理模拟需预先烘焙。\n'
            'GPU/CPU 使用工程及 Blender 已保存的渲染设置。\n\n'
            f'数据目录：{self.manager.root}', parent=self.root)

    def close(self):
        active = any(self.manager.live(j) for j in self.manager.jobs)
        scheduled = any(j['start_at'] or j['pause_at'] for j in self.manager.jobs.values())
        if active or scheduled or self.manager.operations:
            if not messagebox.askokcancel('关闭管理器', '关闭后已启动的 Blender 会继续渲染，但定时暂停 / 开始不会触发。\n\n如需暂停，请先取消关闭并点击“暂停全部托管任务”，等进程退出。\n\n仍要关闭管理器吗？', parent=self.root):
                return
        self.manager.close()
        self.root.destroy()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir')
    args = parser.parse_args()
    if os.name == 'nt':
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    data = Path(args.data_dir) if args.data_dir else default_data_dir()
    data.mkdir(parents=True, exist_ok=True)
    lock = (data / 'manager.lock').open('a+b')
    lock.seek(0)
    if os.name == 'nt':
        import msvcrt
        try:
            if (data / 'manager.lock').stat().st_size == 0:
                lock.write(b'0')
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            root.withdraw()
            messagebox.showinfo('Blender Render Desk', '该数据目录的管理器已经打开，请使用现有窗口。')
            root.destroy()
            return
    try:
        app = App(root, Manager(data))
        root.mainloop()
    finally:
        if 'app' in locals():
            app.manager.close()
            app.manager.executor.shutdown(wait=True,cancel_futures=True)
        lock.close()

if __name__ == '__main__':
    main()
