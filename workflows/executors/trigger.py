"""
Trigger — Exécuteur unifié (nœud 1/7).

Remplace les 4 anciens déclencheurs :
  trigger.GitPush | trigger.Manual | trigger.Cron | trigger.Webhook

Un seul nœud avec trigger_type ∈ {manual, webhook, cron, git}.
Equivalent n8n : Trigger Node — Equivalent Camunda : Start Event.

Config du nœud :
    trigger_type   : enum  — 'manual' | 'webhook' | 'cron' | 'git'
    triggeredBy    : str   — email/nom du déclencheur (mode manual)
    environment    : str   — env cible (manual)
    cronExpression : str   — expression CRON (cron)
    webhookPath    : str   — endpoint (webhook)
    repoUrl        : str   — URL du dépôt (git)
    branch         : str   — branche (git)
    inputParams    : str   — JSON de paramètres additionnels
"""
import logging
import time

from .base import BaseExecutor

logger = logging.getLogger(__name__)


class TriggerExecutor(BaseExecutor):
    """
    trigger — Nœud de démarrage universel.

    Enregistre les métadonnées de déclenchement dans le contexte
    et les propage aux nœuds suivants.
    """

    def run(self) -> dict:
        trigger_type = str(self.cfg('trigger_type', 'manual')).lower().strip()

        logger.info(f'[Trigger] Type : {trigger_type}')

        if trigger_type == 'manual':
            return self._run_manual()
        elif trigger_type == 'webhook':
            return self._run_webhook()
        elif trigger_type == 'cron':
            return self._run_cron()
        elif trigger_type == 'git':
            return self._run_git()
        else:
            logger.warning(f'[Trigger] Type inconnu "{trigger_type}" → traité comme manual')
            return self._run_manual()

    # ─────────────────────────────────────────────────────────────────────────
    # Modes
    # ─────────────────────────────────────────────────────────────────────────

    def _run_manual(self) -> dict:
        # Nouveau nom: initiated_by — ancien: triggeredBy (rétrocompat)
        triggered_by = (
            self.cfg('initiated_by', '').strip()
            or self.cfg('triggeredBy', '').strip()
            or self.ctx('triggered_by', 'user@workflow.local')
        )

        logger.info(f'[Trigger/Manual] Déclenché par {triggered_by}')
        time.sleep(0.3)

        return {
            'trigger_type': 'manual',
            'triggered_by': triggered_by,
        }

    def _run_webhook(self) -> dict:
        # Nouveau nom: webhook_secret — ancien: secretToken (rétrocompat)
        secret      = self.cfg('webhook_secret', '') or self.cfg('secretToken', '')
        allowed_ips = self.cfg('allowed_ips', '').strip()
        payload     = self.ctx('webhook_payload', {})

        logger.info(f'[Trigger/Webhook] secret={"✓" if secret else "non"}  ips={allowed_ips or "all"}')
        time.sleep(0.3)

        return {
            'trigger_type':    'webhook',
            'webhook_payload': payload,
            'allowed_ips':     allowed_ips,
        }

    def _run_cron(self) -> dict:
        # Nouveau nom: cron_expression — ancien: cronExpression (rétrocompat)
        cron_expr = (
            self.cfg('cron_expression', '').strip()
            or self.cfg('cronExpression', '0 9 * * 1-5').strip()
        )
        timezone = self.cfg('timezone', 'Africa/Casablanca').strip()

        logger.info(f'[Trigger/Cron] Expression: {cron_expr}  tz={timezone}')
        time.sleep(0.3)

        return {
            'trigger_type':    'cron',
            'cron_expression': cron_expr,
            'cron_timezone':   timezone,
        }

    def _run_git(self) -> dict:
        # Nouveau nom: branch_filter — ancien: branch (rétrocompat)
        branch_filter = (
            self.cfg('branch_filter', '').strip()
            or self.cfg('branch', 'main').strip()
        )
        # Nouveau nom: event_type — ancien: eventType (rétrocompat)
        event_type = (
            self.cfg('event_type', '').strip()
            or self.cfg('eventType', 'push').strip()
        )

        logger.info(f'[Trigger/Git] {event_type} → branche "{branch_filter}"')
        time.sleep(0.3)

        return {
            'trigger_type':   'git',
            'trigger_event':  event_type,
            'branch':         branch_filter,
            'branch_filter':  branch_filter,
        }
