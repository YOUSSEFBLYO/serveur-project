"""
Human Task — Exécuteur unifié (nœud 4/7).

Remplace :
  Manual Task | Gate Task (GO/NO-GO) | Formulaire (Form)

Suspend le workflow et attend une action humaine.
Equivalent Camunda : User Task — pas de nœud n8n natif.

Config du nœud :
    mode          : enum   — 'approval' | 'form' | 'task'
    title         : str    — titre affiché à l'utilisateur
    description   : text   — instructions
    assignees     : str    — email ou équipe assignée
    priority      : enum   — LOW | MEDIUM | HIGH | CRITICAL
    min_approvals : int    — nombre d'approbateurs requis (si mode=approval)
    formFields    : text   — JSON des champs (si mode=form)
    formTitle     : str    — titre du formulaire (si mode=form)
    timeout_hours : int    — délai max avant expiration (défaut 48h)

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
    """Suspend le workflow jusqu'à une action humaine.
    Le mode détermine le type d'interaction attendue.
    """

    def run(self) -> dict:
        """Lit la configuration, logue le contexte et délègue au handler du mode."""
        mode        = str(self.cfg('mode', 'approval')).lower().strip()
        title       = self.cfg('title', 'Tâche en attente').strip()
        description = (self.cfg('description', '') or '').strip()
        # Rétrocompat : ancien champ "assignee" (singulier) → nouveau "assignees"
        assignees   = (self.cfg('assignees', '') or self.cfg('assignee', '') or '').strip()
        priority    = (self.cfg('priority', 'MEDIUM') or 'MEDIUM').strip().upper()

        # Conversion sécurisée : la valeur peut être une string non numérique depuis le frontend
        try:
            timeout_hours = int(self.cfg('timeout_hours', 48) or 48)
        except (ValueError, TypeError):
            timeout_hours = 48

        logger.info(
            f'[HumanTask] mode={mode}  title="{title}"  priority={priority}  '
            f'assignees={assignees or "(non assigné)"}  timeout={timeout_hours}h'
        )

        # Petit délai pour que l'orchestrateur écrive RUNNING en base avant WAITING
        time.sleep(0.3)

        if mode == 'form':
            return self._handle_form(title, description, assignees, priority)
        elif mode == 'task':
            return self._handle_task(title, description, assignees, priority)
        else:
            return self._handle_approval(title, description, assignees, priority)

    # ─────────────────────────────────────────────────────────────────────────
    # Modes
    # ─────────────────────────────────────────────────────────────────────────

    def _handle_approval(self, title: str, description: str, assignees: str, priority: str) -> dict:
        """Lève SuspendExecution pour attendre une approbation GO/NO-GO."""
        try:
            min_approvals = int(self.cfg('min_approvals', 1) or 1)
        except (ValueError, TypeError):
            min_approvals = 1

        logger.info(
            f'[HumanTask/Approval] "{title}" — '
            f'min_approvals={min_approvals}  assignees={assignees or "(non assigné)"}'
        )
        raise SuspendExecution(
            f'Approbation requise : {title} | '
            f'Assigné à : {assignees or "non défini"} | '
            f'Approbations requises : {min_approvals} | '
            f'Priorité : {priority}'
            + (f' | Instructions : {description}' if description else '')
        )

    def _handle_form(self, title: str, description: str, assignees: str, priority: str) -> dict:
        """Lève SuspendExecution pour attendre la soumission d'un formulaire."""
        # cfg peut retourner None si la clé existe mais vaut None → guard avec "or title"
        raw_form_title = self.cfg('formTitle', '') or ''
        form_title     = raw_form_title.strip() or title

        logger.info(f'[HumanTask/Form] "{form_title}" — En attente de soumission')
        raise SuspendExecution(
            f'Formulaire requis : {form_title} | '
            f'Assigné à : {assignees or "non défini"} | '
            f'Priorité : {priority}'
            + (f' | Instructions : {description}' if description else '')
        )

    def _handle_task(self, title: str, description: str, assignees: str, priority: str) -> dict:
        """Lève SuspendExecution pour attendre la confirmation d'une tâche libre."""
        logger.info(
            f'[HumanTask/Task] "{title}" — '
            f'En attente de complétion par {assignees or "utilisateur"}'
        )
        raise SuspendExecution(
            f'Tâche à compléter : {title} | '
            f'Assigné à : {assignees or "non défini"} | '
            f'Priorité : {priority}'
            + (f' | Instructions : {description}' if description else '')
        )
