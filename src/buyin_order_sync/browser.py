"""Normal browser export; credentials persist only in the private Chrome profile.

Temporary transfer to the visible export context stays in memory. No request
replay, signature generation, or CAPTCHA bypass is implemented.
"""
import json
import os
import re
import subprocess
import time
import sys
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from .runtime_state import export_policy, safe_error

TZ = timezone(timedelta(hours=8))
ENTRY = 'https://buyin.jinritemai.com/dashboard/dataCenter/commission-order-detail'
PROFILE = 'data/browser-profile'
ORDER_QUERY_PATHS = {'/api/author/order/details', '/api/author/order/summary'}


def open_browser(pw, root, headless=False):
    chrome = Path(os.environ.get('PROGRAMFILES', r'C:\Program Files')) / 'Google/Chrome/Application/chrome.exe'
    options = {'channel': 'chrome'} if chrome.exists() else {}
    return pw.chromium.launch_persistent_context(str(root / PROFILE), headless=headless,
                                                accept_downloads=True, viewport={'width':1280,'height':850},
                                                **options)


@contextmanager
def export_context(pw, root):
    """Keep authorized login in Chrome's profile; transfer it only in memory for this run."""
    profile = open_browser(pw, root, headless=True)
    browser = None
    context = None
    try:
        state = profile.storage_state()
        browser = pw.chromium.launch(channel='chrome', headless=False)
        context = browser.new_context(storage_state=state, accept_downloads=True,
                                      viewport={'width':1280,'height':850})
        yield context
    finally:
        if context:
            try:
                profile.add_cookies(context.cookies())
            except Exception:
                pass  # An interrupted browser must not replace a previous successful login.
        if browser: browser.close()
        profile.close()


def now():
    return datetime.now(TZ)


def connection_error(page, error=None):
    return page.url.startswith('chrome-error:') or bool(re.search(r'(?:net::)?ERR_(?:FAILED|CONNECTION_\w+|PROXY_\w+|TUNNEL_\w+|NAME_NOT_RESOLVED|TIMED_OUT)',str(error or '')))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    # Windows virus scanners or the status window can hold a brief read handle.
    for attempt in range(6):
        try:
            temp.replace(path)
            break
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(.05 * (2 ** attempt))


def check_page(page, institution):
    text = page.locator('body').inner_text(timeout=15000)
    if 'ERR_FAILED' in text or page.url.startswith('chrome-error:'):
        raise RuntimeError('浏览器页面连接失败，已停止；这不代表账号登录失效')
    if not page.url.startswith('https://buyin.jinritemai.com/'):
        raise RuntimeError('需要重新登录：当前页面不在百应机构后台')
    for warning in ('请完成安全验证', '请完成验证码', '请拖动滑块', '访问过于频繁', '账号风险提示', '账号存在风险'):
        if warning in text:
            raise RuntimeError('平台安全提示，已停止：' + warning)
    if '/dashboard' not in page.url:
        raise RuntimeError('需要重新登录：未确认机构身份 ' + institution)
    if institution not in text:
        try:
            page.get_by_text(institution,exact=True).first.wait_for(state='visible',timeout=15000)
            text=page.locator('body').inner_text()
        except Exception as exc:
            raise RuntimeError('机构身份未加载完成，已暂停并保留旧数据') from exc
    return text


def enter_institution(page, institution):
    try:
        page.goto('https://buyin.jinritemai.com/mpa/account/institution-role-select',
                  wait_until='domcontentloaded',timeout=60000)
        # The site sometimes remembers the role and sometimes displays the normal role selector.
        deadline=time.monotonic()+30
        while '/dashboard' not in page.url and time.monotonic()<deadline:
            text=page.locator('body').inner_text(timeout=15000)
            if connection_error(page) or 'ERR_FAILED' in text:
                raise RuntimeError('浏览器页面连接失败，已停止；这不代表账号登录失效')
            if any(word in text for word in ('请完成安全验证','请完成验证码','请拖动滑块','访问过于频繁','账号风险提示','账号存在风险')):
                raise RuntimeError('平台安全提示，已停止')
            if re.search('扫码登录|验证码登录|手机号登录|短信登录|请登录',text) or '/login' in page.url or 'douyinec.com' in page.url:
                raise RuntimeError('需要重新登录：请点重新登录百应，完成后关闭登录窗口')
            role=page.get_by_text('MCN机构',exact=True)
            if role.count()==1 and role.is_visible():
                role.click()
                page.wait_for_url('**/dashboard**',timeout=30000)
                break
            page.wait_for_timeout(500)
        if '/dashboard' not in page.url:
            raise RuntimeError('百应入口未加载完成，已暂停并保留旧数据')
    except RuntimeError:
        raise
    except Exception as exc:
        if connection_error(page,exc):
            raise RuntimeError('浏览器页面连接失败，已停止；这不代表账号登录失效') from exc
        if '/login' in page.url or 'douyinec.com' in page.url:
            raise RuntimeError('需要重新登录：请点重新登录百应，完成后关闭登录窗口') from exc
        raise RuntimeError('百应机构入口未完成，已暂停并保留旧数据') from exc
    return check_page(page,institution)


def login(root, institution=None, timeout=600):
    """Let the user sign in in the same managed Chrome runtime used for exports.

    Authentication stays in Chrome's encrypted
    profile; neither passwords nor a plaintext storage-state file are written.
    """
    from playwright.sync_api import sync_playwright
    root = Path(root)
    status = root / 'data/login-state.json'
    chrome = Path(os.environ.get('PROGRAMFILES', r'C:\Program Files')) / 'Google/Chrome/Application/chrome.exe'
    if not chrome.exists():
        raise RuntimeError('未找到本机Chrome，请安装后重新登录')
    def progress(text, verified=False):
        write_json(status,{'status':text,'time':now().isoformat(),'institution':institution,
                           'verified':verified,'profile':PROFILE})
    progress('正在打开专用登录窗口；核对机构成功后自动保存。')
    with sync_playwright() as pw:
        try:ctx=open_browser(pw,root,headless=False)
        except Exception as exc:
            progress('专用登录窗口正在使用，请关闭该窗口后重试。')
            raise RuntimeError('专用登录窗口暂时无法打开，请先关闭同一程序的Chrome窗口') from exc
        denied=[]
        def observed(response):
            if urlsplit(response.url).hostname=='buyin.jinritemai.com' and response.status in (403,429):
                denied.append(response.status);ctx.close()
        ctx.on('response',observed)
        identity_confirmed=False
        try:
            page=ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto('https://www.douyinec.com/',wait_until='domcontentloaded',timeout=60000)
            page.get_by_text('登录',exact=True).hover()
            link=page.get_by_text('达人机构工作台',exact=True)
            with ctx.expect_page(timeout=15000) as popup:link.click()
            page=popup.value
            page.wait_for_load_state('domcontentloaded',timeout=30000)
            progress('请在新窗口正常登录，选择达人机构/MCN机构；程序会核对“'+institution+'”机构后自动保存。')
            deadline=time.monotonic()+timeout
            last_text=None
            while time.monotonic()<deadline and ctx.pages:
                if denied:raise RuntimeError('平台返回403/429，已停止登录检查')
                for candidate in list(ctx.pages):
                    if candidate.is_closed() or not candidate.url.startswith('https://buyin.jinritemai.com/'):
                        continue
                    try:body=candidate.locator('body').inner_text(timeout=3000)
                    except Exception:continue
                    if 'ERR_FAILED' in body or connection_error(candidate):
                        message='浏览器页面连接失败，尚未确认登录；已保留原资料。'
                    elif '入驻中' in body and '欢迎入驻' in body:
                        message='当前显示机构入驻页，尚未进入“'+institution+'”；请核对登录账号和机构身份。'
                    elif '/dashboard' in candidate.url and candidate.get_by_text(institution,exact=True).count():
                        check_page(candidate,institution)
                        identity_confirmed=True
                        progress('已确认“'+institution+'”机构，正在保存登录并用同步窗口验证订单页面。')
                        break
                    else:
                        message='请在新窗口正常登录并进入“'+institution+'”机构；尚未确认登录成功。'
                    if message!=last_text:progress(message);last_text=message
                if identity_confirmed:break
                page=next((p for p in reversed(ctx.pages) if not p.is_closed()),None)
                if page is None:break
                try:page.wait_for_timeout(1000)
                except Exception:break
            if not identity_confirmed:
                progress('登录未完成：尚未核对“'+institution+'”机构及订单页，未标记成功；可重新打开登录窗口。')
        except Exception as exc:
            if denied:
                progress('平台返回403/429，已停止；未标记登录成功。')
                raise RuntimeError('平台返回403/429，已停止登录检查') from None
            if connection_error(page,exc):
                progress('浏览器页面连接失败，尚未确认登录；已保留原资料。')
                raise RuntimeError('浏览器页面连接失败，未标记登录成功') from exc
            progress('登录检查未完成，未标记成功；请查看窗口或重新打开登录。')
            raise
        finally:
            ctx.close()
        if identity_confirmed:
            # Persistent-profile navigation can fail after SSO. Validate through the
            # same isolated browser context used for actual exports, with saved auth.
            with export_context(pw,root) as validation:
                denied=[]
                def observed_validation(response):
                    if urlsplit(response.url).hostname=='buyin.jinritemai.com' and response.status in (403,429):
                        denied.append(response.status);validation.close()
                validation.on('response',observed_validation)
                target=validation.new_page()
                try:
                    target.goto(ENTRY,wait_until='domcontentloaded',timeout=30000)
                    target.get_by_role('button',name='查询',exact=True).wait_for(timeout=30000)
                    check_page(target,institution)
                    progress('登录成功：已核对“'+institution+'”机构和订单页面；登录已保存，可点立即更新。',True)
                except Exception as exc:
                    if denied:
                        progress('平台返回403/429，已停止；登录资料保留，未标记验证成功。')
                        raise RuntimeError('平台返回403/429，已停止登录检查') from None
                    progress('机构身份已确认，订单页面验证未完成；登录资料已保留，可稍后重试。')
                    raise RuntimeError('订单页面验证未完成，已保留正确账号的登录资料') from exc


def set_range(page, start, end):
    page.get_by_role('textbox', name='开始日期', exact=True).first.click()
    calendar = page.locator('.sp-picker-range-body .auxo-picker-dropdown-range:visible')
    calendar.wait_for(timeout=10000)
    for target in (start, end):
        cell = calendar.locator(f'td.auxo-picker-cell-in-view[title="{target:%Y-%m-%d}"]')
        for _ in range(13):
            if cell.count(): break
            header = calendar.locator('.auxo-picker-date-panel').first
            year = int(header.locator('.auxo-picker-year-btn').inner_text().replace('年',''))
            month = int(header.locator('.auxo-picker-month-btn').inner_text().replace('月',''))
            if (year,month) > (target.year,target.month):
                calendar.locator('.auxo-picker-header-prev-btn:visible').first.click()
            else:
                calendar.locator('.auxo-picker-header-next-btn:visible').last.click()
        if cell.count() != 1:
            raise RuntimeError('平台日历无法唯一选择目标日期，已停止')
        cell.click()
    dates = [page.get_by_role('textbox',name=n,exact=True).last.input_value() for n in ('开始日期','结束日期')]
    if dates != [start.strftime('%Y/%m/%d'),end.strftime('%Y/%m/%d')]:
        raise RuntimeError('日历选中日期与请求不符，已停止')
    for name,target in [('开始时间',start),('结束时间',end)]:
        field=page.get_by_placeholder(name,exact=True)
        field.click()
        popup=field.locator('xpath=ancestor::span[1]').locator('.sp-picker-range-time-popup')
        popup.wait_for(state='visible',timeout=10000)
        columns=popup.locator('.auxo-picker-time-panel-column')
        if columns.count()!=3:
            raise RuntimeError('平台时间选择器结构变化，已停止')
        for index,value in enumerate(target.strftime('%H:%M:%S').split(':')):
            columns.nth(index).get_by_text(value,exact=True).click()
        confirm=popup.get_by_role('button',name='确定',exact=True)
        if popup.is_visible() and confirm.is_visible(): confirm.click()
        if field.input_value()!=target.strftime('%H:%M:%S'):
            raise RuntimeError('平台时间选择结果不符，已停止')
    page.locator('.sp-picker-range-ok-btn').click()
    actual=[page.get_by_role('textbox',name=n,exact=True).first.input_value() for n in ('开始日期','结束日期')]
    if actual != [start.strftime('%Y/%m/%d %H:%M:%S'),end.strftime('%Y/%m/%d %H:%M:%S')]:
        raise RuntimeError('日期范围最终校验失败，未导出')


def query_order_count(page, institution, timeout=60, stable_seconds=2, prepare=None, diagnostics=None):
    """Wait for this UI query's requests and rendered total, never an old total."""
    pending, completed, paths, failures, observed_paths = set(), set(), set(), [], set()

    def relevant(request):
        return (request.resource_type in ('xhr', 'fetch') and
                urlsplit(request.url).hostname == 'buyin.jinritemai.com' and
                urlsplit(request.url).path in ORDER_QUERY_PATHS)

    def started(request):
        if request.resource_type in ('xhr', 'fetch'):
            parsed = urlsplit(request.url)
            observed_paths.add((parsed.hostname or '') + parsed.path)
        if relevant(request):
            pending.add(request)
            paths.add(urlsplit(request.url).path)

    def finished(request):
        if request in pending:
            pending.remove(request)
            response = request.response()
            if response is None or not 200 <= response.status < 300:
                failures.append('查询请求未成功完成')
            else:
                completed.add(request)

    def failed(request):
        if request in pending:
            pending.remove(request)
            failures.append('查询请求失败，未读取旧条数')

    listeners = [('request', started), ('requestfinished', finished), ('requestfailed', failed)]
    for event, callback in listeners:
        page.on(event, callback)
    began = time.monotonic()
    stable_since, previous = None, None
    try:
        if prepare is not None:
            prepare()
        page.get_by_role('button', name='查询', exact=True).click()
        while time.monotonic() - began < timeout:
            page.wait_for_timeout(500)
            text = check_page(page, institution)
            if failures:
                raise RuntimeError(failures[0])
            counts = re.findall(r'共\s*([\d,]+)\s*条数据', text)
            if diagnostics is not None:
                try:
                    diagnostics({'completed_requests': len(completed),
                                 'pending_paths': sorted({urlsplit(r.url).path for r in pending}),
                                 'observed_paths': sorted(observed_paths), 'page_counts': counts,
                                 'wait_seconds': round(time.monotonic() - began, 1)})
                except OSError:
                    pass  # Optional diagnostics must not abort a valid collection.
            ready = completed and not pending and len(counts) == 1 and '计算中' not in text
            current = int(counts[0].replace(',', '')) if ready else None
            if current is None or current != previous:
                stable_since = time.monotonic() if current is not None else None
            previous = current
            if stable_since is not None and time.monotonic() - stable_since >= stable_seconds:
                return current, {'completed_requests': len(completed), 'request_paths': sorted(paths),
                                 'wait_seconds': round(time.monotonic() - began, 1)}
        raise RuntimeError('本次查询未完成或订单总量未稳定，已停止导出，未使用旧页面条数')
    finally:
        for event, callback in listeners:
            page.remove_listener(event, callback)


def validate_download_file(path):
    path=Path(path)
    if not path.exists() or path.stat().st_size==0 or not zipfile.is_zipfile(path):
        raise RuntimeError('下载文件不是完整Excel容器，未进入订单处理')
    with zipfile.ZipFile(path) as archive:
        names=set(archive.namelist())
        if not {'[Content_Types].xml','xl/workbook.xml'}<=names or not any(n.startswith('xl/worksheets/sheet') for n in names):
            raise RuntimeError('下载文件缺少Excel工作簿结构，未进入订单处理')


def download_generated_report(page, card, institution, directory, progress, policy=None):
    """Listen on every page for the same generated file, including download popups.

    One bounded retry only repeats the download button, never the export request.
    Ordinary login/security checks must pass again before that retry.
    """
    policy=export_policy({'export_policy':policy or {}})
    downloads=[]
    listeners=[]
    owned={id(page)}
    clicked=[False]
    context=page.context
    isolated=len(context.pages)==1
    def watch(candidate, newly_opened=False):
        if candidate is not page:
            opener=getattr(candidate,'opener',None)
            try:opener=opener() if callable(opener) else opener
            except Exception:opener=None
            if opener is not None and id(opener) in owned:
                owned.add(id(candidate))
            elif opener is None and newly_opened and clicked[0] and isolated:
                # Download-only windows can close before opener() resolves. The
                # dedicated context had only the report page before this click.
                owned.add(id(candidate))
        def received(download):
            if clicked[0] and id(candidate) in owned and all(download is not value[1] for value in downloads):
                downloads.append((candidate,download))
        candidate.on('download',received)
        listeners.append((candidate,received))
    for candidate in context.pages: watch(candidate)
    def watch_new(candidate):watch(candidate,True)
    context.on('page',watch_new)
    def check_popups():
        for candidate,_ in listeners:
            if candidate is page or id(candidate) not in owned:continue
            url=getattr(candidate,'url','')
            if '/login' in url or 'douyinec.com' in url:
                raise RuntimeError('需要重新登录：下载弹窗要求登录，已停止')
            if url.startswith('https://buyin.jinritemai.com/') and not candidate.is_closed():
                body=candidate.locator('body').inner_text(timeout=2000)
                if any(w in body for w in ('请完成安全验证','请完成验证码','请拖动滑块','访问过于频繁','账号风险提示','账号存在风险')):
                    raise RuntimeError('平台安全提示出现在下载弹窗，已停止')
    try:
        button=card.get_by_role('button',name='下载报表',exact=True)
        for attempt in range(policy['download_attempts']):
            if downloads:break
            check_page(page,institution)
            if attempt:progress('重新下载同一份已生成报表，不重复提交导出')
            else:progress('等待目标报表下载事件')
            clicked[0]=True
            button.click()
            deadline=time.monotonic()+policy['download_event_seconds']
            while not downloads and time.monotonic()<deadline:
                page.wait_for_timeout(500)
                check_page(page,institution)
                check_popups()
            if downloads:break
        if not downloads:
            raise RuntimeError('同一报表两次下载均未收到文件；已停止，没有重新提交导出')
        page.wait_for_timeout(250)
        check_page(page,institution)
        check_popups()
        if len(downloads)!=1:
            raise RuntimeError('目标报表出现多个下载文件，无法唯一确认，已停止')
        download=downloads[0][1]
        filename=getattr(download,'suggested_filename','report.xlsx')
        if not str(filename).lower().endswith('.xlsx'):
            raise RuntimeError('目标报表下载类型不是xlsx，已停止')
        saved=Path(directory)/(now().strftime('%Y%m%d-%H%M%S')+'.xlsx')
        saved.parent.mkdir(parents=True,exist_ok=True)
        if saved.exists():saved=saved.with_name(saved.stem+'-'+uuid.uuid4().hex[:6]+'.xlsx')
        temporary=saved.with_suffix('.xlsx.part')
        progress('正在保存下载文件')
        download.save_as(temporary)
        validate_download_file(temporary)
        temporary.replace(saved)
        return saved
    finally:
        context.remove_listener('page',watch_new)
        for candidate,received in listeners:
            try:candidate.remove_listener('download',received)
            except Exception:pass


def _export_orders_impl(root, start, end, institution=None, max_rows=200000, request_path=None,
                        policy=None, progress_path=None):
    """Download one institution export, then the caller retains only configured accounts."""
    root = Path(root)
    policy=export_policy({'export_policy':policy or {}})
    state = root / 'data/login-state.json'
    if not isinstance(institution, str) or not institution.strip():
        raise ValueError('请先配置自己的机构名称')
    if not (root / PROFILE / 'Default').exists():
        raise RuntimeError('需要重新登录：先在程序的专用窗口登录百应')
    from playwright.sync_api import sync_playwright
    start = datetime.fromisoformat(start).astimezone(TZ)
    end = datetime.fromisoformat(end).astimezone(TZ)
    cooldown = Path(request_path) if request_path else root / 'data/export-request.json'
    resume=False
    last={}
    if cooldown.exists():
        last = json.loads(cooldown.read_text(encoding='utf-8'))
        elapsed = (now() - datetime.fromisoformat(last['requested_at'])).total_seconds()
        if elapsed<0:
            raise RuntimeError('本机时间早于已有导出请求时间，停止恢复，请核对时钟')
        if last.get('institution',institution)!=institution:
            raise RuntimeError('已有导出任务机构与当前配置不同，停止恢复')
        resume=last.get('status')!='downloaded' and elapsed<policy['resume_window_seconds']
        if resume:
            start,end=datetime.fromisoformat(last['start']),datetime.fromisoformat(last['end'])
        elif elapsed < policy['cooldown_seconds']:
            raise RuntimeError(f'平台导出冷却中，请在 {int(policy["cooldown_seconds"]+1-elapsed)} 秒后重试；没有重复提交导出')
    start_text, end_text = start.strftime('%Y/%m/%d %H:%M:%S'), end.strftime('%Y/%m/%d %H:%M:%S')
    def progress(stage):
        write_json(Path(progress_path) if progress_path else root/'data/collection-state.json',{'stage':stage,'time':now().isoformat(),
                   'start':start.isoformat(),'end':end.isoformat(),'resume':resume})
    with sync_playwright() as pw:
        try:
            manager = export_context(pw, root)
            ctx = manager.__enter__()
        except Exception as exc:
            raise RuntimeError('专用浏览器暂时无法打开：请先关闭刚才的登录窗口，再点立即更新') from exc
        denied = []
        def observe(response):
            if 'buyin.jinritemai.com' in response.url and response.status in (403,429):
                denied.append(response.status)
                ctx.close()
        ctx.on('response',observe)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            progress('打开百应并确认机构')
            enter_institution(page,institution)
            page.goto(ENTRY, wait_until='domcontentloaded', timeout=60000)
            page.bring_to_front()
            page.get_by_role('button', name='查询', exact=True).wait_for(timeout=30000)
            check_page(page, institution)
            write_json(state, {'status':'登录成功','institution':institution,'time':now().isoformat()})
            if resume:
                expected=last.get('expected_rows')
                query_evidence=last.get('query_evidence',{})
                if not isinstance(expected,int) or expected<0:
                    raise RuntimeError('原导出任务缺少可验证条数，未重新提交导出')
            else:
                page.get_by_role('button', name='重置', exact=True).click()
                for name in ('达人信息','订单 ID','商品 ID'):
                    if page.get_by_role('textbox',name=name,exact=True).input_value():
                        raise RuntimeError('查询条件没有完整重置，停止取数')
                progress('选择并核对日期时间')
                progress('等待本次查询完成并核对订单总量')
                expected, query_evidence = query_order_count(page, institution,
                    prepare=lambda: set_range(page,start,end),
                    diagnostics=lambda value: write_json(root/'data/query-count-check.json', value))
            if expected > max_rows:
                raise RuntimeError('订单量超过本程序单批上限，需缩短区间，未截断采集')
            progress('查询完成，核对导出任务')
            if denied:
                raise RuntimeError('平台返回403/429，已停止')
            submitted = last['requested_at'] if resume else now().isoformat(timespec='seconds')
            request={**last} if resume else {}
            request.update(requested_at=submitted,start=start.isoformat(),end=end.isoformat(),
                           expected_rows=expected,institution=institution,query_evidence=query_evidence,
                           task_fingerprint=last.get('task_fingerprint') if resume else uuid.uuid4().hex)
            if resume:
                progress('恢复已有导出任务，不重新提交')
                page.get_by_role('menuitem',name='导出记录',exact=True).click()
            else:
                progress('提交百应导出任务')
                page.get_by_role('button', name='导出数据', exact=True).click()
                write_json(cooldown,dict(request,status='awaiting_confirmation'))
                progress('确认百应导出任务')
                page.get_by_role('dialog').get_by_role('button', name='导出', exact=True).click()
                request['status']='submitted'
                write_json(cooldown,request)
            progress('等待平台生成报表')
            page.get_by_text('刷新查看最新导出结果', exact=False).wait_for(timeout=30000)
            range_text = start_text + '-' + end_text
            deadline = time.monotonic() + policy['report_wait_seconds']
            while time.monotonic() < deadline:
                try: page.get_by_text('佣金订单明细导出',exact=True).first.wait_for(timeout=30000)
                except Exception: pass
                text=check_page(page, institution)
                if denied:
                    raise RuntimeError('平台返回403/429，停止等待导出')
                match = page.get_by_text(range_text, exact=True)
                if match.count()>1:
                    raise RuntimeError('同一查询范围有多个导出任务，无法唯一确认，未选择任意报表')
                if match.count() == 1:
                    card = match.locator('xpath=ancestor::div[@class="export-list-item"][1]')
                    if card.count() == 1:
                        card_text = card.inner_text()
                        task_id=card.get_attribute('data-task-id') or card.get_attribute('data-export-id')
                        if request.get('platform_task_id') and task_id!=request['platform_task_id']:
                            raise RuntimeError('导出任务标识与原任务不同，停止下载')
                        for label in ('订单Id','作者信息','订单状态','商品Id'):
                            if not re.search(re.escape(label)+r'\s*全部(?:\s|$)',card_text):
                                raise RuntimeError('报表筛选条件与机构全部订单不符，停止同步')
                        button = card.get_by_role('button', name='下载报表', exact=True)
                        if button.count() == 1:
                            request.update(status='ready',platform_task_id=task_id)
                            write_json(cooldown,request)
                            progress('报表就绪，准备下载')
                            saved = download_generated_report(page,card,institution,root/'data/downloads',progress,policy)
                            write_json(cooldown,dict(request,status='downloaded',file=str(saved),file_validation='pending'))
                            progress('下载完成，开始核对订单')
                            return {'file': str(saved), 'expected_rows': expected, 'start': start.isoformat(),
                                    'end': end.isoformat(), 'as_of': submitted, 'scope': 'all', 'method': 'browser_ui',
                                    'query_evidence':query_evidence,'task_fingerprint':request.get('task_fingerprint'),
                                    'platform_task_id':task_id,'as_of_basis':'export_requested_at',
                                    'export_requested_at':submitted}
                        if any(word in card_text for word in ('生成失败', '导出失败')):
                            raise RuntimeError('平台报表生成失败')
                page.wait_for_timeout(30000)
                page.reload(wait_until='domcontentloaded')
            raise RuntimeError('导出报表等待超时；未再次提交，已保留平台任务')
        except Exception:
            if denied:
                raise RuntimeError('平台返回403/429，已立即停止；保留上次成功数据') from None
            raise
        finally:
            manager.__exit__(None, None, None)


def mark_export_validated(root, meta, path):
    state=Path(root)/'data/export-request.json'
    if state.exists():
        request=json.loads(state.read_text(encoding='utf-8'))
        if request.get('file') and Path(request['file']).resolve()==Path(path).resolve():
            request.update(file_validation='passed',validated_at=now().isoformat(timespec='seconds'))
            write_json(state,request)


def windows_parent_pid(pid):
    """Read one process's parent via documented Toolhelp APIs; no process mutation."""
    if os.name != 'nt':
        return None
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                    ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
                    ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                    ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG),
                    ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]

    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    api.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    api.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    api.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = api.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        return None
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        found = api.Process32FirstW(handle, ctypes.byref(entry))
        while found:
            if entry.th32ProcessID == pid:
                return entry.th32ParentProcessID
            found = api.Process32NextW(handle, ctypes.byref(entry))
    finally:
        api.CloseHandle(handle)
    return None


def worker_parent_matches(request):
    parent = os.getppid()
    if request.get('parent_pid') == parent:
        return True
    # Windows venv python.exe can redirect to the base interpreter, adding one
    # launcher hop. Accept only our exact Popen PID with the declared parent.
    return (os.name == 'nt' and request.get('launcher_pid') == parent
            and windows_parent_pid(parent) == request.get('parent_pid'))


def export_worker(root, request_file):
    """Internal child process: the parent owns the run lock and supervises deadlines."""
    root=Path(root).resolve();request_file=Path(request_file).resolve()
    if request_file.parent!=root/'data/export-workers':
        raise RuntimeError('内部导出请求路径无效')
    for _ in range(20):
        request=json.loads(request_file.read_text(encoding='utf-8'))
        if worker_parent_matches(request):
            break
        if request.get('launcher_pid') is not None:
            break
        time.sleep(.05)  # Parent atomically adds the exact launcher PID after Popen.
    if not worker_parent_matches(request):
        raise RuntimeError('导出子进程缺少匹配的调用进程')
    result_file=request_file.with_suffix('.result.json')
    try:
        meta=_export_orders_impl(root,request['start'],request['end'],request['institution'],
            request['max_rows'],request_path=request.get('request_path'),policy=request['policy'],progress_path=request_file.with_suffix('.progress.json'))
        write_json(result_file,{'ok':True,'meta':meta})
        return 0
    except Exception as exc:
        write_json(result_file,{'ok':False,'error':safe_error(exc)})
        return 1


def stop_export_worker(process):
    if process.poll() is not None:return
    # Only the process tree created by this call is stopped; no profile-wide kill.
    if os.name=='nt':
        subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,timeout=20,
                       creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    else:
        process.terminate()
    try:process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill();process.wait(timeout=10)


def export_orders(root, start, end, institution=None, max_rows=200000, request_path=None,
                  *, policy=None, progress_callback=None):
    """Supervise normal browser work so file saving cannot wait forever."""
    root=Path(root).resolve();policy=export_policy({'export_policy':policy or {}})
    folder=root/'data/export-workers';folder.mkdir(parents=True,exist_ok=True)
    request_file=folder/(uuid.uuid4().hex+'.json')
    request={'parent_pid':os.getpid(),'start':start,'end':end,'institution':institution,
             'max_rows':max_rows,'policy':policy,'request_path':str(Path(request_path).resolve()) if request_path else None}
    write_json(request_file,request)
    argv=([sys.executable] if getattr(sys,'frozen',False) else [sys.executable,'-m','buyin_order_sync','--state-dir',str(root)])
    process=subprocess.Popen(argv+['export-worker','--request',str(request_file)],cwd=root,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    request['launcher_pid']=process.pid
    write_json(request_file,request)
    began=time.monotonic();stage='启动导出子进程';changed=began
    progress_file=request_file.with_suffix('.progress.json')
    try:
        while process.poll() is None:
            if progress_file.exists():
                value=json.loads(progress_file.read_text(encoding='utf-8'))
                if value['stage']!=stage:
                    stage=value['stage'];changed=time.monotonic()
                    if progress_callback:progress_callback(stage)
            tick=time.monotonic()
            if tick-began>policy['worker_timeout_seconds'] or (stage=='正在保存下载文件' and tick-changed>policy['save_timeout_seconds']):
                stop_export_worker(process)
                raise RuntimeError('百应导出/下载阶段超时：'+stage+'；已停止本次子进程，保留原导出任务，未重复提交')
            time.sleep(.25)
        result_file=request_file.with_suffix('.result.json')
        if not result_file.exists():
            raise RuntimeError('导出子进程未返回可验证结果，未继续同步')
        result=json.loads(result_file.read_text(encoding='utf-8'))
        if not result.get('ok'):
            raise RuntimeError(result.get('error','导出子进程失败'))
        if process.returncode!=0:
            raise RuntimeError('导出子进程异常退出，未继续同步')
        return result['meta']
    finally:
        stop_export_worker(process)

