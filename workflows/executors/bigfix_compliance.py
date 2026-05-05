"""
BigFix — Conformité serveur — Executor.

Interroge l'API BigFix pour vérifier le taux de conformité
d'un ou plusieurs serveurs avant une MEP.

Kraken dispose déjà de deux instances BigFix (hors-prod et prod)
avec leurs tokens dans le .env.
"""
import base64
import json
import logging
import os
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

from .base import BaseExecutor

logger = logging.getLogger(__name__)

_BIGFIX_URLS = {
    'HORS-PROD': os.environ.get('BIGFIX_URL_HPROD', ''),
    'PROD':      os.environ.get('BIGFIX_URL_PROD',  ''),
}
_BIGFIX_USERS = {
    'HORS-PROD': os.environ.get('BIGFIX_USER_HPROD', ''),
    'PROD':      os.environ.get('BIGFIX_USER_PROD',  ''),
}
_BIGFIX_PASSES = {
    'HORS-PROD': os.environ.get('BIGFIX_PASS_HPROD', ''),
    'PROD':      os.environ.get('BIGFIX_PASS_PROD',  ''),
}


def _bigfix_get(url: str, user: str, password: str, path: str) -> bytes:
    """Appel REST BigFix avec Basic Auth."""
    creds  = base64.b64encode(f'{user}:{password}'.encode()).decode()
    full   = f"{url.rstrip('/')}{path}"
    req    = Request(full, headers={
        'Authorization': f'Basic {creds}',
        'Accept':        'application/json',
    })
    with urlopen(req, timeout=30) as resp:
        return resp.read()


class BigFixComplianceExecutor(BaseExecutor):
    """
    bigfix.Compliance — Vérification de conformité serveur via BigFix.

    Config du nœud :
        environment      : enum   — 'HORS-PROD' | 'PROD'
        bigfixUrl        : str    — URL BigFix (override .env)
        bigfixUser       : str    — Login BigFix (override .env)
        bigfixPass       : str    — Mot de passe BigFix (override .env)
        computerName     : str    — Nom du serveur cible (ou liste séparée par virgule)
        siteId           : str    — ID du site BigFix (optionnel)
        minCompliance    : number — Seuil minimal de conformité % (défaut: 80)
        blockOnFail      : bool   — Bloquer le workflow si sous le seuil
        outputKey        : str    — Clé de sortie (défaut: bigfix_result)
    """

    def run(self) -> dict:
        environment   = self.cfg('environment',   'HORS-PROD').strip().upper()
        bigfix_url    = (self.cfg('bigfixUrl',  '') or _BIGFIX_URLS.get(environment,  '')).strip()
        bigfix_user   = (self.cfg('bigfixUser', '') or _BIGFIX_USERS.get(environment, '')).strip()
        bigfix_pass   = (self.cfg('bigfixPass', '') or _BIGFIX_PASSES.get(environment, '')).strip()
        computer_name = self.cfg('computerName', '').strip()
        min_compliance = float(self.cfg('minCompliance', 80))
        block_on_fail  = self.cfg('blockOnFail', True)
        output_key     = self.cfg('outputKey', 'bigfix_result').strip() or 'bigfix_result'

        if not bigfix_url:
            raise RuntimeError(
                f"[BigFix] URL non configurée pour l'environnement '{environment}'.\n"
                "Renseignez BIGFIX_URL_PROD / BIGFIX_URL_HPROD dans .env ou le champ bigfixUrl."
            )
        if not bigfix_user or not bigfix_pass:
            raise RuntimeError(
                "[BigFix] Identifiants manquants.\n"
                "Configurez BIGFIX_USER/PASS dans .env ou les propriétés du nœud."
            )

        logger.info(
            f'[BigFix] Vérification conformité — env={environment}  '
            f'serveur={computer_name or "tous"}  seuil={min_compliance}%'
        )

        # Appel API BigFix : liste des ordinateurs
        try:
            if computer_name:
                # Filtre sur le nom du computer
                path = f'/api/computers?name={computer_name}'
            else:
                path = '/api/computers'
            raw = _bigfix_get(bigfix_url, bigfix_user, bigfix_pass, path)
        except (URLError, HTTPError) as exc:
            raise RuntimeError(
                f"[BigFix] Impossible de contacter l'API BigFix.\n"
                f"URL: {bigfix_url}\nDétail: {exc}"
            )

        # Parsing de la réponse (BigFix répond en XML ou JSON selon la config)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # BigFix répond souvent en XML — extraction basique
            import re
            names  = re.findall(r'<Name>(.*?)</Name>', raw.decode('utf-8', errors='replace'))
            scores = re.findall(r'<ComplianceScore>(.*?)</ComplianceScore>',
                                raw.decode('utf-8', errors='replace'))
            data = {
                'computers': [
                    {'name': n, 'compliance_score': float(s)}
                    for n, s in zip(names, scores)
                ]
            }

        computers = data.get('computers', [])
        if not computers:
            logger.warning(
                f'[BigFix] Aucun ordinateur trouvé pour "{computer_name}" '
                f'sur {environment}. Vérifiez le nom.'
            )
            compliance_pct = 0.0
            compliant_count = 0
        else:
            scores = [
                float(c.get('compliance_score', c.get('ComplianceScore', 0)))
                for c in computers
            ]
            compliance_pct  = sum(scores) / len(scores) if scores else 0.0
            compliant_count = sum(1 for s in scores if s >= min_compliance)

        is_compliant = compliance_pct >= min_compliance

        logger.info(
            f'[BigFix] Conformité : {compliance_pct:.1f}%  '
            f'(seuil={min_compliance}%)  conforme={is_compliant}'
        )

        if not is_compliant and block_on_fail:
            raise RuntimeError(
                f"[BigFix] Conformité insuffisante : {compliance_pct:.1f}% "
                f"(seuil requis : {min_compliance}%)\n"
                f"Serveur(s) : {computer_name or 'global'}\n"
                "Résolvez les problèmes de conformité avant de poursuivre la MEP."
            )

        return {
            output_key: {
                'environment':     environment,
                'computer_name':   computer_name,
                'compliance_pct':  round(compliance_pct, 2),
                'min_compliance':  min_compliance,
                'is_compliant':    is_compliant,
                'computers_count': len(computers),
                'compliant_count': compliant_count,
            },
            'bigfix_compliance_pct': round(compliance_pct, 2),
            'bigfix_is_compliant':   is_compliant,
        }
