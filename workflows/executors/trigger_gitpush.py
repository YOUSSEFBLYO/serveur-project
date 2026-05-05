"""
Trigger Git Push / Pull Request — Executor.

Simule la réception d'un événement Git Push ou PR.
En production, ce nœud serait déclenché par un webhook GitLab/GitHub.
Il lit les informations de la branche/PR depuis sa config et les injecte
dans le contexte pour les nœuds suivants.
"""
import logging
import time

from .base import BaseExecutor

logger = logging.getLogger(__name__)


class TriggerGitPushExecutor(BaseExecutor):
    """
    trigger.GitPush — Déclencheur Git Push / Pull Request.

    Config du nœud :
        repoUrl      : str  — URL du dépôt (GitLab / GitHub)
        branch       : str  — Branche cible (ex: main, develop)
        eventType    : enum — 'push' | 'pull_request' | 'merge_request'
        targetBranch : str  — Branche cible de la PR (optionnel)
        authorFilter : str  — Restreindre à un auteur (optionnel)
    """

    def run(self) -> dict:
        repo_url      = self.cfg('repoUrl', '').strip()
        branch        = self.cfg('branch', 'main').strip() or 'main'
        event_type    = self.cfg('eventType', 'push').strip() or 'push'
        target_branch = self.cfg('targetBranch', '').strip()
        author_filter = self.cfg('authorFilter', '').strip()

        if not repo_url:
            raise RuntimeError(
                "[TriggerGitPush] Aucune URL de dépôt fournie.\n"
                "Configurez le champ 'repoUrl' dans les propriétés du nœud."
            )

        logger.info(
            f'[TriggerGitPush] Événement détecté — type={event_type}  '
            f'repo={repo_url}  branche={branch}'
        )

        # Simulation : on propage les métadonnées de l'événement
        time.sleep(0.3)

        result = {
            'trigger_event':  event_type,
            'trigger_repo':   repo_url,
            'trigger_branch': branch,
            'triggered_by':   author_filter or 'ci-bot',
            'trigger_ref':    f'refs/heads/{branch}',
        }

        if event_type in ('pull_request', 'merge_request') and target_branch:
            result['target_branch'] = target_branch
            result['is_pr']         = True
            logger.info(
                f'[TriggerGitPush] PR détectée : {branch} → {target_branch}'
            )
        else:
            result['is_pr'] = False

        return result
