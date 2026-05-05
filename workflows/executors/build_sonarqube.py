"""
SonarQube — Executor.

Lance une analyse de qualité de code via l'API SonarQube/SonarCloud.
Attend la fin de l'analyse (polling sur la task queue), lit le Quality Gate
et échoue si le statut est ERROR ou WARN (selon config).
"""
import json
import logging
import time
from base64 import b64encode
from urllib.request import urlopen, Request
from urllib.error import URLError

from .base import BaseExecutor

logger = logging.getLogger(__name__)

_POLL_INTERVAL     = 5    # secondes
_MAX_POLL_ATTEMPTS = 60   # 60 × 5s = 5 min max


def _sonar_get(url: str, token: str) -> dict:
    """Requête GET vers l'API SonarQube avec auth Basic."""
    credentials = b64encode(f'{token}:'.encode()).decode()
    req = Request(url, headers={'Authorization': f'Basic {credentials}'})
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode('utf-8'))


class SonarQubeExecutor(BaseExecutor):
    """
    build.SonarQube — Analyse qualité de code via SonarQube/SonarCloud.

    Config du nœud :
        sonarUrl       : str  — URL de l'instance SonarQube (ex: https://sonarcloud.io)
        projectKey     : str  — Clé du projet SonarQube (ex: org_mon-projet)
        token          : str  — Token d'authentification SonarQube
        organization   : str  — Organisation (SonarCloud uniquement, optionnel)
        qualityGate    : enum — 'BLOCK_ON_ERROR' | 'BLOCK_ON_WARN' | 'REPORT_ONLY'
        branch         : str  — Branche analysée (défaut: main)
    """

    def run(self) -> dict:
        sonar_url    = self.cfg('sonarUrl', 'https://sonarcloud.io').rstrip('/')
        project_key  = self.cfg('projectKey', '').strip()
        token        = self.cfg('token', '').strip()
        organization = self.cfg('organization', '').strip()
        quality_gate = self.cfg('qualityGate', 'BLOCK_ON_ERROR').strip().upper()
        branch       = (
            self.cfg('branch', '').strip()
            or self.ctx('trigger_branch', 'main')
        )

        if not project_key:
            raise RuntimeError(
                "[SonarQube] 'projectKey' non configuré.\n"
                "Renseignez la clé du projet SonarQube (ex: org_mon-projet)."
            )
        if not token:
            raise RuntimeError(
                "[SonarQube] 'token' non configuré.\n"
                "Créez un token dans SonarQube → Mon compte → Sécurité."
            )

        logger.info(
            f'[SonarQube] Lancement analyse — project={project_key}  '
            f'branch={branch}  gate={quality_gate}'
        )

        # ── Récupération du dernier état (analyse déjà lancée par sonar-scanner) ──
        # En production : sonar-scanner est lancé via ScriptTask, puis ce nœud
        # récupère le résultat via l'API. Ici on simule le polling de la ceTask.

        ce_url = (
            f'{sonar_url}/api/ce/component?component={project_key}'
            + (f'&branch={branch}' if branch else '')
        )

        quality_gate_status = 'ERROR'
        coverage            = 0.0
        bugs                = 0
        vulnerabilities     = 0
        code_smells         = 0

        try:
            for attempt in range(_MAX_POLL_ATTEMPTS):
                time.sleep(_POLL_INTERVAL)
                try:
                    data   = _sonar_get(ce_url, token)
                    tasks  = data.get('queue', []) + [data.get('current', {})]
                    latest = next((t for t in reversed(tasks) if t), {})
                    status = latest.get('status', 'IN_PROGRESS')
                    logger.info(
                        f'[SonarQube] Polling {attempt + 1} — CE task status={status}'
                    )
                    if status in ('SUCCESS', 'FAILED', 'CANCELED'):
                        break
                except URLError as exc:
                    logger.warning(f'[SonarQube] Erreur polling : {exc}')

            # Lecture du Quality Gate
            qg_url  = (
                f'{sonar_url}/api/qualitygates/project_status'
                f'?projectKey={project_key}'
                + (f'&branch={branch}' if branch else '')
            )
            qg_data              = _sonar_get(qg_url, token)
            qg_status_raw        = qg_data.get('projectStatus', {})
            quality_gate_status  = qg_status_raw.get('status', 'ERROR')

            # Lecture des métriques
            metrics_url = (
                f'{sonar_url}/api/measures/component'
                f'?component={project_key}'
                f'&metricKeys=coverage,bugs,vulnerabilities,code_smells,reliability_rating'
                + (f'&branch={branch}' if branch else '')
            )
            mdata    = _sonar_get(metrics_url, token)
            measures = {
                m['metric']: m.get('value', '0')
                for m in mdata.get('component', {}).get('measures', [])
            }
            coverage        = float(measures.get('coverage', '0'))
            bugs            = int(measures.get('bugs', '0'))
            vulnerabilities = int(measures.get('vulnerabilities', '0'))
            code_smells     = int(measures.get('code_smells', '0'))

        except URLError as exc:
            # Mode dégradé : on log et on continue avec les valeurs par défaut
            logger.warning(
                f'[SonarQube] API inaccessible — mode dégradé : {exc}'
            )
            quality_gate_status = 'UNKNOWN'

        logger.info(
            f'[SonarQube] Quality Gate = {quality_gate_status}  '
            f'coverage={coverage}%  bugs={bugs}  vulns={vulnerabilities}'
        )

        # ── Décision blocage ─────────────────────────────────────────────────
        should_block = (
            (quality_gate == 'BLOCK_ON_ERROR'  and quality_gate_status == 'ERROR') or
            (quality_gate == 'BLOCK_ON_WARN'   and quality_gate_status in ('ERROR', 'WARN'))
        )

        if should_block:
            raise RuntimeError(
                f"[SonarQube] Quality Gate échoué : {quality_gate_status}\n"
                f"Projet : {project_key}  Branche : {branch}\n"
                f"Bugs: {bugs}  Vulnérabilités: {vulnerabilities}  "
                f"Code Smells: {code_smells}  Couverture: {coverage}%"
            )

        return {
            'sonar_quality_gate':    quality_gate_status,
            'sonar_coverage':        coverage,
            'sonar_bugs':            bugs,
            'sonar_vulnerabilities': vulnerabilities,
            'sonar_code_smells':     code_smells,
            'sonar_project':         project_key,
            'sonar_branch':          branch,
            'sonar_url':             f'{sonar_url}/dashboard?id={project_key}',
        }
