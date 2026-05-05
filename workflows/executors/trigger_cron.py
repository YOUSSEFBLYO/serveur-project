"""
Trigger Planifié (Cron) — Executor.

Déclenchement planifié du workflow selon une expression CRON.
En production, ce nœud serait géré par un scheduler (APScheduler, Celery Beat,
cron Linux, etc.). Ici, l'executor valide l'expression et propage les
métadonnées de planification dans le contexte.
"""
import logging
import re
import time
from datetime import datetime, timezone

from .base import BaseExecutor

logger = logging.getLogger(__name__)

# Expression cron simplifiée : 5 champs (min, heure, jour, mois, dow)
_CRON_RE = re.compile(
    r'^(\*|[0-9,\-\/]+)\s+'   # minutes
    r'(\*|[0-9,\-\/]+)\s+'   # heures
    r'(\*|[0-9,\-\/]+)\s+'   # jour du mois
    r'(\*|[0-9,\-\/]+)\s+'   # mois
    r'(\*|[0-9,\-\/]+)$'     # jour de la semaine
)


def _validate_cron(expr: str) -> bool:
    """Validation basique d'une expression cron à 5 champs."""
    return bool(_CRON_RE.match(expr.strip()))


class TriggerCronExecutor(BaseExecutor):
    """
    trigger.Cron — Déclencheur planifié.

    Config du nœud :
        cronExpression : str  — Expression CRON (ex: '0 8 * * 1-5')
        timezone       : str  — Fuseau horaire (ex: 'Europe/Paris')
        description    : str  — Description lisible (ex: 'Tous les jours à 8h')
        skipIfRunning  : bool — Ignorer si une exécution est déjà en cours
    """

    def run(self) -> dict:
        cron_expr     = self.cfg('cronExpression', '0 * * * *').strip()
        tz            = self.cfg('timezone', 'UTC').strip() or 'UTC'
        description   = self.cfg('description', '').strip()
        skip_running  = self.cfg('skipIfRunning', False)

        if not cron_expr:
            raise RuntimeError(
                "[TriggerCron] Expression CRON vide.\n"
                "Configurez 'cronExpression' (ex: '0 8 * * 1-5' = lun-ven à 8h)."
            )

        if not _validate_cron(cron_expr):
            raise RuntimeError(
                f"[TriggerCron] Expression CRON invalide : '{cron_expr}'.\n"
                "Format attendu : 'min heure jour mois dow'  (ex: '0 8 * * 1-5')"
            )

        now_utc = datetime.now(timezone.utc).isoformat()

        logger.info(
            f'[TriggerCron] Déclenchement planifié — expr="{cron_expr}"  '
            f'tz={tz}  scheduled_at={now_utc}'
        )
        if description:
            logger.info(f'[TriggerCron] Planification : {description}')

        time.sleep(0.2)

        return {
            'trigger_event':      'scheduled',
            'cron_expression':    cron_expr,
            'cron_timezone':      tz,
            'cron_description':   description,
            'scheduled_at':       now_utc,
            'skip_if_running':    skip_running,
        }
