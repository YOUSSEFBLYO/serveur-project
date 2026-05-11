"""
Switch / Multi-branches — Exécuteur (nœud de routage N branches).

Équivalent : n8n Switch Node | Camunda Inclusive Gateway (OR) | Airflow BranchPythonOperator

Évalue une expression et active UNE seule branche parmi N.
Contrairement au nœud Condition (VRAI/FAUX), le Switch peut router
vers N destinations selon la valeur résolue d'une variable.

Exemple :
    expression = "{{environment}}"
    Valeur résolue = "PROD"
    → La branche dont le handle = "PROD" est activée
    → Toutes les autres branches sont ignorées (SKIPPED)

Config du nœud :
    expression   : str — Expression à évaluer, ex: "{{environment}}" ou "{{status_code}}"
    branches     : text — JSON listant les branches possibles
                   [{"handle": "PROD", "label": "Branche Production"},
                    {"handle": "PPRD", "label": "Branche Pré-prod"},
                    {"handle": "default", "label": "Cas par défaut"}]
    defaultHandle: str — Handle activé si aucune branche ne correspond (défaut: 'default')
"""
import json
import logging
import time

from .base import BaseExecutor

logger = logging.getLogger(__name__)


def _resolve_vars(text: str, context: dict) -> str:
    """Remplace {{key}} par la valeur du contexte."""
    if not text:
        return text
    for key, value in context.items():
        text = text.replace(f'{{{{{key}}}}}', str(value))
    return text


class SwitchExecutor(BaseExecutor):
    """
    switch — Routage multi-branches selon la valeur d'une variable.

    L'orchestrateur lit 'switch_matched_handle' dans les outputs
    pour décider quelle branche activer et lesquelles ignorer.
    """

    def run(self) -> dict:
        expression     = self.cfg('expression', '').strip()
        default_handle = self.cfg('defaultHandle', 'default').strip() or 'default'

        if not expression:
            logger.warning('[Switch] Expression vide — utilisation du handle par défaut')
            matched = default_handle
        else:
            # Résoudre les variables {{ctx_key}} dans l'expression
            resolved = _resolve_vars(expression, self.context)

            # Si l'expression contient encore des {{ non résolus,
            # utiliser le handle par défaut
            if '{{' in resolved:
                logger.warning(
                    f'[Switch] Variable non résolue dans "{expression}" — '
                    f'handle par défaut: "{default_handle}"'
                )
                matched = default_handle
            else:
                matched = resolved.strip()

        # Charger les branches configurées pour le log
        branches_raw = self.cfg('branches', '[]')
        try:
            branches = json.loads(branches_raw) if branches_raw else []
        except (json.JSONDecodeError, TypeError):
            branches = []

        handles = [b.get('handle', '') for b in branches]
        if handles and matched not in handles:
            logger.warning(
                f'[Switch] Valeur "{matched}" ne correspond à aucune branche '
                f'{handles} → fallback "{default_handle}"'
            )
            matched = default_handle

        logger.info(
            f'[Switch] Expression: "{expression}"  '
            f'→ Valeur résolue: "{matched}"  '
            f'(branches disponibles: {handles})'
        )

        time.sleep(0.3)

        return {
            'switch_expression':     expression,
            'switch_value':          matched,
            'switch_matched_handle': matched,  # utilisé par l'orchestrateur
        }
