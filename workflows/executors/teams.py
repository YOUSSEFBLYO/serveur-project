"""
Microsoft Teams executors — send Adaptive Cards / MessageCards via webhook.
If the webhook URL is empty → simulation mode.
"""
import json
import logging
import time
import requests
from .base import BaseExecutor

logger = logging.getLogger(__name__)

_MOCK_VARS = {
    '{{release_name}}': 'kraken-v2.4.1',
    '{{version}}':      'v2.4.1',
    '{{environment}}':  'PROD',
}


def _render(template: str) -> str:
    """Replace {{variable}} placeholders with mock or real values."""
    for k, v in _MOCK_VARS.items():
        template = template.replace(k, v)
    return template


def _send_webhook(url: str, body: str | dict) -> dict:
    """POST the MessageCard body to the Teams webhook URL."""
    if isinstance(body, str):
        try:
            payload = json.loads(_render(body))
        except json.JSONDecodeError:
            payload = {'text': _render(body)}
    else:
        payload = body

    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    return {'http_status': resp.status_code, 'response': resp.text[:200]}


class TeamsAlertExecutor(BaseExecutor):
    """teams.Alert — send an alert card to a Teams channel."""

    def run(self) -> dict:
        url  = self.cfg('url', '')
        body = self.cfg('body', '{"text": "Kraken Alert"}')

        if not url:
            logger.info(f'[Teams SIMULATE] Alert — no webhook URL configured')
            time.sleep(1)
            return {'sent': False, 'mode': 'simulation', 'body_preview': str(body)[:100]}

        logger.info(f'[Teams] Sending Alert to {url[:50]}...')
        return {**_send_webhook(url, body), 'sent': True, 'type': 'alert'}


class TeamsNotificationExecutor(BaseExecutor):
    """teams.Notification — send a success notification card."""

    def run(self) -> dict:
        url  = self.cfg('url', '')
        body = self.cfg('body', '{"text": "Kraken Notification"}')

        if not url:
            logger.info('[Teams SIMULATE] Notification — no webhook URL')
            time.sleep(1)
            return {'sent': False, 'mode': 'simulation', 'body_preview': str(body)[:100]}

        logger.info(f'[Teams] Sending Notification to {url[:50]}...')
        return {**_send_webhook(url, body), 'sent': True, 'type': 'notification'}


class TeamsApprovalExecutor(BaseExecutor):
    """
    teams.Approval — blocking node that sends an approval request.
    In real mode it posts a card; the actual approval gate is handled
    by polling or a callback (out of scope here → simulated approval).
    """

    def run(self) -> dict:
        url   = self.cfg('url', '')
        email = self.cfg('approverEmail', 'approver@company.com')
        msg   = _render(self.cfg('message', 'Approval required.'))

        if not url:
            logger.info(f'[Teams SIMULATE] Approval requested from {email}')
            time.sleep(2)  # Simulate waiting
            return {'approved': True, 'approver': email, 'mode': 'simulation'}

        body = {
            '@type':      'MessageCard',
            '@context':   'http://schema.org/extensions',
            'themeColor': 'f97316',
            'summary':    'Approval Required',
            'sections': [{'activityTitle': 'Kraken — Approval Required', 'activityText': msg}],
            'potentialAction': [{'@type': 'OpenUri', 'name': 'Approve', 'targets': [{'os': 'default', 'uri': '#'}]}],
        }

        result = _send_webhook(url, body)
        # In production, implement callback/polling for real approval
        time.sleep(1)
        return {**result, 'approved': True, 'approver': email, 'type': 'approval'}
