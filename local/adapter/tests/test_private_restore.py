"""Tests for the rich private clipboard write (2026-11-08).

    python -I tests\test_private_restore.py

Never touches the real clipboard: the Windows calls are replaced by a fake.
"""
import json
import sys
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ADAPTER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADAPTER))

import flow_clipboard  # noqa: E402
import local_cleanup_shim as shim  # noqa: E402


class CfHtmlTests(unittest.TestCase):
    def offsets(self, data):
        head = data.split(b'<html>')[0].decode('ascii')
        return {line.split(':')[0]: int(line.split(':')[1]) for line in head.splitlines() if line.split(':')[1].isdigit()}

    def test_offsets_point_at_the_fragment(self):
        fragment = '<b>Dana’s garden</b>'
        data = flow_clipboard.cf_html(fragment)
        o = self.offsets(data)
        self.assertEqual(data[o['StartFragment']:o['EndFragment']].decode('utf-8'), fragment)
        self.assertEqual(data[o['StartHTML']:].rstrip(b'\0').decode('utf-8'),
                         '<html><body><!--StartFragment-->' + fragment + '<!--EndFragment--></body></html>')
        self.assertEqual(o['EndHTML'], len(data) - 1)
        self.assertEqual(data[-1:], b'\0')

    def test_offsets_count_bytes_not_characters(self):
        ascii_only = self.offsets(flow_clipboard.cf_html('garden'))
        wide = self.offsets(flow_clipboard.cf_html('gärden'))
        self.assertEqual(wide['EndFragment'] - ascii_only['EndFragment'], 1)


class PayloadTests(unittest.TestCase):
    def write(self, *args, **kw):
        with patch.object(flow_clipboard, '_owner_window', return_value=1), \
                patch.object(flow_clipboard, '_write') as fake, \
                patch.object(flow_clipboard._user32, 'DestroyWindow'):
            flow_clipboard.set_private_text(*args, deadline=time.monotonic() + 1, **kw)
        return fake.call_args[0][1]

    def format_id(self, name):
        return flow_clipboard._user32.RegisterClipboardFormatW(name)

    def test_text_only_payload_is_unchanged(self):
        payload = self.write('garden')
        self.assertEqual(payload[0], (flow_clipboard.CF_UNICODETEXT, 'garden\0'.encode('utf-16-le')))
        self.assertEqual(len(payload), 4)

    def test_rich_formats_are_added_with_the_private_flags(self):
        payload = dict(self.write('garden', html='<b>garden</b>', rtf='{\rtf1 garden}'))
        self.assertIn(self.format_id('HTML Format'), payload)
        self.assertEqual(payload[self.format_id('Rich Text Format')], b'{\rtf1 garden}\0')
        for name in ('ExcludeClipboardContentFromMonitorProcessing', 'CanIncludeInClipboardHistory',
                     'CanUploadToCloudClipboard'):
            self.assertEqual(payload[self.format_id(name)], b'\0\0\0\0')


class EndpointTests(unittest.TestCase):
    def post(self, body):
        server = ThreadingHTTPServer(('127.0.0.1', 0), shim.LocalCleanupShim)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        request = urllib.request.Request(f'http://127.0.0.1:{port}/v1/flow/clipboard', data=json.dumps(body).encode(),
                                         headers={'Content-Type': 'application/json'}, method='POST')
        calls = []
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with patch.object(shim, 'options', lambda: {'privateClipboard': True}),                 patch.object(flow_clipboard, 'set_private_text', lambda t, **kw: calls.append((t, kw))):
            with opener.open(request, timeout=5) as reply:
                self.assertEqual(json.load(reply), {'ok': True})
        return calls[0]

    def test_rich_formats_reach_the_writer(self):
        text, kw = self.post({'text': 'garden', 'html': '<b>garden</b>', 'rtf': '{\rtf1 garden}'})
        self.assertEqual(text, 'garden')
        self.assertEqual((kw['html'], kw['rtf']), ('<b>garden</b>', '{\rtf1 garden}'))

    def test_a_large_rich_copy_is_accepted(self):
        html = '<p>garden</p>' * 20000  # 260 KB
        self.assertEqual(self.post({'text': 'garden', 'html': html})[1]['html'], html)

    def test_plain_call_and_bad_types(self):
        self.assertNotIn('html', self.post({'text': 'garden'})[1])
        self.assertNotIn('html', self.post({'text': 'garden', 'html': 5})[1])


if __name__ == '__main__':
    unittest.main()
