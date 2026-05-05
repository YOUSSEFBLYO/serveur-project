"""
Dynatrace Monitoring — Executor.

Interagit avec l'API Dynatrace pour :
  - Créer un événement de déploiement (CUSTOM_DEPLOYMENT)
  - Vérifier le statut des problèmes actifs sur une application
  - Évaluer le score SLO post-déploiement
"""
import json
import logging
import time
from urllib.request import urlopen, Request
from urllib.error import URLError

from .base import BaseExecutor

logger = logging.getLogger(__name__)

_POLL_INTERVAL     = 10
_MAX_POLL_ATTEMPTS = 18   # 18 × 10s = 3 min


def _dt_request(url: str, token: str, method: str = 'GET',
                data: dict | None = None) -> dict:
    """Requête vers l'API Dynatrace."""
    body = json.dumps(data).encode('utf-8') if data else None
    req  = Request(
        url, data=body, method=method,
        headers={
            'Authorization': f'Api-Token {token}',
            'Content-Type':  'application/json',
        },
    )
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode('utf-8'))


class DynatraceMonitoringExecutor(BaseExecutor):
    """
    observe.Dynatrace — Monitoring et observabilité via Dynatrace.

    Config du nœud :
        tenantUrl      : str  — URL du tenant Dynatrace (ex: https://abc12345.live.dynatrace.com)
        apiToken       : str  — Token API Dynatrace (scope: events.ingest, problems.read)
        entitySelector : str  — Sélecteur d'entité (ex: type(SERVICE),tag(env:prod))
        action         : enum — 'DEPLOYMENT_EVENT' | 'CHECK_PROBLEMS' | 'EVALUATE_SLO'
        sloId          : str  — ID du SLO à évaluer (pour action EVALUATE_SLO)
        minSloScore    : str  — Score SLO minimum requis (défaut: 95)
        waitMinutes    : str  — Minutes à attendre avant vérif post-déploiement
    """

    def run(self) -> dict:
        tenant_url      = self.cfg('tenantUrl', '').rstrip('/').strip()
        api_token       = self.cfg('apiToken', '').strip()
        entity_selector = self.cfg('entitySelector', '').strip()
        action          = self.cfg('action', 'DEPLOYMENT_EVENT').strip().upper()
        slo_id          = self.cfg('sloId', '').strip()
        min_slo_score   = float(self.cfg('minSloScore', '95') or '95')
        wait_minutes    = int(self.cfg('waitMinutes', '0') or '0')

        if not tenant_url:
            raise RuntimeError(
                "[Dynatrace] 'tenantUrl' non configuré.\n"
                "Ex: https://abc12345.live.dynatrace.com"
            )
        if not api_token:
            raise RuntimeError(
                "[Dynatrace] 'apiToken' non configuré.\n"
                "Créez un token API dans Dynatrace → Settings → Integration → API tokens."
            )

        if action == 'DEPLOYMENT_EVENT':
            return self._send_deployment_event(tenant_url, api_token, entity_selector)
        elif action == 'CHECK_PROBLEMS':
            return self._check_problems(tenant_url, api_token, entity_selector, wait_minutes)
        elif action == 'EVALUATE_SLO':
            return self._evaluate_slo(tenant_url, api_token, slo_id, min_slo_score)
        else:
            raise RuntimeError(f"[Dynatrace] Action inconnue : {action}")

    def _send_deployment_event(self, tenant_url: str, token: str,
                                entity_selector: str) -> dict:
        """Envoie un événement de déploiement CUSTOM_DEPLOYMENT à Dynatrace."""
        commit_sha  = self.ctx('commit_sha', 'unknown')[:8]
        branch      = self.ctx('trigger_branch', self.ctx('branch', 'unknown'))
        image_tag   = self.ctx('docker_tag', '')
        deploy_name = f'Workflow deploy — {branch} ({commit_sha})'

        event_payload = {
            'eventType': 'CUSTOM_DEPLOYMENT',
            'title': deploy_name,
            'entitySelector': entity_selector or 'type(SERVICE)',
            'properties': {
                'dt.event.deployment.name':          deploy_name,
                'dt.event.deployment.version':       image_tag or commit_sha,
                'dt.event.deployment.release_stage': self.ctx('trigger_env', 'production'),
                'commit_sha':                        self.ctx('commit_sha', ''),
                'branch':                            branch,
            },
        }

        url = f'{tenant_url}/api/v2/events/ingest'
        logger.info(f'[Dynatrace] Envoi événement déploiement → {deploy_name}')

        try:
            resp = _dt_request(url, token, 'POST', event_payload)
            event_id = resp.get('storedEventIds', ['?'])[0]
            logger.info(f'[Dynatrace] Événement créé — id={event_id}')
        except URLError as exc:
            logger.warning(f'[Dynatrace] API inaccessible : {exc} — mode dégradé')
            event_id = 'N/A'

        return {
            'dynatrace_action':   'DEPLOYMENT_EVENT',
            'dynatrace_event_id': event_id,
            'dynatrace_deploy':   deploy_name,
        }

    def _check_problems(self, tenant_url: str, token: str,
                         entity_selector: str, wait_minutes: int) -> dict:
        """Vérifie les problèmes actifs sur les entités ciblées."""
        if wait_minutes > 0:
            logger.info(f'[Dynatrace] Attente de {wait_minutes} min avant vérification...')
            time.sleep(wait_minutes * 60)

        url = (
            f'{tenant_url}/api/v2/problems?problemSelector=status("OPEN")'
            + (f'&entitySelector={entity_selector}' if entity_selector else '')
        )
        logger.info('[Dynatrace] Vérification des problèmes actifs...')

        try:
            data        = _dt_request(url, token)
            total_count = data.get('totalCount', 0)
            problems    = data.get('problems', [])
        except URLError as exc:
            logger.warning(f'[Dynatrace] API inaccessible : {exc}')
            total_count, problems = 0, []

        high_impact = [p for p in problems if p.get('severityLevel') in ('AVAILABILITY', 'ERROR')]

        logger.info(
            f'[Dynatrace] Problèmes ouverts : {total_count}  '
            f'impact élevé : {len(high_impact)}'
        )

        if high_impact:
            raise RuntimeError(
                f"[Dynatrace] {len(high_impact)} problème(s) critique(s) détecté(s) "
                f"après déploiement !\n"
                + '\n'.join(p.get('title', '?') for p in high_impact[:5])
            )

        return {
            'dynatrace_action':          'CHECK_PROBLEMS',
            'dynatrace_problems_count':  total_count,
            'dynatrace_high_impact':     len(high_impact),
            'dynatrace_healthy':         total_count == 0,
        }

    def _evaluate_slo(self, tenant_url: str, token: str,
                       slo_id: str, min_score: float) -> dict:
        """Évalue un SLO Dynatrace et échoue si le score est insuffisant."""
        if not slo_id:
            raise RuntimeError(
                "[Dynatrace] 'sloId' requis pour l'action EVALUATE_SLO.\n"
                "Trouvez l'ID dans Dynatrace → Service-level objectives."
            )

        url = f'{tenant_url}/api/v2/slo/{slo_id}'
        logger.info(f'[Dynatrace] Évaluation SLO {slo_id}')

        try:
            data      = _dt_request(url, token)
            score     = float(data.get('evaluatedPercentage', 0))
            status    = data.get('status', 'UNKNOWN')
            slo_name  = data.get('name', slo_id)
        except URLError as exc:
            logger.warning(f'[Dynatrace] API SLO inaccessible : {exc}')
            score, status, slo_name = 0.0, 'UNKNOWN', slo_id

        logger.info(
            f'[Dynatrace] SLO "{slo_name}" — score={score}%  status={status}  '
            f'seuil_requis={min_score}%'
        )

        if score < min_score:
            raise RuntimeError(
                f"[Dynatrace] SLO insuffisant : {score:.1f}% < {min_score}%\n"
                f"SLO : {slo_name}  Statut : {status}"
            )

        return {
            'dynatrace_action':    'EVALUATE_SLO',
            'dynatrace_slo_name':  slo_name,
            'dynatrace_slo_score': score,
            'dynatrace_slo_ok':    score >= min_score,
        }
