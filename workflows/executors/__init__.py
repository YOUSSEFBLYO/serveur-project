"""
Registre des exécuteurs — 7 nœuds canoniques + 3 avancés + 3 AIOps.

  trigger        → TriggerExecutor         (manual | webhook | cron | git)
  http_request   → HttpRequestExecutor     (tout appel API REST)
  script         → ScriptExecutor          (shell | python | ansible)
  human_task     → HumanTaskExecutor       (approval | form | task)
  logic.Condition→ ConditionExecutor       (IF/XOR gateway)
  util.Delay     → DelayExecutor           (timer/wait)
  notification   → NotificationExecutor    (email | teams | slack)
  parallel_fork  → ParallelForkExecutor    (AND-split)
  parallel_join  → ParallelJoinExecutor    (AND-join)
  switch         → SwitchExecutor          (multi-branches)
  sub_workflow   → SubWorkflowExecutor     (call activity)

  ── AIOps ──────────────────────────────────────────────────────────
  aiops.ElasticsearchFetch → ElasticsearchFetchExecutor  (récupère logs ES)
  aiops.LogClassifier      → LogClassifierExecutor       (agent IA classification)
  aiops.ReportGenerator    → ReportGeneratorExecutor     (rapport HTML/MD/text)
"""

from .base          import BaseExecutor, StaticExecutor
from .trigger       import TriggerExecutor
from .http_request  import HttpRequestExecutor
from .script        import ScriptExecutor
from .human_task    import HumanTaskExecutor
from .logic_condition import ConditionExecutor
from .util_delay    import DelayExecutor
from .notification  import NotificationExecutor
from .parallel_fork import ParallelForkExecutor
from .parallel_join import ParallelJoinExecutor
from .switch        import SwitchExecutor
from .sub_workflow  import SubWorkflowExecutor
from .set_variable  import SetVariableExecutor

# ── AIOps ────────────────────────────────────────────────────────────────────
from .aiops_elasticsearch   import ElasticsearchFetchExecutor
from .aiops_log_classifier  import LogClassifierExecutor
from .aiops_report_generator import ReportGeneratorExecutor

_REGISTRY: dict[str, type[BaseExecutor]] = {
    'trigger':         TriggerExecutor,
    'http_request':    HttpRequestExecutor,
    'script':          ScriptExecutor,
    'human_task':      HumanTaskExecutor,
    'logic.Condition': ConditionExecutor,
    'util.Delay':      DelayExecutor,
    'notification':    NotificationExecutor,
    'parallel_fork':   ParallelForkExecutor,
    'parallel_join':   ParallelJoinExecutor,
    'switch':          SwitchExecutor,
    'sub_workflow':    SubWorkflowExecutor,
    'set_variable':    SetVariableExecutor,
    # ── AIOps ────────────────────────────────────────────────────────
    'aiops.ElasticsearchFetch': ElasticsearchFetchExecutor,
    'aiops.LogClassifier':      LogClassifierExecutor,
    'aiops.ReportGenerator':    ReportGeneratorExecutor,
}


def get_executor(node_type: str) -> type[BaseExecutor]:
    """Retourne l'exécuteur correspondant au type, ou StaticExecutor si inconnu."""
    return _REGISTRY.get(node_type, StaticExecutor)


__all__ = ['get_executor', 'BaseExecutor', 'StaticExecutor']
