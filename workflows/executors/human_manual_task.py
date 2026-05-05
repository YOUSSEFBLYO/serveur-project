"""
Human Task (Tâche manuelle) — Executor.

Représente une tâche humaine assignée à un utilisateur ou une équipe.
Suspend le workflow jusqu'à ce que l'utilisateur marque la tâche comme complète.
Similaire à un ticket JIRA ou ServiceNow assigné dans un pipeline.
"""
import logging
from .base import BaseExecutor, SuspendExecution

logger = logging.getLogger(__name__)


class HumanManualTaskExecutor(BaseExecutor):
    """
    human.ManualTask — Tâche manuelle assignée à un humain.

    Config du nœud :
        assignee     : str  — Personne/équipe assignée (email ou username)
        title        : str  — Titre de la tâche
        description  : text — Instructions détaillées
        priority     : enum — 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL'
        dueDate      : datetime — Date limite (optionnel)
        formFields   : text — Champs de formulaire JSON à remplir (optionnel)
    """

    def run(self) -> dict:
        assignee    = self.cfg('assignee', '').strip()
        title       = self.cfg('title', 'Tâche manuelle').strip()
        description = self.cfg('description', '').strip()
        priority    = self.cfg('priority', 'MEDIUM').strip().upper()
        due_date    = self.cfg('dueDate', '').strip()

        if not assignee:
            raise RuntimeError(
                "[HumanManualTask] Aucun responsable assigné.\n"
                "Configurez le champ 'assignee' dans les propriétés du nœud."
            )

        priority_icons = {
            'LOW': '🔵', 'MEDIUM': '🟡', 'HIGH': '🟠', 'CRITICAL': '🔴'
        }
        icon = priority_icons.get(priority, '⚪')

        logger.info(
            f'[HumanManualTask] {icon} Tâche assignée à {assignee} — '
            f'priorité={priority}  titre="{title}"'
        )
        if due_date:
            logger.info(f'[HumanManualTask] Date limite : {due_date}')

        raise SuspendExecution(
            f"Tâche manuelle en attente : « {title} » — Assignée à {assignee} "
            f"[Priorité: {priority}]{' | Échéance: ' + due_date if due_date else ''}"
        )
