"""Local desktop presentation. Opening this UI never starts a collection job."""
import json
import os
import subprocess
import sys
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox


COLORS = {
    'bg': '#ffffff', 'sidebar': '#f7f7f8', 'surface': '#f9f9f9',
    'line': '#e7e7e9', 'text': '#202123', 'muted': '#727278',
    'green': '#16866b', 'green_bg': '#edf7f2', 'red': '#bb4545',
    'red_bg': '#fceeee', 'amber': '#936823', 'amber_bg': '#fbf4e5',
}
FONT = 'Microsoft YaHei UI'


def read_json(path):
    try:
        value = Path(path).read_text(encoding='utf-8-sig')
        result = json.loads(value)
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        # A scheduled process may be replacing its status file right now.
        return {}


def time_text(value, short=False):
    if not value:
        return '尚无记录'
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.strftime('%m月%d日 %H:%M' if short else '%Y-%m-%d  %H:%M:%S')
    except (ValueError, TypeError):
        return str(value)


def number(value):
    return f'{value:,}' if isinstance(value, int) and not isinstance(value, bool) else '—'


def modified_at(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return 0


def view_model(latest, success, busy=False):
    """Keep unknown, zero writes, and last successful data distinct."""
    status = latest.get('status', '')
    running = busy or status == '运行中'
    if running:
        title, badge, tone = '正在更新数据', '同步中', 'green'
        description = latest.get('stage') or '正在连接百应，请稍候。'
    elif status == '需要重新登录':
        title, badge, tone = '需要重新登录百应', '待处理', 'amber'
        description = '登录配置中的机构后关闭登录窗口，再点击「立即更新」。'
    elif status == '失败':
        title, badge, tone = '这次更新未完成', '需关注', 'red'
        description = latest.get('error') or '请打开日志查看原因，上次成功数据仍保留。'
    elif status == '部分成功':
        title, badge, tone = '数据已登记，仍有待处理项', '部分完成', 'amber'
        description = latest.get('product_catalog', {}).get('error') or latest.get('coverage_note') or '打开日志查看待处理项。'
    elif status == '成功':
        title, badge, tone = '数据已更新', '已完成', 'green'
        description = '百应订单已登记至飞书，数据核对完成。'
    else:
        title, badge, tone = '准备好更新数据', '就绪', 'green'
        description = '点击「立即更新」，或等待下一次自动任务。'
    data = latest if status == '成功' or latest.get('orders_status') == 'success' else success
    counts = latest.get('sync_stats') or {}
    data_counts = data.get('sync_stats') or {}
    confirmed = data_counts.get('confirmed_rows')
    if confirmed is None and not data_counts:
        confirmed = data.get('written_rows')  # Legacy full-write reports.
    covered = data.get('covered_accounts')
    covered_count = len(covered) if isinstance(covered, list) else data.get('scope_enabled_accounts')
    return {
        'title': title, 'badge': badge, 'tone': tone, 'description': description,
        'running': running, 'accounts': number(covered_count),
        'orders': number(data.get('selected_orders')), 'summaries': number(confirmed),
        'as_of': time_text(data.get('source_as_of')), 'last_run': time_text(latest.get('started_at')),
        'delta': '  /  '.join(number(counts.get(k)) for k in ('created_rows', 'updated_rows', 'skipped_rows')),
        'range': ' 至 '.join(time_text(v, short=True) for v in data.get('source_range', [])[:2]) or '尚无记录',
        'source': data.get('source_file') or '尚无记录',
    }


def rounded(canvas, x1, y1, x2, y2, radius=16, **options):
    radius = min(radius, (x2-x1)/2, (y2-y1)/2)
    return canvas.create_polygon(
        x1+radius, y1, x2-radius, y1, x2, y1, x2, y1+radius,
        x2, y2-radius, x2, y2, x2-radius, y2, x1+radius, y2,
        x1, y2, x1, y2-radius, x1, y1+radius, x1, y1,
        smooth=True, splinesteps=28, **options)


class Panel(tk.Canvas):
    def __init__(self, parent, color=None, padding=22, **kw):
        self.color = color or COLORS['surface']
        self.padding = padding
        super().__init__(parent, bg=parent.cget('bg'), highlightthickness=0, **kw)
        self.inner = tk.Frame(self, bg=self.color)
        self.body = self.create_window(padding, padding, window=self.inner, anchor='nw')
        self.bind('<Configure>', self.paint)

    def paint(self, event=None):
        w, h = self.winfo_width(), self.winfo_height()
        self.delete('shell')
        rounded(self, 1, 1, w-1, h-1, fill=self.color, outline=COLORS['line'], tags='shell')
        self.tag_lower('shell')
        self.itemconfigure(self.body, width=max(1, w-2*self.padding), height=max(1, h-2*self.padding))


class PillButton(tk.Canvas):
    def __init__(self, parent, text, command, primary=False, width=128, height=42):
        super().__init__(parent, width=width, height=height, bg=parent.cget('bg'),
                         highlightthickness=0, cursor='hand2', takefocus=True)
        self.text, self.command, self.primary = text, command, primary
        self.enabled, self.hover = True, False
        self.bind('<Configure>', lambda e: self.paint())
        self.bind('<Enter>', lambda e: self.set_hover(True))
        self.bind('<Leave>', lambda e: self.set_hover(False))
        self.bind('<Button-1>', self.invoke)
        self.bind('<Return>', self.invoke)
        self.bind('<space>', self.invoke)
        self.bind('<FocusIn>', lambda e: self.paint())
        self.bind('<FocusOut>', lambda e: self.paint())
        self.paint()

    def set_hover(self, value):
        self.hover = value
        self.paint()

    def set_enabled(self, value):
        self.enabled = value
        self.configure(cursor='hand2' if value else 'arrow')
        self.paint()

    def invoke(self, event=None):
        if self.enabled:
            self.focus_set()
            self.command()
        return 'break'

    def paint(self):
        self.delete('all')
        w, h = int(self.cget('width')), int(self.cget('height'))
        bg = '#202123' if self.primary else '#ffffff'
        fg = '#ffffff' if self.primary else COLORS['text']
        if self.hover and self.enabled:
            bg = '#3a3a3e' if self.primary else '#ededee'
        if not self.enabled:
            bg, fg = '#e8e8eb', '#95959b'
        outline = '#95959b' if self.focus_get() == self else (bg if self.primary else COLORS['line'])
        rounded(self, 1, 1, w-1, h-1, radius=21, fill=bg, outline=outline)
        self.create_text(w/2, h/2, text=self.text, fill=fg, font=(FONT, 10, 'bold' if self.primary else 'normal'))


class SyncApp:
    def __init__(self, root, load_config, schedule_times, now):
        self.root, self.load_config, self.schedule_times, self.now = Path(root), load_config, schedule_times, now
        self.active = []
        self.last_action = None
        self.action_started = 0
        self.pending_report = False
        self.cached_latest, self.cached_success = {}, {}
        self.window = tk.Tk()
        self.window.title('百应出单助手 v0.1.0')
        self.app_icon = tk.PhotoImage(width=32, height=32)
        for y in range(32):
            for x in range(32):
                dx, dy = max(7-x, 0, x-24), max(7-y, 0, y-24)
                if dx*dx+dy*dy <= 49:
                    self.app_icon.put('#202123', (x, y))
        for y, end in ((9, 23), (15, 20), (21, 17)):
            self.app_icon.put('#ffffff', to=(9, y, end, y+2))
        self.window.iconphoto(True, self.app_icon)
        self.window.geometry('1080x810')
        self.window.minsize(1000, 790)
        self.window.configure(bg=COLORS['bg'])
        self.window.option_add('*Font', (FONT, 10))
        self.window.grid_rowconfigure(0, weight=1)
        self.window.grid_columnconfigure(1, weight=1)
        self.sidebar()
        self.main = tk.Frame(self.window, bg=COLORS['bg'])
        self.main.grid(row=0, column=1, sticky='nsew', padx=36, pady=(28, 18))
        self.main.grid_columnconfigure(0, weight=1)
        self.main.grid_rowconfigure(0, weight=1)
        self.pages = {}
        self.overview()
        self.history_page()
        self.help_page()
        self.show_page('工作台')
        self.refresh()

    def label(self, parent, text='', size=10, color=None, bold=False, **kw):
        return tk.Label(parent, text=text, bg=parent.cget('bg'), fg=color or COLORS['text'],
                        font=(FONT, size, 'bold' if bold else 'normal'), anchor='w', **kw)

    def sidebar(self):
        side = tk.Frame(self.window, bg=COLORS['sidebar'], width=218)
        side.grid(row=0, column=0, sticky='nsew')
        side.grid_propagate(False)
        side.grid_columnconfigure(0, weight=1)
        side.grid_rowconfigure(5, weight=1)
        brand = tk.Frame(side, bg=COLORS['sidebar'])
        brand.grid(row=0, column=0, sticky='ew', padx=22, pady=(30, 35))
        icon = tk.Canvas(brand, width=34, height=34, bg=COLORS['sidebar'], highlightthickness=0)
        icon.pack(side='left', padx=(0, 10))
        rounded(icon, 1, 1, 33, 33, radius=10, fill='#202123', outline='#202123')
        icon.create_line(10, 12, 24, 12, fill='white', width=2)
        icon.create_line(10, 18, 21, 18, fill='white', width=2)
        icon.create_line(10, 24, 17, 24, fill='white', width=2)
        self.label(brand, '百应同步', size=14, bold=True).pack(side='left')
        self.label(side, '工作空间', size=9, color=COLORS['muted']).grid(row=1, column=0, sticky='w', padx=26, pady=(0, 10))
        self.nav = {}
        for row, (name, glyph) in enumerate((('工作台', '◫'), ('同步记录', '↺'), ('数据说明', 'ⓘ')), 2):
            button = tk.Button(side, text=f'{glyph}   {name}', command=lambda n=name: self.show_page(n),
                               bg=COLORS['sidebar'], fg=COLORS['text'], activebackground='#e9e9ec',
                               relief='flat', bd=0, anchor='w', cursor='hand2', padx=16, pady=12)
            button.grid(row=row, column=0, sticky='ew', padx=12, pady=2)
            self.nav[name] = button
        schedule = tk.Frame(side, bg=COLORS['sidebar'])
        schedule.grid(row=6, column=0, sticky='ew', padx=26, pady=(10, 20))
        self.label(schedule, '计划时间（需安装任务）', size=9, color=COLORS['muted']).pack(anchor='w', pady=(0, 6))
        self.schedule_label = self.label(schedule, '09:00  /  18:00', size=12, bold=True)
        self.schedule_label.pack(anchor='w')
        self.label(schedule, '每天 · 北京时间', size=9, color=COLORS['muted']).pack(anchor='w', pady=(5, 0))
        tk.Frame(side, bg=COLORS['line'], height=1).grid(row=7, column=0, sticky='ew', padx=24)
        self.label(side, '百应出单助手  v0.1.0', size=9, color=COLORS['muted']).grid(row=8, column=0, sticky='w', padx=26, pady=22)

    def page(self, name, title, subtitle):
        frame = tk.Frame(self.main, bg=COLORS['bg'])
        frame.grid(row=0, column=0, sticky='nsew')
        frame.grid_columnconfigure(0, weight=1)
        self.label(frame, name, size=10, color=COLORS['muted']).grid(row=0, column=0, sticky='w', pady=(0, 10))
        self.label(frame, title, size=25, bold=True).grid(row=1, column=0, sticky='w')
        self.label(frame, subtitle, color=COLORS['muted']).grid(row=2, column=0, sticky='w', pady=(8, 16))
        self.pages[name] = frame
        return frame

    def overview(self):
        page = self.page('工作台', '出单数据，一目了然。', '从百应到飞书，让每日经营数据保持更新。')
        status = Panel(page, height=194, color='#ffffff', padding=22)
        status.grid(row=3, column=0, sticky='ew')
        status.inner.grid_columnconfigure(0, weight=1)
        self.status_title = self.label(status.inner, size=16, bold=True)
        self.status_title.grid(row=0, column=0, sticky='w')
        self.badge = self.label(status.inner, size=9, padx=10, pady=4)
        self.badge.grid(row=0, column=1, sticky='e')
        self.status_description = self.label(status.inner, color=COLORS['muted'], wraplength=570, justify='left')
        self.status_description.grid(row=1, column=0, columnspan=2, sticky='w', pady=(8, 14))
        buttons = tk.Frame(status.inner, bg='#ffffff')
        buttons.grid(row=2, column=0, columnspan=2, sticky='w')
        self.update_button = PillButton(buttons, '立即更新  ↗', lambda: self.launch('run'), primary=True, width=136)
        self.update_button.pack(side='left', padx=(0, 10))
        self.login_button = PillButton(buttons, '重新登录百应', lambda: self.launch('login'), width=138)
        self.login_button.pack(side='left')
        metrics = tk.Frame(page, bg=COLORS['bg'])
        metrics.grid(row=4, column=0, sticky='ew', pady=(16, 16))
        self.metrics = {}
        for i, (key, label, hint) in enumerate((('accounts', '覆盖账号', '本次成功覆盖'), ('orders', '订单数量', '本次成功采集'), ('summaries', '飞书汇总', '本次成功核对'))):
            metrics.grid_columnconfigure(i, weight=1, uniform='metrics')
            card = Panel(metrics, height=136, padding=18)
            card.grid(row=0, column=i, sticky='ew', padx=(0 if i == 0 else 6, 0 if i == 2 else 6))
            self.label(card.inner, label, size=9, color=COLORS['muted']).pack(anchor='w')
            value = self.label(card.inner, '—', size=23, bold=True)
            value.pack(anchor='w', pady=(4, 0))
            self.label(card.inner, hint, size=8, color=COLORS['muted']).pack(anchor='w', pady=(2, 0))
            self.metrics[key] = value
        detail = Panel(page, height=178, padding=20)
        detail.grid(row=5, column=0, sticky='ew')
        self.label(detail.inner, '最近一次同步', size=11, bold=True).grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 10))
        self.details = {}
        for row, (key, title) in enumerate((('as_of', '有效数据截至'), ('last_run', '最近执行时间'), ('delta', '新增 / 更新 / 未变化')), 1):
            self.label(detail.inner, title, size=9, color=COLORS['muted']).grid(row=row, column=0, sticky='w', pady=3, padx=(0, 26))
            value = self.label(detail.inner, '—', size=10)
            value.grid(row=row, column=1, sticky='w', pady=3)
            self.details[key] = value
        footer = tk.Frame(page, bg=COLORS['bg'])
        footer.grid(row=6, column=0, sticky='ew', pady=(12, 0))
        self.label(footer, '当日数据为截至上方时间的快照。', size=9, color=COLORS['muted']).pack(side='left')
        self.log_button = tk.Button(footer, text='打开日志  ↗', command=self.open_logs, relief='flat', bd=0,
                                    bg=COLORS['bg'], fg=COLORS['text'], cursor='hand2', font=(FONT, 9))
        self.log_button.pack(side='right')
        self.clock = self.label(page, size=8, color='#99999f')
        self.clock.grid(row=7, column=0, sticky='w', pady=(8, 0))

    def history_page(self):
        page = self.page('同步记录', '每一次更新，都有记录。', '查看最近运行结果；完整过程可在日志中查阅。')
        card = Panel(page, height=430, padding=20)
        card.grid(row=3, column=0, sticky='nsew')
        self.history_text = tk.Text(card.inner, bg=COLORS['surface'], fg=COLORS['text'], relief='flat',
                                    bd=0, wrap='word', font=(FONT, 10), padx=0, pady=0, cursor='arrow')
        scrollbar = tk.Scrollbar(card.inner, command=self.history_text.yview, width=10, relief='flat', bd=0)
        scrollbar.pack(side='right', fill='y')
        self.history_text.configure(yscrollcommand=scrollbar.set)
        self.history_text.pack(side='left', fill='both', expand=True)
        self.history_text.configure(state='disabled')
        PillButton(page, '打开日志文件夹  ↗', self.open_logs, width=176).grid(row=4, column=0, sticky='w', pady=18)

    def help_page(self):
        page = self.page('数据说明', '统一口径，清楚看数。', '使用自己的机构账号与飞书空表，首次使用请先阅读安装说明。')
        card = Panel(page, height=390, padding=24)
        card.grid(row=3, column=0, sticky='ew')
        for title, description in (
            ('实际营业额', '百应原成交金额 − 整单退款金额。平台补贴保留用于核对，不再从计佣金额中扣减。'),
            ('预估佣金', '直接汇总百应机构报表的「总佣金收入」。'),
            ('数据日期', '订单按付款日期归属；后续退款会刷新原付款日的数据。'),
            ('更新与保留', '另行安装Windows任务后按左侧时间运行，也可手动更新。失败保留上次成功数据。'),
        ):
            self.label(card.inner, title, size=11, bold=True).pack(anchor='w', pady=(0, 8))
            self.label(card.inner, description, color=COLORS['muted'], wraplength=580, justify='left').pack(anchor='w', pady=(0, 18))

    def show_page(self, name):
        self.pages[name].tkraise()
        for title, button in self.nav.items():
            button.configure(bg='#e9e9ec' if title == name else COLORS['sidebar'])
        if name == '同步记录':
            self.update_history()

    def open_logs(self):
        folder = self.root/'data/logs'
        if folder.is_dir():
            os.startfile(str(folder))
        else:
            messagebox.showinfo('暂无日志', '首次运行后会在这里保留日志。', parent=self.window)

    def update_history(self):
        files = sorted((self.root/'data/logs').glob('*.json'), key=lambda p: p.name, reverse=True)
        lines = []
        for path in files:
            report = read_json(path)
            if not report.get('started_at'):
                continue
            origin = report.get('execution_origin')
            kind = {'scheduled': '自动更新', 'manual': '手动更新', 'import': '文件导入'}.get(origin, '历史运行')
            lines.append(f"{time_text(report['started_at'])}    {report.get('status', '待核验')}\n{kind}  ·  数据截至 {time_text(report.get('source_as_of'), short=True)}")
            if len(lines) == 8:
                break
        self.history_text.configure(state='normal')
        self.history_text.delete('1.0', 'end')
        self.history_text.insert('1.0', '\n\n'.join(lines) or '还没有运行记录。首次更新后可在这里查看。')
        self.history_text.configure(state='disabled')

    def launch(self, command):
        if any(p.poll() is None for p in self.active):
            return
        base = [sys.executable] if getattr(sys, 'frozen', False) else [sys.executable, '-m', 'buyin_order_sync']
        argv = base + ['--config', str(self.load_config()['_config_path']), '--state-dir', str(self.root), command]
        if command == 'run':
            argv += ['--sync', '--origin', 'manual']
        folder = self.root/'data/logs'
        folder.mkdir(parents=True, exist_ok=True)
        self.action_started = self.now().timestamp()
        try:
            with open(folder/f'console-{self.now().strftime("%Y%m%d-%H%M%S")}.log', 'w', encoding='utf-8') as stream:
                self.active = [subprocess.Popen(argv, cwd=self.root, stdout=stream, stderr=stream,
                                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))]
        except OSError as exc:
            messagebox.showerror('未能启动', str(exc), parent=self.window)
            return
        self.last_action = command
        self.pending_report = True
        self.refresh(reschedule=False)

    def refresh(self, reschedule=True):
        latest = read_json(self.root/'data/last-run.json')
        success = read_json(self.root/'data/last-success.json')
        if latest:
            self.cached_latest = latest
        if success:
            self.cached_success = success
        latest, success = self.cached_latest, self.cached_success
        busy = any(p.poll() is None for p in self.active)
        vm = view_model(latest, success, busy)
        if busy:
            vm['title'] = '正在打开登录窗口' if self.last_action == 'login' else '正在更新数据'
            progress = read_json(self.root/'data/collection-state.json')
            if self.last_action == 'login':
                vm['description'] = '请在百应窗口完成登录，随后关闭登录窗口。'
            elif progress and modified_at(self.root/'data/collection-state.json') >= self.action_started:
                vm['description'] = progress.get('stage') or '正在准备更新，请稍候。'
            else:
                vm['description'] = '正在准备更新，请稍候。'
        elif self.last_action == 'login' and self.pending_report:
            login_path = self.root/'data/login-state.json'
            login_state = read_json(login_path)
            if login_state and modified_at(login_path) >= self.action_started:
                login_ok = login_state.get('status') == '登录成功'
                vm.update(title=login_state.get('status', '登录操作已结束'), description='点击「立即更新」获取最新数据。' if login_ok else '请打开日志查看登录结果。', badge='登录状态', tone='green' if login_ok else 'amber')
            else:
                vm.update(title='登录窗口已关闭', description='如已完成登录，可点击「立即更新」。', badge='待更新', tone='amber')
        elif self.last_action == 'run' and self.pending_report and modified_at(self.root/'data/last-run.json') < self.action_started:
            vm.update(title='尚未取得新的同步结果', description='请打开日志查看本次操作；上次成功数据仍保留。', badge='待核对', tone='amber')
        color = COLORS[vm['tone']]
        self.status_title.configure(text=vm['title'])
        description = vm['description']
        self.status_description.configure(text=description[:100] + ('… 详情见日志。' if len(description) > 100 else ''))
        self.badge.configure(text='●  '+vm['badge'], fg=color, bg=COLORS[vm['tone']+'_bg'])
        for key, value in self.metrics.items():
            value.configure(text=vm[key])
        for key, value in self.details.items():
            value.configure(text=vm[key])
        self.update_button.set_enabled(not busy and not vm['running'])
        self.login_button.set_enabled(not busy and not vm['running'])
        try:
            times = self.schedule_times(self.load_config())
            self.schedule_label.configure(text='  /  '.join(times))
        except (OSError, ValueError):
            self.schedule_label.configure(text='配置待核对')
        self.clock.configure(text='本地时间  '+self.now().strftime('%H:%M:%S'))
        if reschedule:
            self.window.after(1500, self.refresh)

    def run(self):
        self.window.mainloop()


def show_app(root, load_config, schedule_times, now):
    SyncApp(root, load_config, schedule_times, now).run()
