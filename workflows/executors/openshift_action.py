"""
OpenShift Action — Executor.

Effectue des opérations directes sur un cluster OpenShift :
restart (rollout restart), scale, get_pods, get_status.
Utilise les tokens DEV/PPRD/PROD du .env Kraken ou le CLI oc.
"""
import json
import logging
import os
import subprocess
import time
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

from .base import BaseExecutor

logger = logging.getLogger(__name__)

_OC_TOKENS = {
    'DEV':  os.environ.get('OC_TOKEN_DEV',  ''),
    'PPRD': os.environ.get('OC_TOKEN_PPRD', ''),
    'PROD': os.environ.get('OC_TOKEN_PROD', ''),
}
_OC_URLS = {
    'DEV':  os.environ.get('OC_URL_DEV',  ''),
    'PPRD': os.environ.get('OC_URL_PPRD', ''),
    'PROD': os.environ.get('OC_URL_PROD', ''),
}


def _oc_api(url: str, token: str, path: str, method: str = 'GET', data: dict = None) -> dict:
    full_url = f"{url.rstrip('/')}{path}"
    body = json.dumps(data).encode('utf-8') if data else None
    req  = Request(full_url, data=body, method=method, headers={
        'Authorization': f'Bearer {token}',
        'Content-Type':  'application/json',
        'Accept':        'application/json',
    })
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _oc_cli(args: list) -> str:
    result = subprocess.run(['oc'] + args, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(
            f"[OpenShift] oc échoué (rc={result.returncode})\n{result.stderr.strip()}"
        )
    return result.stdout.strip()


class OpenShiftActionExecutor(BaseExecutor):
    """
    openshift.Action — Opérations sur un cluster OpenShift.

    Config du nœud :
        environment  : enum   — 'DEV' | 'PPRD' | 'PROD'
        ocUrl        : str    — URL OpenShift (override .env)
        ocToken      : str    — Token Bearer (override .env)
        namespace    : str    — Namespace/Project cible
        action       : enum   — 'restart' | 'scale' | 'get_pods' | 'get_status'
        resourceType : enum   — 'deployment' | 'deploymentconfig' | 'statefulset'
        resourceName : str    — Nom de la ressource
        replicas     : number — Réplicas cibles (pour scale)
        outputKey    : str    — Clé de sortie contexte
    """

    def run(self) -> dict:
        environment   = self.cfg('environment',  'DEV').strip().upper()
        oc_url        = (self.cfg('ocUrl', '') or _OC_URLS.get(environment, '')).strip()
        oc_token      = (self.cfg('ocToken', '') or _OC_TOKENS.get(environment, '')).strip()
        namespace     = self.cfg('namespace',    '').strip()
        action        = self.cfg('action',       'get_pods').strip().lower()
        resource_type = self.cfg('resourceType', 'deployment').strip().lower()
        resource_name = self.cfg('resourceName', '').strip()
        replicas      = int(self.cfg('replicas', 1))
        output_key    = self.cfg('outputKey', 'openshift_result').strip() or 'openshift_result'

        if not namespace:
            raise RuntimeError("[OpenShift] 'namespace' est obligatoire.")

        logger.info(
            f'[OpenShift] env={environment}  ns={namespace}  '
            f'action={action}  {resource_type}/{resource_name or "*"}'
        )

        # GET_PODS
        if action == 'get_pods':
            if oc_url and oc_token:
                data = _oc_api(oc_url, oc_token, f'/api/v1/namespaces/{namespace}/pods')
            else:
                data = json.loads(_oc_cli(['get', 'pods', '-n', namespace, '-o', 'json']))
            pods = [
                {
                    'name':  p['metadata']['name'],
                    'phase': p['status'].get('phase', 'Unknown'),
                    'ready': all(
                        c.get('ready', False)
                        for c in p['status'].get('containerStatuses', [])
                    ),
                }
                for p in data.get('items', [])
            ]
            running = sum(1 for p in pods if p['phase'] == 'Running')
            logger.info(f'[OpenShift] {len(pods)} pods — {running} Running ✓')
            return {
                output_key: {'action': 'get_pods', 'namespace': namespace,
                             'pods': pods, 'total': len(pods), 'running': running},
                'openshift_pods_count':   len(pods),
                'openshift_pods_running': running,
            }

        # RESTART
        elif action == 'restart':
            if not resource_name:
                raise RuntimeError("[OpenShift] 'resourceName' obligatoire pour restart.")
            if oc_url and oc_token:
                import datetime
                patch = {'spec': {'template': {'metadata': {'annotations': {
                    'kubectl.kubernetes.io/restartedAt': datetime.datetime.utcnow().isoformat()
                }}}}}
                plural = resource_type + 's'
                _oc_api(oc_url, oc_token,
                        f'/apis/apps/v1/namespaces/{namespace}/{plural}/{resource_name}',
                        'PATCH', patch)
            else:
                _oc_cli(['rollout', 'restart', f'{resource_type}/{resource_name}',
                         '-n', namespace])
            logger.info(f'[OpenShift] Restart {resource_type}/{resource_name} ✓')
            time.sleep(2)
            return {
                output_key: {'action': 'restart', 'namespace': namespace,
                             'resource_type': resource_type, 'resource_name': resource_name,
                             'environment': environment, 'status': 'restarted'},
                'openshift_restart_resource': f'{resource_type}/{resource_name}',
            }

        # SCALE
        elif action == 'scale':
            if not resource_name:
                raise RuntimeError("[OpenShift] 'resourceName' obligatoire pour scale.")
            if oc_url and oc_token:
                plural = resource_type + 's'
                _oc_api(oc_url, oc_token,
                        f'/apis/apps/v1/namespaces/{namespace}/{plural}/{resource_name}/scale',
                        'PATCH', {'spec': {'replicas': replicas}})
            else:
                _oc_cli(['scale', f'{resource_type}/{resource_name}',
                         f'--replicas={replicas}', '-n', namespace])
            logger.info(f'[OpenShift] Scale → {replicas} réplicas ✓')
            return {
                output_key: {'action': 'scale', 'namespace': namespace,
                             'resource_type': resource_type, 'resource_name': resource_name,
                             'replicas': replicas, 'environment': environment},
                'openshift_replicas': replicas,
            }

        # GET_STATUS
        elif action == 'get_status':
            if not resource_name:
                raise RuntimeError("[OpenShift] 'resourceName' obligatoire pour get_status.")
            if oc_url and oc_token:
                plural = resource_type + 's'
                data   = _oc_api(oc_url, oc_token,
                                 f'/apis/apps/v1/namespaces/{namespace}/{plural}/{resource_name}')
            else:
                data = json.loads(
                    _oc_cli(['get', f'{resource_type}/{resource_name}',
                             '-n', namespace, '-o', 'json'])
                )
            spec      = data.get('spec', {})
            status    = data.get('status', {})
            desired   = spec.get('replicas', 0)
            ready     = status.get('readyReplicas', 0)
            available = status.get('availableReplicas', 0)
            healthy   = ready == desired and desired > 0
            logger.info(
                f'[OpenShift] {resource_type}/{resource_name} — '
                f'desired={desired} ready={ready} healthy={healthy}'
            )
            return {
                output_key: {'action': 'get_status', 'namespace': namespace,
                             'resource_type': resource_type, 'resource_name': resource_name,
                             'desired': desired, 'ready': ready, 'available': available,
                             'healthy': healthy, 'environment': environment},
                'openshift_healthy': healthy,
                'openshift_ready':   ready,
            }

        else:
            raise RuntimeError(
                f"[OpenShift] Action inconnue : '{action}'.\n"
                "Valeurs : restart | scale | get_pods | get_status"
            )
