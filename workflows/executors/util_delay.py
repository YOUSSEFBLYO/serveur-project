"""
Délai / Timer — Executor.

Bloque l'exécution du workflow pendant une durée configurable
avant de passer à l'étape suivante.

Utile entre un déploiement et une vérification de santé,
ou pour attendre la propagation d'un changement.
"""
import logging
import time

from .base import BaseExecutor

logger = logging.getLogger(__name__)

_MAX_DELAY_SECONDS = 24 * 60 * 60  # 24 heures max


class DelayExecutor(BaseExecutor):
    """
    util.Delay — Pause temporisée du workflow.

    Config du nœud :
        duration    : number — Durée numérique
        unit        : enum   — 'seconds' | 'minutes' | 'hours'
        reason      : str    — Motif du délai (informatif, trace)
        outputKey   : str    — Clé de sortie (défaut: delay_result)
    """

    def run(self) -> dict:
        duration  = float(self.cfg('duration',  30))
        unit      = self.cfg('unit',      'seconds').strip().lower()
        reason    = self.cfg('reason',    '').strip()
        output_key = self.cfg('outputKey', 'delay_result').strip() or 'delay_result'

        # Conversion en secondes
        if unit == 'minutes':
            seconds = duration * 60
        elif unit == 'hours':
            seconds = duration * 3600
        else:
            seconds = duration

        if seconds <= 0:
            raise RuntimeError(
                f"[Delay] Durée invalide : {duration} {unit}.\n"
                "La durée doit être strictement positive."
            )

        if seconds > _MAX_DELAY_SECONDS:
            raise RuntimeError(
                f"[Delay] Durée trop longue : {seconds}s (max 24h).\n"
                "Réduisez la durée ou découpez en plusieurs délais."
            )

        label = f"{duration} {unit}"
        msg   = f'Délai de {label}'
        if reason:
            msg += f' — {reason}'

        logger.info(f'[Delay] ⏱  {msg} …')

        # Implémentation avec ticks pour permettre un suivi dans les logs
        elapsed   = 0.0
        tick      = min(seconds, 10.0)   # tick max 10 s pour les logs
        while elapsed < seconds:
            sleep_time = min(tick, seconds - elapsed)
            time.sleep(sleep_time)
            elapsed += sleep_time
            remaining = seconds - elapsed
            if remaining > 0:
                logger.info(
                    f'[Delay] … {elapsed:.0f}s écoulées, '
                    f'{remaining:.0f}s restantes'
                )

        logger.info(f'[Delay] ✓ Délai de {label} terminé.')

        return {
            output_key: {
                'duration_seconds': seconds,
                'duration_label':   label,
                'reason':           reason,
            },
            'delay_elapsed_seconds': seconds,
        }
