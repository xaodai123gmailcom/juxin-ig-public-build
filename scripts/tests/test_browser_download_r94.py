"""Real HTTP and filesystem fault tests for browser download recovery."""
from pathlib import Path
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import http.client
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.error
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import browser_download as download


def archive_bytes(seed=0):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        for name in ('chrome.exe', 'chrome.dll', 'icudtl.dat', 'resources.pak', 'locales/en-US.pak'):
            archive.writestr('chrome-win64/' + name, bytes(range(256)) * 2 + str(seed).encode())
    return stream.getvalue()


class Response(io.BytesIO):
    def __init__(self, body, *, status=200, headers=None, cut=None, chunk=None, error=None):
        super().__init__(body)
        self.status = status
        self.headers = {'Content-Length': str(len(body)), 'ETag': '"fixture-v1"'} if headers is None else headers
        self.cut, self.chunk, self.error = cut, chunk, error

    def read1(self, length):
        if self.cut is not None:
            if self.tell() >= self.cut:raise self.error or TimeoutError('injected stall')
            length = min(length, self.cut - self.tell())
        if self.chunk:length = min(length, self.chunk)
        return self.read(length)


def resumed(data, offset, **changes):
    headers = {'Content-Length': str(len(data) - offset), 'ETag': '"fixture-v1"',
               'Content-Range': f'bytes {offset}-{len(data)-1}/{len(data)}'}
    headers.update(changes)
    return Response(data[offset:], status=206, headers=headers)


class DownloadRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='聚鑫 下载 (F8) ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'cache'
        self.url = 'https://example.invalid/chrome.zip'
        self.data = archive_bytes()
        self.calls = []

    def run_download(self, responses, **options):
        queue = iter(responses)
        def opener(request, **kwargs):
            self.calls.append((request, kwargs))
            value = next(queue)
            if isinstance(value, BaseException):raise value
            return value
        return download.download_archive(self.url, self.root, opener=opener, sleep=lambda _: None, **options)

    def partial(self, offset=333):
        with self.assertRaises(download.DownloadError):
            self.run_download([Response(self.data, cut=offset)], attempts=1)
        self.calls.clear()
        return offset

    def check_result(self, result):
        self.assertEqual(self.data, result['path'].read_bytes())
        self.assertEqual(hashlib.sha256(self.data).hexdigest(), result['sha256'])

    def test_slow_active_transfer_beyond_ten_minutes_completes(self):
        ticks = iter(range(0, 100000, 120))
        self.check_result(self.run_download([Response(self.data, chunk=31)], clock=lambda: next(ticks)))
        self.assertEqual(1, len(self.calls))

    def test_interrupted_connection_resumes_exact_saved_offset(self):
        cut = 437
        self.check_result(self.run_download([Response(self.data, cut=cut), resumed(self.data, cut)]))
        request = self.calls[1][0]
        self.assertEqual(f'bytes={cut}-', request.get_header('Range'))
        self.assertEqual('"fixture-v1"', request.get_header('If-range'))

    def test_restart_resumes_previous_process_progress(self):
        cut = self.partial()
        self.check_result(self.run_download([resumed(self.data, cut)]))
        self.assertEqual(f'bytes={cut}-', self.calls[0][0].get_header('Range'))

    def test_range_ignored_replaces_bytes_instead_of_appending(self):
        self.partial()
        self.check_result(self.run_download([Response(self.data)]))

    def test_changed_if_range_full_response_restarts_safely(self):
        self.partial()
        self.data = archive_bytes(123)
        self.check_result(self.run_download([Response(self.data, headers={'ETag': '"new"', 'Content-Length': str(len(self.data))})]))

    def test_incorrect_206_metadata_never_publishes(self):
        for headers in ({'ETag': '"changed"'}, {'Content-Range': f'bytes 0-{len(self.data)-1}/{len(self.data)}'},
                        {'Content-Range': 'garbage'}, {'Content-Length': '5'}, {'Content-Encoding': 'gzip'}):
            cut = self.partial()
            with self.subTest(headers=headers), self.assertRaises(download.DownloadError):
                self.run_download([resumed(self.data, cut, **headers)], attempts=1)
            self.assertFalse(list(self.root.glob('*/browser.zip')))

    def test_incompatible_range_automatically_restarts_without_mixing_bytes(self):
        cut = self.partial()
        self.check_result(self.run_download([resumed(self.data,cut,ETag='"changed"'),Response(self.data)]))
        self.assertIsInstance(self.calls[1][0], str)

    def test_missing_browser_resources_cannot_poison_completed_cache(self):
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as archive:archive.writestr('chrome-win64/chrome.exe',b'broken fixture')
        with self.assertRaisesRegex(download.DownloadError,'incomplete'):
            self.run_download([Response(stream.getvalue())])
        self.assertFalse(list(self.root.glob('*/browser.zip')))
        self.check_result(self.run_download([Response(self.data)]))

    def test_short_eof_resumes_until_declared_size_complete(self):
        cut = 601
        first = Response(self.data[:cut], headers={'ETag': '"fixture-v1"', 'Content-Length': str(len(self.data))})
        self.check_result(self.run_download([first, resumed(self.data, cut)]))

    def test_incomplete_read_preserves_exception_partial_bytes(self):
        response = Response(b'')
        response.headers = {'ETag': '"fixture-v1"', 'Content-Length': str(len(self.data))}
        response.read1 = Mock(side_effect=http.client.IncompleteRead(self.data[:199]))
        self.check_result(self.run_download([response, resumed(self.data, 199)]))

    def test_no_validator_restarts_without_unsafe_range(self):
        with self.assertRaises(download.DownloadError):
            self.run_download([Response(self.data, cut=333, headers={'Content-Length': str(len(self.data))})], attempts=1)
        self.calls.clear();self.check_result(self.run_download([Response(self.data)]))
        self.assertIsInstance(self.calls[0][0], str)

    def test_weak_etag_is_not_used_for_resumption(self):
        with self.assertRaises(download.DownloadError):
            self.run_download([Response(self.data, cut=333, headers={'ETag': 'W/"weak"', 'Content-Length': str(len(self.data))})], attempts=1)
        self.calls.clear();self.check_result(self.run_download([Response(self.data)]))
        self.assertIsInstance(self.calls[0][0], str)

    def test_last_modified_can_validate_a_resume(self):
        modified = 'Wed, 30 Sep 2026 01:00:00 GMT';cut=200
        headers={'Last-Modified': modified, 'Content-Length': str(len(self.data))}
        second = resumed(self.data, cut);second.headers.pop('ETag');second.headers['Last-Modified']=modified
        self.check_result(self.run_download([Response(self.data, cut=cut, headers=headers), second]))
        self.assertEqual(modified, self.calls[1][0].get_header('If-range'))

    def test_416_invalid_range_restarts_with_full_response(self):
        self.partial()
        error=urllib.error.HTTPError(self.url,416,'range',{},None)
        self.check_result(self.run_download([error, Response(self.data)]))
        self.assertIsInstance(self.calls[1][0], str)

    def test_retryable_http_and_connection_errors_are_bounded(self):
        errors=[urllib.error.HTTPError(self.url,503,'unavailable',{},None),
                urllib.error.URLError('connection down'),TimeoutError('connect stalled')]
        with self.assertRaisesRegex(download.DownloadError,'3 network attempts'):
            self.run_download(errors, attempts=3)
        self.assertEqual(3,len(self.calls))

    def test_404_is_not_retried(self):
        with self.assertRaisesRegex(download.DownloadError,'HTTP 404'):
            self.run_download([urllib.error.HTTPError(self.url,404,'not found',{},None)])
        self.assertEqual(1,len(self.calls))

    def test_complete_cache_is_checked_and_reused_without_network(self):
        original=self.run_download([Response(self.data)]);self.calls.clear()
        self.check_result(self.run_download([]));self.assertEqual([],self.calls)
        self.assertEqual(original['path'],self.run_download([])['path'])

    def test_corrupt_complete_cache_is_downloaded_again(self):
        original=self.run_download([Response(self.data)]);original['path'].write_bytes(b'broken')
        self.calls.clear();self.check_result(self.run_download([Response(self.data)]));self.assertEqual(1,len(self.calls))

    def test_completed_partial_can_be_sealed_after_restart_without_network(self):
        with patch.object(download, 'write_record', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.run_download([Response(self.data)])
        # Seed the exact state left by a kill after the final byte, before rename.
        entry=self.root/hashlib.sha256(self.url.encode()).hexdigest()
        (entry/'browser.zip.part').write_bytes(self.data)
        download.write_record(entry/'download.json',{'format':1,'url':self.url,'validator':None,'total':len(self.data)})
        self.calls.clear();self.check_result(self.run_download([]));self.assertEqual([],self.calls)

    def test_invalid_json_and_oversized_partial_restart_cleanly(self):
        self.partial();entry=next(self.root.glob('*/download.json')).parent
        (entry/'download.json').write_text('{broken');self.calls.clear()
        self.check_result(self.run_download([Response(self.data)]));self.assertIsInstance(self.calls[0][0],str)

    def test_oversized_response_is_rejected_before_body_is_written(self):
        response=Response(self.data,headers={'Content-Length':str(download.MAX_BYTES+1)})
        with self.assertRaises(download.DownloadError):self.run_download([response])
        self.assertFalse(list(self.root.glob('*/browser.zip')))

    def test_wrong_length_invalid_zip_and_path_traversal_are_rejected(self):
        bad=io.BytesIO()
        with zipfile.ZipFile(bad,'w') as archive:archive.writestr('../escape',b'bad')
        for data in (b'<html>proxy error</html>',bad.getvalue()):
            with self.subTest(data=data[:20]),self.assertRaises(download.DownloadError):self.run_download([Response(data)])
            self.assertFalse(list(self.root.glob('*/browser.zip')))

    def test_disk_full_does_not_trigger_network_retry(self):
        original=Path.open
        class FullDisk:
            def __init__(self,stream):self.stream=stream
            def __enter__(self):return self
            def __exit__(self,*args):self.stream.close()
            def write(self,data):raise OSError(28,'No space left on device')
        def open_file(path,*args,**kwargs):
            stream=original(path,*args,**kwargs)
            return FullDisk(stream) if path.name=='browser.zip.part' and args and args[0]=='wb' else stream
        with patch.object(Path,'open',open_file),self.assertRaisesRegex(OSError,'No space'):
            self.run_download([Response(self.data)])
        self.assertEqual(1,len(self.calls))

    def test_keyboard_interrupt_keeps_bytes_and_releases_cache_lock(self):
        with self.assertRaises(KeyboardInterrupt):self.run_download([Response(self.data,cut=188,error=KeyboardInterrupt())])
        self.check_result(self.run_download([resumed(self.data,188)]))

    def test_cache_retains_at_most_three_version_entries(self):
        for index in range(5):
            self.url=f'https://example.invalid/chrome-{index}.zip'
            self.check_result(self.run_download([Response(self.data)]))
        self.assertEqual(3,len(list(self.root.glob('*/browser.zip'))))

    def test_file_in_place_of_cache_directory_is_not_modified(self):
        self.root.write_bytes(b'preserve')
        with self.assertRaises(download.DownloadError):self.run_download([])
        self.assertEqual(b'preserve',self.root.read_bytes())

    def test_owned_process_lock_releases_after_process_termination(self):
        code="import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);from browser_download import cache_lock\nwith cache_lock(Path(sys.argv[2])):\n print('READY',flush=True)\n sys.stdin.read(1)\n"
        child=subprocess.Popen([sys.executable,'-I','-c',code,str(ROOT/'scripts'),str(self.root)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            self.assertEqual(b'READY\n',child.stdout.readline().replace(b'\r\n',b'\n'))
            with self.assertRaisesRegex(download.DownloadError,'Another browser download'):self.run_download([])
        finally:
            child.terminate();child.communicate(timeout=10)
        self.check_result(self.run_download([Response(self.data)]))


class RealHTTPResumeTests(unittest.TestCase):
    def test_actual_disconnect_range_and_verified_cache(self):
        data=archive_bytes(555);requests=[];cut=900
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(dict(self.headers))
                if len(requests)==1:
                    self.send_response(200);self.send_header('ETag','"fixture-v1"');self.send_header('Content-Length',str(len(data)));self.end_headers()
                    self.wfile.write(data[:cut]);self.wfile.flush();self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
                self.send_response(206);self.send_header('ETag','"fixture-v1"');self.send_header('Content-Length',str(len(data)-cut));self.send_header('Content-Range',f'bytes {cut}-{len(data)-1}/{len(data)}');self.end_headers();self.wfile.write(data[cut:])
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                url=f'http://127.0.0.1:{server.server_port}/browser.zip'
                result=download.download_archive(url,folder,sleep=lambda _:None)
                self.assertEqual(data,result['path'].read_bytes());self.assertEqual(f'bytes={cut}-',requests[1]['Range'])
                self.assertEqual('"fixture-v1"',requests[1]['If-Range'])
                download.download_archive(url,folder,opener=Mock(side_effect=AssertionError('network used for cached file')))
                self.assertEqual(2,len(requests))
        finally:server.shutdown();server.server_close();thread.join(timeout=5)


if __name__=='__main__':unittest.main()
