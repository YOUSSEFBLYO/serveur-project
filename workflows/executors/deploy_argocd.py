"""
ArgoCD Deploy — Executor.

Déclenche un déploiement via l'API REST ArgoCD.
Supporte la synchronisation d'une application ArgoCD et l'attente
de la fin du déploiement (health check).
"""
import json
import logging
import time
from urllib.request import urlopen, Request
from urllib.error import URLError

from .base import BaseExecutor

logger = logging.getLogger(__name__)

_POLL_INTERVAL     = 5
_MAX_POLL_ATTEMPTS = 72   # 72 × 5s = 6 min


def _argocd_request(url: str, token: str, method: str = 'GET',
                    data: dict | None = None) -> dict:
    """Effectue une requête HTTP vers l'API ArgoCD."""
    body = json.dumps(data).encode('utf-8') if data else None
    req  = Request(
        url, data=body, method=method,
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type':  'application/json',
        },
    )
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode('utf-8'))


class ArgoCDDeployExecutor(BaseExecutor):
    """
    deploy.ArgoCD — Déploiement via ArgoCD.

    Config du nœud :
        argocdUrl    : str  — URL de l'instance ArgoCD (ex: https://argocd.company.com)
        token        : str  — Token d'authentification ArgoCD
        appName      : str  — Nom de l'application ArgoCD à synchroniser
        revision     : str  — Révision Git à déployer (défaut: HEAD)
        prune        : bool — Supprimer les ressources obsolètes
        force        : bool — Forcer le sync même si l'app est saine
        waitHealthy  : bool — Attendre que l'app soit healthy avant de continuer
    """

    def run(self) -> dict:
        argocd_url   = self.cfg('argocdUrl', '').rstrip('/').strip()
        token        = self.cfg('token', '').strip()
        app_name     = self.cfg('appName', '').strip()
        revision     = (
            self.cfg('revision', '').strip()
            or self.ctx('commit_sha', 'HEAD')
        )
        prune        = self.cfg('prune', True)
        force        = self.cfg('force', False)
        wait_healthy = self.cfg('waitHealthy', True)

        if not argocd_url:
            raise RuntimeError(
                "[ArgoCD] 'argocdUrl' non configuré.\n"
                "Renseignez l'URL de votre instance ArgoCD."
            )
        if not token:
            raise RuntimeError(
                "[ArgoCD] 'token' non configuré.\n"
                "Générez un token dans ArgoCD → Settings → Accounts."
            )
        if not app_name:
            raise RuntimeError(
                "[ArgoCD] 'appName' non configuré.\n"
                "Renseignez le nom de l'application ArgoCD à synchroniser."
            )

        logger.info(
            f'[ArgoCD] Synchronisation de "{app_name}" — '
            f'revision={revision}  prune={prune}  force={force}'
        )

        sync_url  = f'{argocd_url}/api/v1/applications/{app_name}/sync'
        sync_body = {
            'revision': revision,
            'prune':    prune,
            'dryRun':   False,
        }
        if force:
            sync_body['strategy'] = {'apply': {'force': True}}

        try:
            resp = _argocd_request(sync_url, token, 'POST', sync_body)
        except URLError as exc:
            raise RuntimeError(
                f"[ArgoCD] Impossible de déclencher la synchronisation.\n"
                f"URL: {sync_url}\nDétail: {exc}"
            )

        op_state    = resp.get('operation', {})
        sync_status = op_state.get('phase', 'Running')
        logger.info(f'[ArgoCD] Sync déclenchée — phase={sync_status}')

        if not wait_healthy:
            return {
                'argocd_app':        app_name,
                'argocd_revision':   revision,
                'argocd_sync_phase': sync_status,
                'argocd_healthy':    False,
                'argocd_url':        f'{argocd_url}/applications/{app_name}',
            }

        # ── Polling health check ────────────────────────────────────────────
        app_url = f'{argocd_url}/api/v1/applications/{app_name}'

        for attempt in range(_MAX_POLL_ATTEMPTS):
            time.sleep(_POLL_INTERVAL)
            try:
                app_data   = _argocd_request(app_url, token)
                health     = app_data.get('status', {}).get('health', {}).get('status', 'Unknown')
                sync_phase = app_data.get('status', {}).get('operationState', {}).get('phase', 'Running')

                logger.info(
                    f'[ArgoCD] Polling {attempt + 1} — '
                    f'health={health}  sync_phase={sync_phase}'
                )

                if sync_phase in ('Succeeded', 'Failed', 'Error') and health in ('Healthy', 'Degraded'):
                    break
            except URLError as exc:
                logger.warning(f'[ArgoCD] Erreur polling : {exc}')

        # Lecture finale
        try:
            final      = _argocd_request(app_url, token)
            health     = final.get('status', {}).get('health', {}).get('status', 'Unknown')
            sync_phase = final.get('status', {}).get('operationState', {}).get('phase', 'Unknown')
        except URLError:
            health, sync_phase = 'Unknown', 'Unknown'

        if health != 'Healthy' or sync_phase != 'Succeeded':
            raise RuntimeError(
                f"[ArgoCD] Déploiement de '{app_name}' échoué.\n"
                f"Health: {health}  Sync phase: {sync_phase}\n"
                f"URL: {argocd_url}/applications/{app_name}"
            )

        logger.info(f'[ArgoCD] Application "{app_name}" déployée avec succès ✓')

        return {
            'argocd_app':        app_name,
            'argocd_revision':   revision,
            'argocd_sync_phase': sync_phase,
            'argocd_healthy':    health == 'Healthy',
            'argocd_url':        f'{argocd_url}/applications/{app_name}',
        }
