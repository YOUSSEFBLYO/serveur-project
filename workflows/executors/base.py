"""Base executor class every node executor must inherit from."""
import logging
import time
from abc import ABC, abstractmethod

class SubtaskException(Exception): pass
class SuspendExecution(Exception):
    """Raised when an executor needs to pause the workflow for manual intervention."""
    pass

logger = logging.getLogger(__name__)


class BaseExecutor(ABC):
    """
    Abstract executor.  Each concrete executor receives the WorkflowNode
    instance and must implement run() which returns an outputs dict.

    The optional *context* dict carries runtime variables accumulated from
    all previously completed nodes in the same execution run.  Use
    self.ctx(key) to read a variable from context with a fallback to the
    node's own config.
    """

    def __init__(self, node, context: dict = None):
        self.node    = node
        self.config  = node.config or {}
        self.context = context or {}

    def cfg(self, key: str, default=None):
        """Convenience accessor for node config properties."""
        return self.config.get(key, default)

    def ctx(self, key: str, default=None):
        """Read a runtime variable — context first, then node config, then default."""
        return self.context.get(key, self.config.get(key, default))

    @abstractmethod
    def run(self) -> dict:
        """Execute the node logic.  Returns an outputs dict."""

    def _simulate(self, label: str, seconds: float = 1.5) -> dict:
        """Utility: sleep to simulate work and return a mock result."""
        logger.info(f'[SIMULATE] {label} — sleeping {seconds}s')
        time.sleep(seconds)
        return {'result': 'simulated', 'label': label}


class StaticExecutor(BaseExecutor):
    """
    No-op executor for node types without real integration
    (e.g. Jenkins, Email placeholders kept static on the canvas).
    """

    def run(self) -> dict:
        logger.info(f'[STATIC] Node "{self.node.label}" ({self.node.node_type}) — passthrough')
        time.sleep(0.5)
        return {'result': 'static-passthrough', 'node_type': self.node.node_type}
