"""1,000 parameterized download faults using the production downloader.

Uses in-memory HTTP responses and real temporary cache files, not live CDN
downloads or Windows builds. Real HTTP disconnects have a separate unit test.
"""
from pathlib import Path
import argparse
from contextlib import redirect_stdout
import hashlib
import json
import sys
import tempfile
import time
import traceback
import urllib.error

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts/tests'))
from test_browser_download_r94 import Response, archive_bytes, download, resumed

FAMILIES = ('resume', 'restart', 'ignored_range', 'changed_validator',
            'short_eof', 'slow_active', 'corrupt_cache', 'network_retry',
            'invalid_range', 'no_validator')


def scenario(family, index):
    data = archive_bytes(index)
    cut = 1 + ((101 + index * 17) % (len(data) - 1))
    url = f'https://example.invalid/{family}/{index}/chrome.zip'
    calls = []
    with tempfile.TemporaryDirectory(prefix=f'F8 下载 [{index}] ') as folder:
        def run(responses, **kwargs):
            responses = iter(responses)
            def opener(request, **options):
                calls.append(request)
                response = next(responses)
                if isinstance(response, BaseException):raise response
                return response
            return download.download_archive(url, folder, opener=opener, sleep=lambda _:None, **kwargs)
        def save_partial(headers=None):
            try:run([Response(data, cut=cut, headers=headers)], attempts=1)
            except download.DownloadError:pass
            else:raise AssertionError('Interrupted transfer unexpectedly succeeded')
            assert next(Path(folder).glob('*/browser.zip.part')).stat().st_size == cut
            calls.clear()
        if family == 'resume':
            result = run([Response(data, cut=cut), resumed(data, cut)])
            assert calls[1].get_header('Range') == f'bytes={cut}-'
        elif family == 'restart':
            save_partial();result = run([resumed(data, cut)])
            assert calls[0].get_header('Range') == f'bytes={cut}-'
        elif family == 'ignored_range':
            save_partial();result = run([Response(data)])
        elif family == 'changed_validator':
            save_partial();data = archive_bytes(index + 10000)
            result = run([Response(data, headers={'Content-Length':str(len(data)), 'ETag':'"new-version"'})])
        elif family == 'short_eof':
            result = run([Response(data[:cut], headers={'Content-Length':str(len(data)), 'ETag':'"fixture-v1"'}), resumed(data, cut)])
        elif family == 'slow_active':
            ticks = iter(range(0, 10000000, 61 + index))
            result = run([Response(data, chunk=31 + index)], clock=lambda:next(ticks))
            assert next(ticks) > 600
        elif family == 'corrupt_cache':
            result = run([Response(data)]);result['path'].write_bytes(b'corrupted-' + str(index).encode())
            result = run([Response(data)])
        elif family == 'network_retry':
            result = run([urllib.error.HTTPError(url, (408,429,500,502,503,504)[index % 6], 'injected', {}, None),
                          urllib.error.URLError('injected connection loss'), Response(data)])
            assert len(calls) == 3
        elif family == 'invalid_range':
            save_partial()
            result = run([resumed(data, cut, **{'Content-Range':f'bytes {cut+1}-{len(data)-1}/{len(data)}'}), Response(data)])
            assert isinstance(calls[1], str)
        elif family == 'no_validator':
            save_partial({'Content-Length':str(len(data))});result = run([Response(data)])
            assert isinstance(calls[0], str)
        else:raise AssertionError(family)
        assert result['path'].read_bytes() == data
        assert result['sha256'] == hashlib.sha256(data).hexdigest()
        before = len(calls);again = run([])
        assert len(calls) == before and again['path'] == result['path']


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    started=time.monotonic();failures=0
    with (args.output/'cases.jsonl').open('w',encoding='utf-8') as results, (args.output/'execution.log').open('w',encoding='utf-8') as log:
        for family in FAMILIES:
            for index in range(100):
                record={'id':f'{family}-{index:03}','family':family,'parameter':index}
                try:
                    with redirect_stdout(log):scenario(family,index)
                    record['status']='passed'
                except Exception:
                    failures+=1;record.update(status='failed',error=traceback.format_exc())
                results.write(json.dumps(record,ensure_ascii=False)+'\n')
            results.flush();print(f'{family}: 100 scenarios completed',flush=True)
    summary={'scenarios':1000,'passed':1000-failures,'failed':failures,'families':len(FAMILIES),
             'live_cdn_downloads':0,'windows_builds':0,'seconds':round(time.monotonic()-started,3)}
    (args.output/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    print(json.dumps(summary),flush=True);return bool(failures)


if __name__=='__main__':raise SystemExit(main())
