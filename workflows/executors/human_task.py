"""
Human Task — Exécuteur unifié (nœud 4/7).

Remplace :
  Manual Task | Gate Task (GO/NO-GO) | Formulaire (Form)

Suspend le workflow et attend une action humaine.
Equivalent Camunda : User Task — pas de nœud n8n natif.

Config du nœud :
    mode        : enum   — 'approval' | 'form' | 'task'
    title       : str    — titre affiché à l'utilisateur
    description : text   — instructions
    assignee    : str    — email ou équipe assignée
    priority    : enum   — LOW | MEDIUM | HIGH | CRITICAL
    formFields  : text   — JSON des champs (si mode=form)
    formTitle   : str    — titre du formulaire (si mode=form)

Comportement :
    approval → GO/NO-GO bloquant — lève SuspendExecution
               Reprise via POST /executions/{id}/approve/{node_id}/
    form     → Formulaire — lève SuspendExecution
               Reprise via POST /executions/{id}/form/{node_id}/
    task     → Tâche libre — lève SuspendExecution
               Reprise via POST /executions/{id}/approve/{node_id}/ avec decision=APPROVED
"""
import logging
import time

from .base import BaseExecutor, SuspendExecution

logger = logging.getLogger(__name__)


class HumanTaskExecutor(BaseExecutor):
    """
    human_task — Suspend le workflow jusqu'à une action humaine.
    Le mode détermine le type d'interaction attendue.
    """

    def run(self) -> dict:
        mode        = str(self.cfg('mode', 'approval')).lower().strip()
        title       = self.cfg('title', 'Tâche en attente').strip()
        description = self.cfg('description', '').strip()
        # Nouveau nom: assignees (plural) — ancien: assignee (rétrocompat)
        assignees   = (self.cfg('assignees', '') or self.cfg('assignee', '')).strip()
        timeout_hours = int(self.cfg('timeout_hours', 48) or 48)

        logger.info(
            f'[HumanTask] mode={mode}  title="{title}"  '
            f'assignees={assignees or "(non assigné)"}  timeout={timeout_hours}h'
        )

        time.sleep(0.3)

        if mode == 'form':
            return self._handle_form(title, description, assignees)
        elif mode == 'task':
            return self._handle_task(title, description, assignees)
        else:
            return self._handle_approval(title, description, assignees)

    # ─────────────────────────────────────────────────────────────────────────
    # Modes
    # ─────────────────────────────────────────────────────────────────────────

    def _handle_approval(self, title: str, description: str, assignees: str) -> dict:
        min_approvals = int(self.cfg('min_approvals', 1) or 1)
        logger.info(
            f'[HumanTask/Approval] "{title}" — '
            f'min_approvals={min_approvals}  assignees={assignees or "(non assigné)"}'
        )
        raise SuspendExecution(
            f'Approbation requise : {title} | '
            f'Assigné à : {assignees or "non défini"} | '
            f'Approbations requises : {min_approvals}'
        )

    def _handle_form(self, title: str, description: str, assignees: str) -> dict:
        form_title = self.cfg('formTitle', title).strip() or title
        logger.info(f'[HumanTask/Form] "{form_title}" — En attente de soumission')
        raise SuspendExecution(
            f'Formulaire requis : {form_title} | Assigné à : {assignees or "non défini"}'
        )

    def _handle_task(self, title: str, description: str, assignees: str) -> dict:
        logger.info(
            f'[HumanTask/Task] "{title}" — '
            f'En attente de complétion par {assignees or "utilisateur"}'
        )
        raise SuspendExecution(
            f'Tâche à compléter : {title} | Assigné à : {assignees or "non défini"}'
        )
