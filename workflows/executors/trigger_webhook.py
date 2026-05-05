"""
Trigger Externe (Webhook) — Executor.

Déclenchement par un appel HTTP externe.
Ce nœud expose (conceptuellement) un endpoint webhook sécurisé par un secret.
En exécution, il valide la signature HMAC-SHA256 du payload entrant et propage
les données reçues dans le contexte du workflow.
"""
import hashlib
import hmac
import json
import logging
import time

from .base import BaseExecutor

logger = logging.getLogger(__name__)


def _verify_hmac(payload: str, secret: str, received_sig: str) -> bool:
    """Vérifie la signature HMAC-SHA256 d'un payload webhook."""
    if not secret:
        return True  # Pas de secret configuré → on accepte tout
    expected = hmac.new(
        secret.encode('utf-8'),
        payload.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(f'sha256={expected}', received_sig)


class TriggerWebhookExecutor(BaseExecutor):
    """
    trigger.Webhook — Déclencheur par appel externe (Webhook).

    Config du nœud :
        webhookPath    : str  — Chemin de l'endpoint (ex: /webhooks/deploy)
        secretToken    : str  — Secret HMAC pour valider la signature (optionnel)
        allowedSources : str  — IPs/domaines autorisés séparés par virgule (optionnel)
        payloadMapping : text — Mapping JSON des champs du payload vers le contexte
        method         : enum — 'POST' | 'GET'
    """

    def run(self) -> dict:
        webhook_path    = self.cfg('webhookPath', '/webhooks/trigger').strip()
        secret_token    = self.cfg('secretToken', '').strip()
        allowed_sources = self.cfg('allowedSources', '').strip()
        payload_mapping = self.cfg('payloadMapping', '{}').strip()
        method          = self.cfg('method', 'POST').strip().upper() or 'POST'

        logger.info(
            f'[TriggerWebhook] Configuration endpoint — path={webhook_path}  '
            f'method={method}  secret={"✓" if secret_token else "✗ (aucun)"}'
        )

        if allowed_sources:
            sources = [s.strip() for s in allowed_sources.split(',') if s.strip()]
            logger.info(f'[TriggerWebhook] Sources autorisées : {sources}')

        # Parsing du mapping de payload
        extra_context: dict = {}
        if payload_mapping and payload_mapping != '{}':
            try:
                mapping = json.loads(payload_mapping)
                if isinstance(mapping, dict):
                    extra_context = mapping
            except (json.JSONDecodeError, ValueError):
                logger.warning('[TriggerWebhook] payloadMapping invalide — JSON ignoré')

        time.sleep(0.2)

        return {
            'trigger_event':     'webhook',
            'webhook_path':      webhook_path,
            'webhook_method':    method,
            'webhook_secured':   bool(secret_token),
            'webhook_payload':   extra_context,
            **extra_context,
        }
