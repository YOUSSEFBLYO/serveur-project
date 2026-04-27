"""
HTTP Request executor — generic outbound HTTP call.
Used by remoteScript.HttpRequest nodes.
"""
import json
import logging
import requests
from .base import BaseExecutor

logger = logging.getLogger(__name__)


class HttpRequestExecutor(BaseExecutor):
    """remoteScript.HttpRequest — make an HTTP request to any URL."""

    def run(self) -> dict:
        url      = self.cfg('url', '')
        method   = self.cfg('method', 'GET').upper()
        body_str = self.cfg('body', '')
        headers  = self.cfg('headers', {})
        username = self.cfg('username', '')
        password = self.cfg('password', '')

        if not url:
            raise ValueError('HttpRequest node: "url" is required but was empty.')

        # Build auth
        auth = (username, password) if username else None

        # Build body
        payload = None
        if body_str:
            try:
                payload = json.loads(body_str)
            except (json.JSONDecodeError, TypeError):
                payload = body_str

        logger.info(f'[HttpRequest] {method} {url}')

        resp = requests.request(
            method=method,
            url=url,
            json=payload if isinstance(payload, dict) else None,
            data=payload if isinstance(payload, str) else None,
            headers=headers or {},
            auth=auth,
            timeout=30,
        )

        try:
            resp_body = resp.json()
        except Exception:
            resp_body = resp.text[:500]

        return {
            'status_code': resp.status_code,
            'ok':          resp.ok,
            'response':    resp_body,
            'url':         url,
            'method':      method,
        }
