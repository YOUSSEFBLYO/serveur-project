"""
n8n Orchestration executor.
Triggers an external n8n workflow via a webhook URL.
"""
import logging
import requests
import time
from .base import BaseExecutor

logger = logging.getLogger(__name__)


class N8nTriggerExecutor(BaseExecutor):
    """n8n.Trigger — calls an n8n webhook."""

    def run(self) -> dict:
        webhook_url = self.cfg('webhookUrl', '')
        payload     = self.cfg('payload', {})
        wait        = self.cfg('waitForCallback', True)

        if not webhook_url:
            logger.info('[n8n SIMULATE] Triggering external n8n workflow')
            time.sleep(1.5)
            return {
                'triggered': True,
                'waited': wait,
                'mode': 'simulation',
            }

        logger.info(f'[n8n] Triggering webhook: {webhook_url}')
        
        resp = requests.post(webhook_url, json=payload, timeout=30)
        resp.raise_for_status()
        
        data = {}
        try:
            data = resp.json()
        except:
            data = {'response': resp.text[:200]}

        return {
            'triggered': True,
            'n8n_response': data,
            'waited': wait, # Actual asynchronous callback wait requires more complex engine state
        }
