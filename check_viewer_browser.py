"""Optional development check. Uses loopback HTTP, generated clips and Playwright."""
import datetime
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit
from unittest.mock import patch

import record_config as config
import recording_archive
import web_viewer as viewer


A, B, C = ('192.0.2.' + str(i) for i in range(1, 4))


def directory(ip):
    return ip.replace('.', '_')


def run(root, browser, screenshots):
    today = datetime.date.today()
    recent = today.strftime('%Y%m%d') + '_091000.mp4'
    yesterday = (today - datetime.timedelta(days=1)).strftime('%Y%m%d') + '_090000.mp4'
    older = (today - datetime.timedelta(days=3)).strftime('%Y%m%d') + '_091000.mp4'
    template = root / 'fixture.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=320x180:r=15',
                    '-t', '6', '-c:v', 'libx264', '-profile:v', 'main', '-g', '15',
                    '-movflags', '+faststart', str(template)], check=True)
    frag = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(template), '-c', 'copy',
                                   '-f', 'mp4', '-movflags', 'empty_moov+default_base_moof+frag_every_frame', 'pipe:1'])
    jpeg = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(template), '-frames:v', '1',
                                   '-f', 'image2pipe', '-c:v', 'mjpeg', 'pipe:1'])
    for ip, names in [(A, [recent, yesterday, older]), (B, [recent]), (C, [older])]:
        folder = root / directory(ip)
        folder.mkdir()
        for name in names:
            shutil.copyfile(str(template), str(folder / name))
    config.save_config({'paused': False, 'cameras': [{'ip': A}, {'ip': B}]})
    devices = [{'ip': '192.0.2.' + str(i), 'name': '測試鏡頭 ' + str(i), 'status': 'ok',
                'port': 554, 'path': '/h264'} for i in range(1, 6)]
    def scan():
        return dict(time=time.time(), seconds=1, networks=['192.0.2.0/24'],
                    cameras=devices, scanning=False, error='')
    def heartbeat():
        now = time.time()
        return dict(running=True, updated=now, schedule=['09:00', '17:00'], in_schedule=True,
                    progress_version=1, cameras={A: dict(state='recording', file=recent,
                    size=2048, last_write=now-1), B: dict(state='error')})
    class FixtureHandler(viewer.Handler):
        def stream_fmp4(self, ip):
            self.send_response(200)
            self.send_header('Content-Type', 'video/mp4')
            self.end_headers()
            for i in range(0, len(frag), 4096):
                self.wfile.write(frag[i:i+4096])
                time.sleep(0.06)
            time.sleep(0.5)
        def stream_live(self, ip):
            self.send_bytes(jpeg, 'image/jpeg')
    requests, errors = [], []
    def page_at(url, width, mjpeg=False, use_clock=False):
        page = browser.new_page(viewport={'width': width, 'height': 900})
        page.set_default_timeout(7000)
        if use_clock:
            page.clock.install()
        page.on('pageerror', lambda err: errors.append(str(err)))
        page.on('request', lambda r: requests.append(urlsplit(r.url).path))
        if mjpeg:
            page.add_init_script('window.MediaSource = window.ManagedMediaSource = undefined;')
        page.goto(url, wait_until='domcontentloaded')
        return page

    def check_layout(page, name, width):
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (name, width)
        small = page.locator('button:visible,select:visible,.button:visible,summary:visible,nav a:visible').evaluate_all(
            '(nodes,min) => nodes.filter(n => n.offsetHeight < min || n.offsetWidth < min).map(n => n.innerText)', 44 if width<=760 else 40)
        assert not small, (name, width, small)
        if screenshots:
            page.screenshot(path=str(screenshots / ('%s-%s.png' % (name, width))), full_page=True)

    def choose(page, ip):
        page.select_option('#camera', directory(ip))

    def wait_days(page, count):
        page.wait_for_function("n => document.querySelectorAll('.day').length===n && !document.querySelector('#range').disabled", arg=count)

    def select_clip(page, day, name):
        group = page.locator('.day[data-day="%s"]' % day)
        if not group.evaluate('n => n.open'):
            group.locator('summary').click()
        group.locator('.file[data-name="%s"] .file-time' % name).click()

    with patch.object(viewer, 'scan_snapshot', scan), patch.object(viewer, 'rec_status', heartbeat), \
            patch.object(viewer, 'run_scan', return_value=True) as scan_button, \
            viewer.Server(('127.0.0.1', 0), FixtureHandler) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = 'http://127.0.0.1:' + str(server.server_port)
        try:
            for width in (320, 390, 768, 1440):
                page = page_at(url, width, mjpeg=width==390)
                page.wait_for_function("document.querySelector('#writeCount').textContent==='1 / 2'")
                assert not any(p.startswith(('/fmp4/', '/live/')) for p in requests)
                check_layout(page, 'setup', width)
                if width==320:
                    picks=page.locator('.pick input:visible')
                    picks.nth(0).check(); picks.nth(1).check()
                    assert page.locator('#batchStart').inner_text()=='啟用所選（2）'
                    with page.expect_request(lambda req: req.url.endswith('/api/record')) as sent:
                        page.click('#batchStart')
                    assert sent.value.post_data_json=={'ips':[C,'192.0.2.4'],'record':True}
                    page.wait_for_function("document.querySelector('#writeCount').textContent==='1 / 4'")
                    assert page.locator('#batchStart').is_disabled()
                    page.request.post(url+'/api/record',data={'ips':[C,'192.0.2.4'],'record':False})
                if width==390:
                    page.locator('#detailsBtn').click()
                    assert page.locator('#heartbeat').inner_text()=='已收到'
                    page.locator('.watch').first.click()
                    page.wait_for_function("document.querySelector('.preview img')?.naturalWidth===320")
                    page.locator('.watch').first.click()
                page.click('#nav-archive')
                wait_days(page, 2)
                check_layout(page, 'archive', width)
                page.close()
                requests.clear()

            page = page_at(url, 1440)
            page.wait_for_selector('.camera-row')
            page.locator('.watch').first.click()
            page.wait_for_function("document.querySelector('.preview video')?.videoWidth===320")
            for i in range(1, 5):
                page.locator('.watch').nth(i).click()
            assert page.locator('.preview:not([hidden])').count()==4
            assert page.locator('.watch').first.get_attribute('aria-expanded')=='false'
            page.click('#scanBtn')
            assert scan_button.called
            page.locator('#controls summary').click()
            page.click('#recAll'); page.click('#cancel')
            assert not config.load_config()['paused']
            page.locator('#recAll').click(); page.locator('#proceed').click()
            page.wait_for_function("document.querySelector('#recAll').textContent.includes('恢復')")
            page.locator('#controls summary').click(); page.click('#recAll')
            page.wait_for_function("document.querySelector('#recAll').textContent.includes('暫停')")
            page.locator('.record').first.click(); page.keyboard.press('Escape')
            assert config.load_config()['cameras'][0]['ip']==A
            page.locator('.record').first.click(); page.click('#proceed')
            page.wait_for_function("document.querySelector('.record').textContent==='啟用錄影'")
            assert [c['ip'] for c in config.load_config()['cameras']]==[B]
            page.locator('.record').first.click()
            page.wait_for_function("document.querySelector('.record').textContent==='停止錄影'")
            page.click('#nav-archive'); wait_days(page, 2)
            assert page.locator('.preview video,.preview img').count()==0
            assert page.locator('#folder').input_value()==str(root / directory(A))
            page.evaluate("""() => {
                Object.defineProperty(window,'isSecureContext',{value:false});
                document.execCommand=cmd=>{
                    const input=document.querySelector('#folder');
                    window.copied=input.value.slice(input.selectionStart,input.selectionEnd);
                    return cmd==='copy';
                };
            }""")
            page.click('#copy')
            assert page.evaluate('window.copied')==str(root / directory(A))
            assert page.locator('#archiveMeta').inner_text()=='已完成 2 段'
            page.select_option('#range', 'all'); wait_days(page, 3)
            select_clip(page, today.isoformat(), recent)
            page.wait_for_function("document.querySelector('#player').videoWidth===320")
            page.eval_on_selector('#player', 'v=>v.currentTime=2')
            page.wait_for_function("document.querySelector('#player').currentTime>=2")
            href = page.locator('#download').get_attribute('href')
            assert page.request.get(url+href).body()==template.read_bytes()
            ranged = page.request.get(url+href, headers={'Range':'bytes=12-19'})
            assert ranged.status==206 and ranged.body()==template.read_bytes()[12:20]
            assert page.request.get(url+href, headers={'Range':'bytes=999999999-'}).status==416
            legacy={'cam':directory(B),'names':[recent]}
            assert page.request.post(url+'/api/delete',data=legacy).status==409
            assert (root / directory(B) / recent).exists()
            page.click('#manage'); page.click('#delCur')
            assert A in page.locator('#confirmDetail').inner_text()
            page.keyboard.press('Escape')
            choose(page, B); wait_days(page, 1)
            assert page.locator('#playback').is_hidden() and not page.locator('#download').get_attribute('href')
            assert page.locator('#player').get_attribute('src') is None
            page.evaluate("""(cam) => {
              const fetchNow=window.fetch;
              window.fetch=async (...args)=>{
                const response=await fetchNow(...args);
                if(args[0]==='/api/files/'+cam) await new Promise(resolve=>window.releaseOld=resolve);
                return response;
              };
            }""", directory(A))
            choose(page, A)
            page.wait_for_function("typeof window.releaseOld==='function'")
            choose(page, B); wait_days(page, 1)
            page.evaluate('window.releaseOld()')
            select_clip(page, today.isoformat(), recent)
            assert B in page.locator('#clipInfo').inner_text()
            page.select_option('#range', 'all')
            assert page.locator('#playback').is_hidden()
            select_clip(page, today.isoformat(), recent)
            page.click('#manage'); page.click('#delCur')
            detail = page.locator('#confirmDetail').inner_text()
            assert B in detail and recent in detail and '1 段' in detail
            page.click('#proceed')
            page.wait_for_selector('.day', state='detached')
            page.wait_for_function("document.querySelector('#archiveMeta').textContent==='已完成 0 段'")
            assert not (root / directory(B) / recent).exists()
            assert (root / directory(A) / recent).exists()
            choose(page, C)
            page.wait_for_selector('.empty button')
            page.click('.empty button'); wait_days(page, 1)
            page.click('#manage'); page.click('.day-actions button')
            assert older[:4] in page.locator('#confirmDetail').inner_text()
            page.click('#cancel')
            assert (root / directory(C) / older).exists()
            page.click('.day-actions button')
            with (root / directory(C) / older).open('ab'):
                page.click('#proceed')
                page.wait_for_function("document.querySelector('#toast').textContent.includes('保留 1 段')")
                page.wait_for_function("document.querySelector('#archiveMeta').textContent==='已完成 0 段'")
            assert (root / directory(C) / older).exists()
            devices[:] = devices[:2]
            page.click('#reload')
            page.wait_for_function("document.querySelector('#camera').options.length===2")
            assert (root / directory(C) / older).exists()
            page.close()

            with patch.object(viewer, 'rec_status', return_value=dict(running=False, cameras={})):
                active = root / directory(A) / (today.strftime('%Y%m%d') + '_235959.mp4')
                active.write_bytes(b'initial header')
                page = page_at(url, 390, mjpeg=True, use_clock=True)
                page.wait_for_function("document.querySelector('#writeCount').textContent==='0 / 2'")
                with active.open('ab') as stream:
                    stream.write(b'more bytes')
                page.clock.run_for(5100)
                page.wait_for_function("document.querySelector('#writeCount').textContent==='1 / 2'")
                assert '未取得服務回報' in page.locator('.evidence').first.inner_text()
                page.locator('.watch').first.click()
                page.wait_for_function("document.querySelector('.preview img')?.naturalWidth===320")
                page.evaluate("""() => {
                    Object.defineProperty(document,'hidden',{configurable:true,value:true});
                    document.dispatchEvent(new Event('visibilitychange'));
                }""")
                assert page.locator('.preview img').count()==0
                before = len(requests)
                page.clock.fast_forward(35000)
                assert len(requests)==before
                page.route('**/api/live', lambda route: route.abort())
                page.evaluate("""() => {
                    delete document.hidden; document.dispatchEvent(new Event('visibilitychange'));
                }""")
                page.wait_for_function("document.querySelector('#writeCount').textContent.includes('—')")
                assert not page.locator('.state.good').count()
                page.click('#nav-archive'); wait_days(page, 2)
                before = requests.count('/api/files/'+directory(A)), requests.count('/api/live')
                page.clock.fast_forward(35000)
                assert before==(requests.count('/api/files/'+directory(A)), requests.count('/api/live'))
                page.close()
            assert not errors, errors
        finally:
            server.shutdown()


def main():
    from playwright.sync_api import sync_playwright
    screenshots = os.environ.get('VIEWER_SCREENSHOTS')
    if screenshots:
        screenshots = Path(screenshots)
        screenshots.mkdir(parents=True, exist_ok=True)
    chrome = os.environ.get('CHROME_BIN')
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw, \
            patch.object(config, 'OUT_DIR', tmp), patch.object(config, 'CONFIG_FILE', str(Path(tmp) / 'record_config.json')):
        recording_archive.prepare_writer_checks()
        channel = 'chrome' if not chrome and not Path(pw.chromium.executable_path).exists() else None
        browser = pw.chromium.launch(executable_path=chrome, channel=channel, args=['--disable-background-networking'])
        try:
            run(Path(tmp), browser, screenshots)
        finally:
            browser.close()
    print('Browser checks passed at 320, 390, 768 and 1440 px.')


if __name__ == '__main__':
    main()
