"""
Registre des exécuteurs — 23 nœuds couvrant tous les cas métier CI/CD.

Catégories :
  Déclencheurs     : trigger.GitPush | trigger.Manual | trigger.Cron | trigger.Webhook
  Validation       : human.ManualTask | human.GateTask | human.Form
  Build & Qualité  : build.GitLabPipeline | build.DockerBuild | build.SonarQube | script.Task
  Déploiement      : deploy.ArgoCD | deploy.Rollback | deploy.Terraform | openshift.Action
  Observabilité    : observe.Dynatrace | nexus.Artifact | bigfix.Compliance
  Intégrations     : integration.Http | integration.Teams | itop.Ticket
  Logique          : logic.Condition | util.Delay

  (Héritage) : approval.Manual | gitlab.Pull | report.Generate | email.Send
"""

# ── Base ──────────────────────────────────────────────────────────────────────
from .base                  import BaseExecutor, StaticExecutor

# ── Héritage (pipeline existant) ─────────────────────────────────────────────
from .manual_approval       import ManualApprovalExecutor
from .gitlab_pull           import GitLabPullExecutor
from .script_task           import ScriptTaskExecutor
from .generate_report       import GenerateReportExecutor
from .send_email            import SendEmailExecutor

# ── Déclencheurs ─────────────────────────────────────────────────────────────
from .trigger_gitpush       import TriggerGitPushExecutor
from .trigger_manual        import TriggerManualDispatchExecutor
from .trigger_cron          import TriggerCronExecutor
from .trigger_webhook       import TriggerWebhookExecutor

# ── Validation humaine ────────────────────────────────────────────────────────
from .human_manual_task     import HumanManualTaskExecutor
from .human_gate_task       import GateTaskExecutor

# ── Build & Qualité ───────────────────────────────────────────────────────────
from .build_gitlab_pipeline import GitLabPipelineExecutor
from .build_docker          import DockerBuildPushExecutor
from .build_sonarqube       import SonarQubeExecutor

# ── Déploiement ───────────────────────────────────────────────────────────────
from .deploy_argocd         import ArgoCDDeployExecutor
from .deploy_rollback       import RollbackGitRevertExecutor
from .deploy_terraform      import TerraformPlanExecutor

# ── Observabilité ─────────────────────────────────────────────────────────────
from .observe_dynatrace     import DynatraceMonitoringExecutor

# ── Intégrations ──────────────────────────────────────────────────────────────
from .integration_http      import HttpRequestExecutor
from .integration_teams     import TeamsNotificationExecutor

# ── Nouveaux nœuds critiques ──────────────────────────────────────────────────
from .itop_ticket           import ITopTicketExecutor
from .openshift_action      import OpenShiftActionExecutor
from .logic_condition       import ConditionExecutor
from .util_delay            import DelayExecutor
from .human_form            import FormInputExecutor
from .bigfix_compliance     import BigFixComplianceExecutor
from .nexus_artifact        import NexusArtifactExecutor


_REGISTRY: dict[str, type[BaseExecutor]] = {

    # ── Héritage pipeline existant ───────────────────────────────────────────
    'approval.Manual':          ManualApprovalExecutor,
    'gitlab.Pull':              GitLabPullExecutor,
    'script.Task':              ScriptTaskExecutor,
    'report.Generate':          GenerateReportExecutor,
    'email.Send':               SendEmailExecutor,

    # ── Déclencheurs ─────────────────────────────────────────────────────────
    'trigger.GitPush':          TriggerGitPushExecutor,
    'trigger.Manual':           TriggerManualDispatchExecutor,
    'trigger.Cron':             TriggerCronExecutor,
    'trigger.Webhook':          TriggerWebhookExecutor,

    # ── Validation humaine ────────────────────────────────────────────────────
    'human.ManualTask':         HumanManualTaskExecutor,
    'human.GateTask':           GateTaskExecutor,

    # ── Build & Qualité ───────────────────────────────────────────────────────
    'build.GitLabPipeline':     GitLabPipelineExecutor,
    'build.DockerBuild':        DockerBuildPushExecutor,
    'build.SonarQube':          SonarQubeExecutor,
    'build.ScriptTask':         ScriptTaskExecutor,    # alias canonique

    # ── Déploiement ───────────────────────────────────────────────────────────
    'deploy.ArgoCD':            ArgoCDDeployExecutor,
    'deploy.Rollback':          RollbackGitRevertExecutor,
    'deploy.Terraform':         TerraformPlanExecutor,

    # ── Observabilité ─────────────────────────────────────────────────────────
    'observe.Dynatrace':        DynatraceMonitoringExecutor,

    # ── Intégrations ──────────────────────────────────────────────────────────
    'integration.Http':         HttpRequestExecutor,
    'integration.Teams':        TeamsNotificationExecutor,

    # ── Nouveaux nœuds critiques ──────────────────────────────────────────────
    'itop.Ticket':              ITopTicketExecutor,
    'openshift.Action':         OpenShiftActionExecutor,
    'logic.Condition':          ConditionExecutor,
    'util.Delay':               DelayExecutor,
    'human.Form':               FormInputExecutor,
    'bigfix.Compliance':        BigFixComplianceExecutor,
    'nexus.Artifact':           NexusArtifactExecutor,
}


def get_executor(node_type: str) -> type[BaseExecutor]:
    """Retourne l'exécuteur correspondant au type, ou StaticExecutor (no-op) si inconnu."""
    return _REGISTRY.get(node_type, StaticExecutor)


__all__ = ['get_executor', 'BaseExecutor', 'StaticExecutor']
