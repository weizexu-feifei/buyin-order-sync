"""Local run diagnostics. No credentials, browser state or remote calls belong here."""
import hashlib
import json
import re
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = '0.1.0'
TZ = timezone(timedelta(hours=8))
DEFAULT_SCHEDULE = ['09:00', '18:00']
DEFAULT_EXPORT_POLICY = {
    'resume_window_seconds': 7200,
    'cooldown_seconds': 310,
    'report_wait_seconds': 600,
    'download_event_seconds': 60,
    'download_attempts': 2,
    'save_timeout_seconds': 180,
    'worker_timeout_seconds': 1200,
}


def schedule_times(config):
    values = config.get('schedule', DEFAULT_SCHEDULE)
    if not isinstance(values, list) or not values or len(set(values)) != len(values):
        raise ValueError('自动运行时间配置无效')
    if any(not isinstance(v, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', v) for v in values):
        raise ValueError('自动运行时间必须为HH:MM')
    return sorted(values)


def export_policy(config=None):
    result = dict(DEFAULT_EXPORT_POLICY)
    result.update((config or {}).get('export_policy', {}))
    for key, value in result.items():
        if key not in DEFAULT_EXPORT_POLICY or not isinstance(value, int) or value < 1:
            raise ValueError('导出恢复策略无效：' + key)
    # Changing frequency/retry budgets requires a separate reviewed change.
    if result['download_attempts'] > 2:
        raise ValueError('同一报表最多下载两次，不增加平台重试频率')
    return result


def safe_error(error):
    value = str(error)
    value = re.sub(r'https?://[^\s\]\)\>"\']+', '[链接已隐藏]', value)
    value = re.sub(r'(?i)\b(cookie|token|authorization|password|secret)\s*[:=]\s*[^\s,;]+', r'\1=[已隐藏]', value)
    return value[:1800]


def error_code(error):
    text = str(error)
    for words, code in [
        (('403', '429', '验证码', '安全提示', '风险提示'), 'PLATFORM_SECURITY_STOP'),
        (('重新登录',), 'LOGIN_REQUIRED'),
        (('连接失败', 'ERR_', '网络'), 'CONNECTION_FAILED'),
        (('待确认', '新增结果尚未确认'), 'WRITE_RESULT_UNCERTAIN'),
        (('文件不完整', '订单文件', '金额', '订单状态', '日期超出', '核心字段'), 'SOURCE_VALIDATION_FAILED'),
        (('下载', '导出', '报表'), 'EXPORT_OR_DOWNLOAD_FAILED'),
        (('飞书',), 'FEISHU_SYNC_FAILED'),
        (('正在运行',), 'RUN_ALREADY_ACTIVE'),
    ]:
        if any(word in text for word in words):
            return code
    return 'RUN_FAILED'


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_schedule(config):
    """Read the existing Windows task; never register or repair it during a run."""
    expected=schedule_times(config)
    if os.name!='nt':return {'status':'unavailable','expected_times':expected}
    script="""$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new();
$t=Get-ScheduledTask -TaskName 'BuyinOrderSync';
[pscustomobject]@{times=@($t.Triggers|Where-Object Enabled|ForEach-Object {$_.StartBoundary.Substring(11,5)});daily=@($t.Triggers|ForEach-Object {$_.DaysInterval});enabled=$t.Settings.Enabled;timezone=(Get-TimeZone).Id}|ConvertTo-Json -Compress
"""
    try:
        result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],
            capture_output=True,timeout=15,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode!=0:raise RuntimeError('Windows任务读取未完成')
        actual=json.loads(result.stdout.decode('utf-8-sig'))
        matches=sorted(actual['times'])==expected and actual['enabled'] and all(x==1 for x in actual['daily']) and actual['timezone']=='China Standard Time'
        return {'status':'matched' if matches else 'drift','expected_times':expected,'actual_times':sorted(actual['times']),'timezone':actual['timezone']}
    except Exception as exc:
        return {'status':'unavailable','expected_times':expected,'error':safe_error(exc)}


class RunJournal:
    def __init__(self, root, report, writer):
        self.root, self.report, self.writer = Path(root), report, writer
        self.started = self.changed = time.monotonic()
        self.report['stage_events'] = []

    def stage(self, name, **details):
        tick = time.monotonic()
        events = self.report['stage_events']
        if events:
            events[-1]['duration_seconds'] = round(tick - self.changed, 3)
        self.changed = tick
        self.report['stage'] = name
        events.append({'stage': name, 'at': datetime.now(TZ).isoformat(timespec='seconds'),
                       'elapsed_seconds': round(tick - self.started, 3), **details})
        self.writer(self.root/'data/collection-state.json', {
            'run_id': self.report['run_id'], 'stage': name, 'time': events[-1]['at']})
        self.persist()

    def persist(self):
        self.writer(self.root/f"data/logs/{self.report['run_id']}.json", self.report)
        self.writer(self.root/'data/last-run.json', self.report)

    def finish(self):
        if self.report['stage_events']:
            self.report['stage_events'][-1]['duration_seconds'] = round(time.monotonic() - self.changed, 3)
        self.report['duration_seconds'] = round(time.monotonic() - self.started, 3)
        self.persist()
