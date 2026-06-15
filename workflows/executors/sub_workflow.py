"""
Sub-Workflow (Call Activity) — Exécuteur.

Équivalent : Camunda Call Activity | n8n Execute Workflow | XLRelease Trigger Release

Déclenche un autre workflow existant (par son ID) et attend sa complétion.
Les sorties du sous-workflow sont injectées dans le contexte du workflow parent.

Cas d'usage :
    - Réutiliser un workflow "Validation Sécurité" dans plusieurs workflows parents
    - Déclencher un workflow "Notification" depuis un workflow "Déploiement"
    - Isoler des processus complexes en sous-workflows réutilisables

Config du nœud :
    workflowId  : str  — ID numérique du workflow cible (requis)
    contextPass : text — JSON des variables du contexte parent à passer au sous-workflow
                         Ex: {"branch": "branch", "env": "environment"}
    outputPrefix: str  — Préfixe des sorties dans le contexte parent (défaut: 'sub_')
    timeout     : int  — Timeout en secondes (défaut: 300)
    failOnError : bool — Faire échouer le parent si le sous-workflow échoue (défaut: true)
"""
import json
import logging

from .base import BaseExecutor

logger = logging.getLogger(__name__)


class SubWorkflowExecutor(BaseExecutor):
    """
    sub_workflow — Déclenche un workflow existant et attend sa complétion.
    """

    def run(self) -> dict:
        workflow_id_raw = self.cfg('workflowId', '').strip()
        context_pass    = self.cfg('contextPass', '{}').strip()
        output_prefix   = self.cfg('outputPrefix', 'sub_').strip() or 'sub_'
        fail_on_error   = bool(self.cfg('failOnError', True))

        if not workflow_id_raw:
            raise RuntimeError(
                "[SubWorkflow] Le champ 'workflowId' est requis.\n"
                "Renseignez l'ID du workflow à déclencher (visible dans l'URL du dashboard)."
            )

        try:
            workflow_id = int(workflow_id_raw)
        except ValueError:
            raise RuntimeError(
                f"[SubWorkflow] workflowId doit être un entier — reçu: '{workflow_id_raw}'"
            )

        # ── Import Django ORM (disponible dans le contexte du serveur) ────────
        from workflows.models import Workflow, Execution

        # Vérifier que le workflow cible existe
        try:
            target_workflow = Workflow.objects.get(pk=workflow_id)
        except Workflow.DoesNotExist:
            raise RuntimeError(
                f"[SubWorkflow] Workflow #{workflow_id} introuvable.\n"
                f"Vérifiez l'ID dans le dashboard."
            )

        # ── Construire le contexte à passer au sous-workflow ──────────────────
        sub_context = {}
        try:
            mapping = json.loads(context_pass) if context_pass else {}
            for sub_key, parent_key in mapping.items():
                if parent_key in self.context:
                    sub_context[sub_key] = self.context[parent_key]
        except (json.JSONDecodeError, TypeError):
            logger.warning('[SubWorkflow] contextPass JSON invalide — contexte vide passé')

        # Toujours passer l'instance_id parent pour la traçabilité
        sub_context['parent_execution_id'] = self.context.get('instance_id', '')

        # ── Créer et lancer l'exécution du sous-workflow ─────────────────────
        logger.info(
            f'[SubWorkflow] Lancement du workflow #{workflow_id} '
            f'("{target_workflow.name}") avec contexte: {sub_context}'
        )

        sub_execution = Execution.objects.create(
            workflow=target_workflow,
            triggered_by=f'sub_workflow:{self.context.get("instance_id", "?")}',
            context=sub_context,
        )

        # ── Exécution directe (synchrone) — évite le deadlock pool=solo ─────
        # Avec pool=solo, launch_execution_async() enqueue dans Celery mais le
        # worker est déjà occupé → le sous-workflow ne démarre jamais (deadlock).
        # On appelle _run_workflow() directement dans le même thread.
        from workflows.orchestrator import _run_workflow
        _run_workflow(sub_execution.id)

        # Lire le statut final après exécution synchrone
        sub_execution.refresh_from_db()
        final_status = sub_execution.status

        # Gérer le cas PAUSED (Human Task dans le sous-workflow)
        if final_status == 'PAUSED':
            raise RuntimeError(
                f"[SubWorkflow] Le sous-workflow #{workflow_id} est suspendu "
                f"sur une tâche humaine — les sous-workflows avec Human Task "
                f"ne sont pas supportés en mode synchrone."
            )

        if final_status == 'CANCELLED':
            raise RuntimeError(
                f"[SubWorkflow] Le sous-workflow #{workflow_id} a été annulé."
            )

        logger.info(
            f'[SubWorkflow] Exécution #{sub_execution.id} terminée — statut: {final_status}'
        )

        # ── Collecter les sorties du sous-workflow ────────────────────────────
        sub_outputs: dict = {}
        node_execs = sub_execution.node_executions.filter(status='DONE')
        for ne in node_execs:
            if ne.outputs:
                for k, v in ne.outputs.items():
                    sub_outputs[f'{output_prefix}{k}'] = v

        sub_outputs[f'{output_prefix}execution_id'] = sub_execution.id
        sub_outputs[f'{output_prefix}status']       = final_status
        sub_outputs[f'{output_prefix}workflow_id']  = workflow_id

        logger.info(
            f'[SubWorkflow] Workflow #{workflow_id} terminé — '
            f'statut: {final_status}  sorties: {list(sub_outputs.keys())}'
        )

        # ── Gestion de l'échec ────────────────────────────────────────────────
        if final_status != 'SUCCESS' and fail_on_error:
            raise RuntimeError(
                f"[SubWorkflow] Le workflow #{workflow_id} (\"{target_workflow.name}\") "
                f"a terminé avec le statut {final_status}.\n"
                f"Définissez 'failOnError = false' pour ignorer cet échec."
            )

        return sub_outputs
