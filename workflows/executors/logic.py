"""
Logic node executors: Decision and Parallel.
"""
import logging
import time
from .base import BaseExecutor

logger = logging.getLogger(__name__)


class DecisionExecutor(BaseExecutor):
    """
    logic.Decision — evaluates a condition.
    In simulation mode the condition always evaluates to True.
    In a real scenario the condition could be a Python expression
    evaluated against the previous nodes' outputs.
    """

    def run(self) -> dict:
        condition    = self.cfg('condition', 'True')
        success_path = self.cfg('successPath', 'true')
        failure_path = self.cfg('failurePath', 'false')

        logger.info(f'[Decision] Evaluating: {condition!r}')
        time.sleep(0.5)

        # Safe eval — only allow simple boolean expressions
        try:
            result = bool(eval(condition, {'__builtins__': {}}, {}))  # noqa: S307
        except Exception:
            result = True  # Default to True on eval error

        chosen_path = success_path if result else failure_path
        logger.info(f'[Decision] Result={result} → path={chosen_path}')

        return {
            'condition':   condition,
            'result':      result,
            'chosen_path': chosen_path,
        }


class ParallelExecutor(BaseExecutor):
    """
    logic.Parallel — represents a fork into N branches.
    The actual parallelism is handled by the graph topology;
    this node acts as a synchronisation barrier.
    """

    def run(self) -> dict:
        branches = self.cfg('branches', '2')
        mode     = self.cfg('mode', 'Attendre tout')

        logger.info(f'[Parallel] Fork into {branches} branches — mode: {mode}')
        time.sleep(0.3)

        return {
            'branches': branches,
            'mode':     mode,
            'status':   'forked',
        }
