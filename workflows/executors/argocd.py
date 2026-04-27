"""
ArgoCD executors.
- If ARGOCD_URL + ARGOCD_TOKEN are set → real API calls.
- Otherwise                            → simulation mode.
"""
import logging
import time
import requests
from django.conf import settings
from .base import BaseExecutor

logger = logging.getLogger(__name__)


class ArgoCDDeployExecutor(BaseExecutor):
    """argocd.Deploy — trigger an ArgoCD application sync."""

    def run(self) -> dict:
        app_name  = self.cfg('applicationName', 'my-app')
        env       = self.cfg('environment', 'QA')
        sync_policy = self.cfg('syncPolicy', 'Automatic')

        argocd_url   = settings.ARGOCD_URL
        argocd_token = settings.ARGOCD_TOKEN

        if argocd_url and argocd_token:
            return self._real_deploy(app_name, argocd_url, argocd_token)

        return self._simulate_deploy(app_name, env, sync_policy)

    def _real_deploy(self, app_name: str, url: str, token: str) -> dict:
        """POST /api/v1/applications/{name}/sync"""
        endpoint = f'{url.rstrip("/")}/api/v1/applications/{app_name}/sync'
        headers  = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        logger.info(f'[ArgoCD] Syncing {app_name} → {endpoint}')
        resp = requests.post(endpoint, headers=headers, json={}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return {
            'status':      data.get('status', {}).get('sync', {}).get('status', 'Unknown'),
            'health':      data.get('status', {}).get('health', {}).get('status', 'Unknown'),
            'revision':    data.get('status', {}).get('sync', {}).get('revision', ''),
            'application': app_name,
        }

    def _simulate_deploy(self, app_name: str, env: str, policy: str) -> dict:
        logger.info(f'[ArgoCD SIMULATE] Deploying {app_name} to {env} (policy={policy})')
        time.sleep(2)
        return {
            'status':      'Synced',
            'health':      'Healthy',
            'revision':    'abc1234',
            'application': app_name,
            'environment': env,
            'mode':        'simulation',
        }


class ArgoCDRollbackExecutor(BaseExecutor):
    """argocd.Rollback — revert to a specific git revision."""

    def run(self) -> dict:
        app_name = self.cfg('applicationName', 'my-app')
        revision = self.cfg('targetRevision', 'HEAD~1')
        force    = self.cfg('force', True)

        argocd_url   = settings.ARGOCD_URL
        argocd_token = settings.ARGOCD_TOKEN

        if argocd_url and argocd_token:
            return self._real_rollback(app_name, revision, force, argocd_url, argocd_token)

        return self._simulate_rollback(app_name, revision)

    def _real_rollback(self, app_name, revision, force, url, token) -> dict:
        endpoint = f'{url.rstrip("/")}/api/v1/applications/{app_name}/rollback'
        headers  = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        payload  = {'revision': revision, 'force': force}
        logger.info(f'[ArgoCD] Rolling back {app_name} to {revision}')
        resp = requests.post(endpoint, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        return {'rolled_back_to': revision, 'application': app_name}

    def _simulate_rollback(self, app_name, revision) -> dict:
        logger.info(f'[ArgoCD SIMULATE] Rolling back {app_name} → {revision}')
        time.sleep(1.5)
        return {
            'rolled_back_to': revision,
            'application':    app_name,
            'mode':           'simulation',
        }
