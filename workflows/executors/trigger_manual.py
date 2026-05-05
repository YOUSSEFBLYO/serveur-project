"""
Trigger Manuel (Dispatch) — Executor.

Déclenchement manuel du workflow : l'utilisateur fournit des paramètres
d'entrée qui sont injectés dans le contexte d'exécution.
Équivalent du `workflow_dispatch` de GitHub Actions.
"""
import json
import logging
import time

from .base import BaseExecutor

logger = logging.getLogger(__name__)


class TriggerManualDispatchExecutor(BaseExecutor):
    """
    trigger.Manual — Déclencheur manuel (dispatch).

    Config du nœud :
        triggeredBy   : str  — Identifiant de l'utilisateur déclencheur
        environment   : enum — 'dev' | 'staging' | 'production'
        inputParams   : text — Paramètres JSON libres (ex: {"version": "1.2.3"})
        reason        : str  — Motif du déclenchement (pour l'audit)
    """

    def run(self) -> dict:
        triggered_by = self.cfg('triggeredBy', 'anonymous').strip() or 'anonymous'
        environment  = self.cfg('environment', 'dev').strip() or 'dev'
        input_params = self.cfg('inputParams', '{}').strip()
        reason       = self.cfg('reason', '').strip()

        # Parse les paramètres d'entrée JSON
        extra_params: dict = {}
        if input_params:
            try:
                extra_params = json.loads(input_params)
                if not isinstance(extra_params, dict):
                    extra_params = {}
            except (json.JSONDecodeError, ValueError):
                logger.warning('[TriggerManual] inputParams invalide — JSON ignoré')
                extra_params = {}

        logger.info(
            f'[TriggerManual] Déclenchement manuel par {triggered_by} '
            f'— env={environment}  raison={reason or "non spécifiée"}'
        )

        time.sleep(0.2)

        return {
            'trigger_event':   'manual_dispatch',
            'trigger_user':    triggered_by,
            'trigger_env':     environment,
            'trigger_reason':  reason,
            'trigger_params':  extra_params,
            **extra_params,   # Propagation directe des paramètres dans le contexte
        }
