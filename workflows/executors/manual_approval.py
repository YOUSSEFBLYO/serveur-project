"""
Approbation Manuelle — Executor isolé.
Suspend l'exécution du workflow jusqu'à validation humaine.
"""
import logging
from .base import BaseExecutor, SuspendExecution

logger = logging.getLogger(__name__)


class ManualApprovalExecutor(BaseExecutor):
    """
    approval.Manual — Tâche d'approbation manuelle.
    Suspend le workflow et attend une action humaine explicite.
    """

    def run(self) -> dict:
        assignee    = self.cfg('assignee', 'Non assigné')
        description = self.cfg('description', 'Approbation requise avant de continuer.')

        logger.info(f'[ManualApproval] En attente de validation par : {assignee}')
        raise SuspendExecution(
            f"En attente d'approbation manuelle par « {assignee} » — {description}"
        )
