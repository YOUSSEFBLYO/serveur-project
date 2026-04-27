"""
Dynatrace Monitor executor.
Queries the Dynatrace Metrics API v2 to check service health.
Falls back to simulation when no DT credentials are configured.
"""
import logging
import time
import requests
from .base import BaseExecutor

logger = logging.getLogger(__name__)

# Dynatrace credentials can be added to .env if needed
import os
DT_URL   = os.getenv('DYNATRACE_URL', '')
DT_TOKEN = os.getenv('DYNATRACE_TOKEN', '')


class DynatraceMonitorExecutor(BaseExecutor):
    """dynatrace.Monitor — check entity health and alert status."""

    def run(self) -> dict:
        entity_id   = self.cfg('entityId', 'SERVICE-000000000000001')
        timeframe   = self.cfg('timeframe', 'Last 30 mins')
        metrics     = self.cfg('metrics', ['health', 'errors', 'latency'])
        fail_on_alert = self.cfg('failOnAlert', True)

        if DT_URL and DT_TOKEN:
            return self._real_check(entity_id, timeframe, metrics, fail_on_alert)

        return self._simulate_check(entity_id, metrics)

    def _real_check(self, entity_id, timeframe, metrics, fail_on_alert) -> dict:
        headers  = {'Authorization': f'Api-Token {DT_TOKEN}'}
        endpoint = f'{DT_URL.rstrip("/")}/api/v2/entities/{entity_id}'
        logger.info(f'[Dynatrace] Checking entity {entity_id}')

        resp = requests.get(endpoint, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        health = data.get('properties', {}).get('status', 'UNKNOWN')
        has_alert = health not in ('AVAILABLE', 'HEALTHY')

        if fail_on_alert and has_alert:
            raise RuntimeError(f'Dynatrace health alert on {entity_id}: {health}')

        return {
            'entity_id': entity_id,
            'health':    health,
            'has_alert': has_alert,
            'metrics':   metrics,
        }

    def _simulate_check(self, entity_id, metrics) -> dict:
        logger.info(f'[Dynatrace SIMULATE] Monitoring {entity_id}')
        time.sleep(1.5)
        return {
            'entity_id':  entity_id,
            'health':     'AVAILABLE',
            'error_rate': '0.02%',
            'latency_p99': '145ms',
            'alerts':     0,
            'metrics':    metrics,
            'mode':       'simulation',
        }
