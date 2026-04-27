"""
Manual tasks and gate executors.
These tasks are inherently blocking and require human intervention.
In this automated executor scope, they simulate waiting for a user action.
"""
import logging
import time
from .base import BaseExecutor, SuspendExecution

logger = logging.getLogger(__name__)


class ManualTaskExecutor(BaseExecutor):
    """
    xlrelease.Task — represents a manual task assigned to a team.
    In a real engine, this would pause the workflow until marked done.
    """

    def run(self) -> dict:
        team        = self.cfg('team', 'Unassigned')
        description = self.cfg('description', '')

        logger.info(f'[ManualTask] Assigned to {team}: {description}. Suspending for manual intervention.')
        
        # Suspend workflow execution!
        raise SuspendExecution(f"En attente de validation manuelle par l'équipe {team}")


class GateTaskExecutor(BaseExecutor):
    """
    xlrelease.GateTask — represents a blocking GO/NO-GO gate.
    Similar to a manual task, but represents a strict condition check.
    """

    def run(self) -> dict:
        team       = self.cfg('team', 'Release Managers')
        conditions = self.cfg('conditions', [])

        logger.info(f'[GateTask] Gate assigned to {team}. Conditions: {conditions}. Suspending.')
        
        # Suspend workflow execution!
        raise SuspendExecution(f"Gate de validation GO/NO-GO requise par {team}")
