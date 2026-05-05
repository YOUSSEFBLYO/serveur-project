"""
GitLab Pipeline — Executor.

Déclenche un pipeline CI/CD GitLab via l'API REST.
Supporte les pipelines sur branche, tag, ou avec variables d'entrée.
Attend la fin du pipeline (polling) et retourne le statut.
"""
import json
import logging
import time
from urllib.parse import urljoin
from urllib.request import urlopen, Request
from urllib.error import URLError

from .base import BaseExecutor

logger = logging.getLogger(__name__)

_MAX_POLL_ATTEMPTS = 60   # 60 × 5s = 5 minutes max
_POLL_INTERVAL     = 5    # secondes


def _gitlab_request(url: str, token: str, method: str = 'GET',
                    data: dict | None = None) -> dict:
    """Effectue une requête HTTP à l'API GitLab."""
    body = json.dumps(data).encode('utf-8') if data else None
    req  = Request(
        url,
        data=body,
        method=method,
        headers={
            'PRIVATE-TOKEN': token,
            'Content-Type':  'application/json',
        },
    )
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode('utf-8'))


class GitLabPipelineExecutor(BaseExecutor):
    """
    build.GitLabPipeline — Déclenchement de pipeline CI/CD GitLab.

    Config du nœud :
        gitlabUrl    : str  — URL de base de GitLab (ex: https://gitlab.com)
        projectId    : str  — ID numérique ou chemin encodé du projet
        token        : str  — Token d'accès personnel (PAT) avec scope api
        ref          : str  — Branche ou tag à pipeliner (défaut: main)
        variables    : text — Variables JSON à passer au pipeline (optionnel)
        waitForEnd   : bool — Attendre la fin du pipeline avant de continuer
    """

    def run(self) -> dict:
        base_url    = self.cfg('gitlabUrl', 'https://gitlab.com').rstrip('/')
        project_id  = self.cfg('projectId', '').strip()
        token       = self.cfg('token', '').strip()
        ref         = self.cfg('ref', 'main').strip() or 'main'
        variables   = self.cfg('variables', '{}').strip()
        wait_end    = self.cfg('waitForEnd', True)

        if not project_id:
            raise RuntimeError(
                "[GitLabPipeline] 'projectId' non configuré.\n"
                "Renseignez l'ID numérique ou le chemin encodé (ex: 42 ou org%2Frepo)."
            )
        if not token:
            raise RuntimeError(
                "[GitLabPipeline] 'token' non configuré.\n"
                "Fournissez un Personal Access Token avec le scope 'api'."
            )

        # Parsing des variables
        pipeline_vars = []
        if variables and variables != '{}':
            try:
                vdict = json.loads(variables)
                if isinstance(vdict, dict):
                    pipeline_vars = [
                        {'key': k, 'value': str(v), 'variable_type': 'env_var'}
                        for k, v in vdict.items()
                    ]
            except (json.JSONDecodeError, ValueError):
                logger.warning('[GitLabPipeline] variables JSON invalides — ignorées')

        api_base = f'{base_url}/api/v4/projects/{project_id}'
        trigger_url = f'{api_base}/pipeline'

        payload = {'ref': ref}
        if pipeline_vars:
            payload['variables'] = pipeline_vars

        logger.info(
            f'[GitLabPipeline] Déclenchement pipeline — '
            f'project={project_id}  ref={ref}  vars={len(pipeline_vars)}'
        )

        try:
            resp = _gitlab_request(trigger_url, token, 'POST', payload)
        except URLError as exc:
            raise RuntimeError(
                f"[GitLabPipeline] Impossible de déclencher le pipeline GitLab.\n"
                f"URL: {trigger_url}\nDétail: {exc}"
            )

        pipeline_id  = resp.get('id')
        pipeline_url = resp.get('web_url', '')
        status       = resp.get('status', 'created')

        logger.info(
            f'[GitLabPipeline] Pipeline créé — id={pipeline_id}  '
            f'statut={status}  url={pipeline_url}'
        )

        if not wait_end:
            return {
                'pipeline_id':     pipeline_id,
                'pipeline_url':    pipeline_url,
                'pipeline_status': status,
                'pipeline_ref':    ref,
            }

        # ── Polling jusqu'à la fin du pipeline ────────────────────────────────
        status_url = f'{api_base}/pipelines/{pipeline_id}'
        terminal   = {'success', 'failed', 'canceled', 'skipped'}

        for attempt in range(_MAX_POLL_ATTEMPTS):
            time.sleep(_POLL_INTERVAL)
            try:
                status_resp = _gitlab_request(status_url, token)
                status      = status_resp.get('status', 'running')
                logger.info(
                    f'[GitLabPipeline] Polling {attempt + 1}/{_MAX_POLL_ATTEMPTS} '
                    f'— statut={status}'
                )
                if status in terminal:
                    break
            except URLError as exc:
                logger.warning(f'[GitLabPipeline] Erreur polling : {exc}')

        if status not in ('success', 'skipped'):
            raise RuntimeError(
                f"[GitLabPipeline] Le pipeline {pipeline_id} s'est terminé avec "
                f"le statut : {status}\nURL: {pipeline_url}"
            )

        return {
            'pipeline_id':       pipeline_id,
            'pipeline_url':      pipeline_url,
            'pipeline_status':   status,
            'pipeline_ref':      ref,
            'pipeline_success':  status == 'success',
        }
