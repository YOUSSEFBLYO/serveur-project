"""
Executor registry.
Maps node_type strings → executor classes.
Unknown types fall back to StaticExecutor (no-op).
"""
from .base          import BaseExecutor, StaticExecutor
from .argocd        import ArgoCDDeployExecutor, ArgoCDRollbackExecutor
from .teams         import TeamsAlertExecutor, TeamsNotificationExecutor, TeamsApprovalExecutor
from .gitlab        import GitLabPipelineExecutor
from .dynatrace     import DynatraceMonitorExecutor
from .http_request  import HttpRequestExecutor
from .script        import ScriptTaskExecutor
from .logic         import DecisionExecutor, ParallelExecutor
from .manual        import ManualTaskExecutor, GateTaskExecutor
from .n8n           import N8nTriggerExecutor
from .cicd          import (
    GitPullExecutor, BranchGateExecutor,
    NpmInstallExecutor, NpmBuildExecutor,
    NpmTestExecutor, NpmCoverageExecutor,
    GenerateReportExecutor, SendTeamsReportExecutor,
    SendEmailReportExecutor,
)

_REGISTRY: dict[str, type[BaseExecutor]] = {
    # Logique & Flux
    'logic.Decision':           DecisionExecutor,
    'logic.Parallel':           ParallelExecutor,
    'xlrelease.Task':           ManualTaskExecutor,
    'xlrelease.GateTask':       GateTaskExecutor,
    # Déploiement & GitOps
    'argocd.Deploy':            ArgoCDDeployExecutor,
    'argocd.Rollback':          ArgoCDRollbackExecutor,
    # Observabilité
    'dynatrace.Monitor':        DynatraceMonitorExecutor,
    # Notifications
    'teams.Alert':              TeamsAlertExecutor,
    'teams.Notification':       TeamsNotificationExecutor,
    'teams.Approval':           TeamsApprovalExecutor,
    # Orchestration & CI
    'gitlab.RunPipeline':       GitLabPipelineExecutor,
    'n8n.Trigger':              N8nTriggerExecutor,
    # Intégrations & Scripts
    'remoteScript.HttpRequest': HttpRequestExecutor,
    'xlrelease.ScriptTask':     ScriptTaskExecutor,
    # ══ CI/CD Pipeline (GitLab → Build → Test → Teams) ════════════════════
    'cicd.GitPull':             GitPullExecutor,
    'cicd.BranchGate':          BranchGateExecutor,
    'cicd.NpmInstall':          NpmInstallExecutor,
    'cicd.NpmBuild':            NpmBuildExecutor,
    'cicd.NpmTest':             NpmTestExecutor,
    'cicd.NpmCoverage':         NpmCoverageExecutor,
    'cicd.GenerateReport':      GenerateReportExecutor,
    'cicd.SendTeams':           SendTeamsReportExecutor,
    'cicd.SendEmail':           SendEmailReportExecutor,
}


def get_executor(node_type: str) -> type[BaseExecutor]:
    return _REGISTRY.get(node_type, StaticExecutor)


__all__ = ['get_executor', 'BaseExecutor', 'StaticExecutor']
