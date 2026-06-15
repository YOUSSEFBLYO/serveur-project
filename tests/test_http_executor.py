from io import BytesIO
import json
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError, URLError
from django.test import TestCase

from workflows.executors.http_request import HttpRequestExecutor


class DummyNode:
    def __init__(self, config):
        self.config = config
        self.label = "Test Node"
        self.node_type = "http_request"


class TestHttpRequestExecutor(TestCase):

    def make_executor(self, config, context=None):
        node = DummyNode(config)
        return HttpRequestExecutor(node, context)

    @patch('workflows.executors.http_request.urlopen')
    def test_expected_status_parsing(self, mock_urlopen):
        # 1. Test standard parsing: expected_status is int 201, gets 201 back
        mock_resp = MagicMock()
        mock_resp.status = 201
        mock_resp.read.return_value = b'{"success": true}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        exec1 = self.make_executor({
            'url': 'http://test.com',
            'expected_status': 201
        })
        res1 = exec1.run()
        self.assertEqual(res1['http_status_code'], 201)

        # 2. Test string parsing "200 OK"
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"success": true}'
        exec2 = self.make_executor({
            'url': 'http://test.com',
            'expected_status': '200 OK'
        })
        res2 = exec2.run()
        self.assertEqual(res2['http_status_code'], 200)

        # 3. Test invalid expected_status "invalid_value", should fallback to 200
        mock_resp.status = 200
        exec3 = self.make_executor({
            'url': 'http://test.com',
            'expected_status': 'invalid_value'
        })
        res3 = exec3.run()
        self.assertEqual(res3['http_status_code'], 200)

        # 4. Test expected_status unequal raises RuntimeError
        mock_resp.status = 400
        exec4 = self.make_executor({
            'url': 'http://test.com',
            'expected_status': 200
        })
        with self.assertRaises(RuntimeError):
            exec4.run()

    @patch('workflows.executors.http_request.urlopen')
    def test_response_mapping(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"project": {"id": 123, "meta": {"owner": "alice"}}, "status": "active"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        # We configure response_mapping in node config
        exec_obj = self.make_executor({
            'url': 'http://test.com',
            'expected_status': 200,
            'response_mapping': json.dumps({
                'project_id': 'project.id',
                'owner': 'project.meta.owner',
                'status': 'status',
                'missing': 'project.nonexistent.field'
            })
        })
        res = exec_obj.run()
        self.assertEqual(res['project_id'], 123)
        self.assertEqual(res['owner'], 'alice')
        self.assertEqual(res['status'], 'active')
        self.assertIsNone(res['missing'])

    @patch('workflows.executors.http_request.random.uniform', return_value=0.5)
    @patch('workflows.executors.http_request.time.sleep')
    @patch('workflows.executors.http_request.urlopen')
    def test_retry_behavior_transient(self, mock_urlopen, mock_sleep, mock_uniform):
        # We mock 2 failed calls with 500 (transient error), then a success 200.
        # HTTPError constructor: url, code, msg, hdrs, fp
        err500 = HTTPError('http://test.com', 500, 'Internal Server Error', {}, BytesIO(b'server error'))
        
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"success": true}'

        mock_context = MagicMock()
        mock_context.__enter__.return_value = mock_resp

        # side_effect returns the exception first, then the mock response
        mock_urlopen.side_effect = [err500, err500, mock_context]

        exec_obj = self.make_executor({
            'url': 'http://test.com',
            'retry_on_fail': True,
            'retry_count': 3,
            'retry_delay': 2,
        })
        
        res = exec_obj.run()
        self.assertEqual(res['http_status_code'], 200)
        self.assertEqual(mock_urlopen.call_count, 3)

        # Check sleep durations (exponential backoff)
        # retry_delay = 2
        # attempt 1: wait = min(60, 2 * 2**0) + 0.5 = 2.5
        # attempt 2: wait = min(60, 2 * 2**1) + 0.5 = 4.5
        self.assertEqual(mock_sleep.call_count, 2)
        mock_sleep.assert_any_call(2.5)
        mock_sleep.assert_any_call(4.5)

    @patch('workflows.executors.http_request.random.uniform', return_value=0.5)
    @patch('workflows.executors.http_request.time.sleep')
    @patch('workflows.executors.http_request.urlopen')
    def test_retry_behavior_non_transient(self, mock_urlopen, mock_sleep, mock_uniform):
        # Non transient error like 404 should fail immediately
        err404 = HTTPError('http://test.com', 404, 'Not Found', {}, BytesIO(b'not found'))
        mock_urlopen.side_effect = [err404]

        exec_obj = self.make_executor({
            'url': 'http://test.com',
            'retry_on_fail': True,
            'retry_count': 3,
            'retry_delay': 2,
        })

        with self.assertRaises(RuntimeError) as context:
            exec_obj.run()
        
        self.assertIn("HTTP 404", str(context.exception))
        # urlopen was called once, no retry sleep was done
        self.assertEqual(mock_urlopen.call_count, 1)
        mock_sleep.assert_not_called()
