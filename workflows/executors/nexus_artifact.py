"""
Nexus — Vérification d'artefact — Executor.

Vérifie qu'une image Docker ou un artefact Maven/npm existe bien
dans le registre Nexus avant un déploiement.

Kraken pousse et lit déjà dans Nexus (172.29.7.156:8081).
"""
import json
import logging
import os
from urllib.request import urlopen, Request
from urllib.parse import urlencode, quote
from urllib.error import URLError, HTTPError
import base64

from .base import BaseExecutor

logger = logging.getLogger(__name__)

_NEXUS_URL  = os.environ.get('NEXUS_URL',  'http://172.29.7.156:8081')
_NEXUS_USER = os.environ.get('NEXUS_USER', '')
_NEXUS_PASS = os.environ.get('NEXUS_PASS', '')


def _nexus_request(url: str, user: str, password: str, path: str) -> dict:
    """Appel REST Nexus avec Basic Auth."""
    creds = base64.b64encode(f'{user}:{password}'.encode()).decode() if user else None
    full  = f"{url.rstrip('/')}{path}"
    headers = {'Accept': 'application/json'}
    if creds:
        headers['Authorization'] = f'Basic {creds}'
    req = Request(full, headers=headers)
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode('utf-8'))


class NexusArtifactExecutor(BaseExecutor):
    """
    nexus.Artifact — Vérification d'artefact dans Nexus.

    Config du nœud :
        nexusUrl     : str  — URL Nexus (défaut: NEXUS_URL du .env)
        nexusUser    : str  — Login Nexus (override .env)
        nexusPass    : str  — Mot de passe Nexus (override .env)
        repository   : str  — Nom du repository Nexus (ex: docker-hosted)
        artifactType : enum — 'docker' | 'maven2' | 'npm' | 'raw'
        imageName    : str  — Nom de l'image Docker (ex: myorg/myapp)
        imageTag     : str  — Tag Docker (ex: 1.2.3)
        groupId      : str  — GroupId Maven (ex: com.mycompany)
        artifactId   : str  — ArtifactId Maven
        version      : str  — Version de l'artefact
        blockIfMissing : bool — Bloquer si artefact absent (défaut: true)
        outputKey    : str  — Clé de sortie (défaut: nexus_result)
    """

    def run(self) -> dict:
        nexus_url      = (self.cfg('nexusUrl',  '') or _NEXUS_URL).strip()
        nexus_user     = (self.cfg('nexusUser', '') or _NEXUS_USER).strip()
        nexus_pass     = (self.cfg('nexusPass', '') or _NEXUS_PASS).strip()
        repository     = self.cfg('repository',     '').strip()
        artifact_type  = self.cfg('artifactType',   'docker').strip().lower()
        image_name     = self.cfg('imageName',       '').strip()
        image_tag      = self.cfg('imageTag',        'latest').strip()
        group_id       = self.cfg('groupId',         '').strip()
        artifact_id    = self.cfg('artifactId',      '').strip()
        version        = self.cfg('version',         '').strip()
        block_if_missing = self.cfg('blockIfMissing', True)
        output_key     = self.cfg('outputKey', 'nexus_result').strip() or 'nexus_result'

        if not nexus_url:
            raise RuntimeError("[Nexus] 'nexusUrl' non configuré.")
        if not repository:
            raise RuntimeError(
                "[Nexus] 'repository' non configuré.\n"
                "Renseignez le nom du repository Nexus (ex: docker-hosted)."
            )

        logger.info(
            f'[Nexus] Vérification artefact — type={artifact_type}  '
            f'repo={repository}  url={nexus_url}'
        )

        found     = False
        asset_url = ''
        details   = {}

        # ── Docker ───────────────────────────────────────────────────────────
        if artifact_type == 'docker':
            if not image_name:
                raise RuntimeError("[Nexus] 'imageName' obligatoire pour la vérification Docker.")

            # Nexus Search API v1 pour les composants Docker
            params = {
                'repository': repository,
                'name':       image_name,
                'version':    image_tag,
                'sort':       'version',
            }
            search_path = f'/service/rest/v1/search?{urlencode(params)}'

            try:
                resp = _nexus_request(nexus_url, nexus_user, nexus_pass, search_path)
            except HTTPError as e:
                if e.code == 404:
                    resp = {'items': []}
                else:
                    raise RuntimeError(
                        f"[Nexus] Erreur API Nexus (HTTP {e.code}).\nURL: {nexus_url}{search_path}"
                    )
            except URLError as exc:
                raise RuntimeError(
                    f"[Nexus] Impossible de contacter Nexus.\n"
                    f"URL: {nexus_url}\nDétail: {exc}"
                )

            items = resp.get('items', [])
            found = len(items) > 0

            if found:
                item      = items[0]
                asset_url = (
                    item.get('assets', [{}])[0].get('downloadUrl', '')
                    if item.get('assets') else ''
                )
                details = {
                    'name':       item.get('name', image_name),
                    'version':    item.get('version', image_tag),
                    'repository': item.get('repository', repository),
                    'format':     item.get('format', 'docker'),
                    'url':        asset_url,
                }
                logger.info(
                    f'[Nexus] Image trouvée : {image_name}:{image_tag} '
                    f'dans {repository} ✓'
                )
            else:
                logger.warning(
                    f'[Nexus] Image introuvable : {image_name}:{image_tag} '
                    f'dans {repository}'
                )

        # ── Maven2 ───────────────────────────────────────────────────────────
        elif artifact_type == 'maven2':
            if not group_id or not artifact_id:
                raise RuntimeError(
                    "[Nexus] 'groupId' et 'artifactId' obligatoires pour Maven."
                )
            params = {
                'repository': repository,
                'group':      group_id,
                'name':       artifact_id,
            }
            if version:
                params['version'] = version
            search_path = f'/service/rest/v1/search?{urlencode(params)}'

            try:
                resp  = _nexus_request(nexus_url, nexus_user, nexus_pass, search_path)
                items = resp.get('items', [])
                found = len(items) > 0
                if found:
                    item    = items[0]
                    details = {
                        'group':      item.get('group', group_id),
                        'name':       item.get('name', artifact_id),
                        'version':    item.get('version', version),
                        'repository': item.get('repository', repository),
                        'format':     'maven2',
                    }
            except (URLError, HTTPError) as exc:
                raise RuntimeError(f"[Nexus] Erreur Maven search.\nDétail: {exc}")

        # ── npm / raw ─────────────────────────────────────────────────────────
        else:
            params = {
                'repository': repository,
                'name':       image_name or artifact_id,
            }
            if version:
                params['version'] = version
            search_path = f'/service/rest/v1/search?{urlencode(params)}'
            try:
                resp  = _nexus_request(nexus_url, nexus_user, nexus_pass, search_path)
                items = resp.get('items', [])
                found = len(items) > 0
                if found:
                    details = {'name': items[0].get('name', ''), 'version': items[0].get('version', '')}
            except (URLError, HTTPError) as exc:
                raise RuntimeError(f"[Nexus] Erreur search.\nDétail: {exc}")

        # ── Résultat ─────────────────────────────────────────────────────────
        if not found and block_if_missing:
            artifact_label = (
                f'{image_name}:{image_tag}' if artifact_type == 'docker'
                else f'{group_id}:{artifact_id}:{version}' if artifact_type == 'maven2'
                else f'{image_name or artifact_id}:{version}'
            )
            raise RuntimeError(
                f"[Nexus] Artefact introuvable : {artifact_label}\n"
                f"Repository : {repository}  |  Nexus : {nexus_url}\n"
                "Vérifiez que l'artefact a bien été publié avant de déployer."
            )

        return {
            output_key: {
                'found':         found,
                'artifact_type': artifact_type,
                'repository':    repository,
                'details':       details,
                'nexus_url':     nexus_url,
            },
            'nexus_artifact_found': found,
            'nexus_artifact_url':   asset_url,
        }
