import logging

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Workflow, Execution, NodeExecution, Approval, FormSubmission
from .serializers import (
    WorkflowListSerializer,
    WorkflowCreateSerializer,
    AuditLogSerializer,
)
from .orchestrator import (
    launch_execution_async,
    resume_after_approval,
    cancel_execution,
    validate_workflow,
)

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Workflow CRUD
# ══════════════════════════════════════════════════════════════════════════════

@api_view(['GET', 'POST'])
def workflow_list(request):
    """
    GET  /api/v2/workflows/   → Liste tous les workflows (dashboard)
    POST /api/v2/workflows/   → Crée un nouveau workflow
    """
    if request.method == 'GET':
        is_template_param = request.query_params.get('is_template')
        if is_template_param is not None:
            is_template = is_template_param.lower() == 'true'
            workflows = Workflow.objects.filter(is_template=is_template)
        else:
            workflows = Workflow.objects.all()
        return Response(WorkflowListSerializer(workflows, many=True).data)

    serializer = WorkflowCreateSerializer(data=request.data)
    if serializer.is_valid():
        name = serializer.validated_data.get('name', '')
        if not name.startswith('WF-'):
            serializer.validated_data['name'] = f'WF-{name}'
        workflow = serializer.save()
        return Response(WorkflowListSerializer(workflow).data, status=status.HTTP_201_CREATED)

    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@api_view(['GET', 'PUT', 'DELETE'])
def workflow_detail(request, pk):
    """
    GET    /api/v2/workflows/{id}/  → Détail workflow
    PUT    /api/v2/workflows/{id}/  → Mise à jour (incrémente version)
    DELETE /api/v2/workflows/{id}/  → Suppression
    """
    workflow = get_object_or_404(Workflow, pk=pk)

    if request.method == 'GET':
        return Response(WorkflowListSerializer(workflow).data)

    if request.method == 'PUT':
        serializer = WorkflowCreateSerializer(workflow, data=request.data, partial=True)
        if serializer.is_valid():
            instance = serializer.save()
            # Incrémenter la version à chaque mise à jour du canvas
            if 'canvas_nodes' in request.data or 'canvas_edges' in request.data:
                instance.version += 1
                instance.save(update_fields=['version'])
            return Response(WorkflowListSerializer(instance).data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    workflow.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


# ══════════════════════════════════════════════════════════════════════════════
# Exécution
# ══════════════════════════════════════════════════════════════════════════════

@api_view(['POST'])
def workflow_launch(request, pk):
    """
    POST /api/v2/workflows/{id}/launch/
    Body optionnel : { triggered_by, input_variables, context }

    Crée une Execution et lance l'orchestrateur dans un thread daemon.
    Retourne immédiatement — le frontend commence à poller /status/.
    """
    workflow = get_object_or_404(Workflow, pk=pk)

    # Un workflow ARCHIVED ne peut plus être lancé
    if workflow.status == 'ARCHIVED':
        return Response(
            {'error': 'Ce workflow est archivé et ne peut plus être exécuté.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    execution = Execution.objects.create(
        workflow=workflow,
        triggered_by=request.data.get('triggered_by', 'canvas'),
        input_variables=request.data.get('input_variables', {}),
        context=request.data.get('context', {}),
    )

    launch_execution_async(execution.id)

    return Response({
        'execution_id': execution.id,
        'workflow_id':  workflow.id,
        'status':       execution.status,
        'message':      'Exécution lancée en arrière-plan.',
    }, status=status.HTTP_202_ACCEPTED)


@api_view(['GET'])
def workflow_status(request, pk):
    """
    GET /api/v2/workflows/{id}/status/
    Pollé par React toutes les 2 secondes.
    Retourne le statut de l'exécution la plus récente + l'état de chaque nœud.
    """
    workflow  = get_object_or_404(Workflow, pk=pk)
    execution = workflow.executions.order_by('-started_at').first()

    if not execution:
        return Response({
            'workflow_definition_status': workflow.status,
            'execution_id':              None,
            'nodes':                     [],
        })

    node_execs = execution.node_executions.all()

    nodes_data = [
        {
            'node_id':       ne.node_id,
            'label':         ne.label,
            'status':        ne.status,
            'outputs':       ne.outputs,
            'error_message': ne.error_message,
            'step':          ne.step_number,
            'retry_count':   ne.retry_count,
        }
        for ne in node_execs
    ]

    return Response({
        'workflow_definition_status': workflow.status,
        'workflow_status':            execution.status,   # alias rétro-compat frontend
        'execution_id':               execution.id,
        'started_at':                 execution.started_at,
        'finished_at':                execution.finished_at,
        'error_message':              execution.error_message,
        'resume_from_node_id':        execution.resume_from_node_id,
        'total':                      node_execs.count(),
        'nodes':                      nodes_data,
    })


@api_view(['DELETE'])
def execution_cancel(request, execution_id):
    """
    DELETE /api/v2/workflows/executions/{execution_id}/cancel/
    Annule une exécution RUNNING ou PAUSED.
    """
    execution = get_object_or_404(Execution, pk=execution_id)
    cancelled = cancel_execution(execution_id)

    if cancelled:
        _log_audit(execution, 'EXECUTION_CANCELLED', {
            'actor': request.data.get('actor', 'user'),
        })
        return Response({'message': f'Exécution #{execution_id} annulée.'})

    return Response(
        {'error': f'Impossible d\'annuler — statut actuel : {execution.status}'},
        status=status.HTTP_409_CONFLICT,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Approbation Gate Task (multi-approbateurs)
# ══════════════════════════════════════════════════════════════════════════════

@api_view(['POST'])
def approve_node(request, execution_id, node_id):
    """
    POST /api/v2/workflows/executions/{execution_id}/approve/{node_id}/
    Body: {
        decision       : 'APPROVED' | 'REJECTED',
        approver_email : '...',
        comment        : '...',
        required_count : 1,          # optionnel — défaut 1
        approval_type  : 'ANY'|'ALL' # optionnel — défaut ANY
    }

    Logique multi-approbateurs :
      - ANY  : le premier APPROVED suffit pour reprendre
      - ALL  : required_count approbations nécessaires
      - Un REJECTED bloque immédiatement le workflow
    """
    execution = get_object_or_404(Execution, pk=execution_id)
    node_exec = get_object_or_404(NodeExecution, execution=execution, node_id=node_id)

    if node_exec.status not in ('WAITING',):
        return Response(
            {'error': f'Ce nœud n\'est pas en attente d\'approbation (statut: {node_exec.status})'},
            status=status.HTTP_409_CONFLICT,
        )

    decision       = request.data.get('decision', 'APPROVED').upper()
    approver_email = request.data.get('approver_email', '')
    comment        = request.data.get('comment', '')
    required_count = int(request.data.get('required_count', 1))
    approval_type  = request.data.get('approval_type', 'ANY').upper()

    if decision not in ('APPROVED', 'REJECTED'):
        return Response(
            {'error': 'decision doit être APPROVED ou REJECTED'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Créer l'enregistrement d'approbation (ForeignKey → plusieurs approbateurs possibles)
    Approval.objects.create(
        node_execution=node_exec,
        decision=decision,
        approver_email=approver_email,
        comment=comment,
        required_count=required_count,
        approval_type=approval_type,
    )

    # Évaluer la règle d'approbation
    gate_passed = Approval.is_gate_passed(node_exec)
    has_rejection = node_exec.approvals.filter(decision='REJECTED').exists()

    # Enrichir les outputs du nœud
    approved_emails = list(
        node_exec.approvals.filter(decision='APPROVED').values_list('approver_email', flat=True)
    )
    outputs = node_exec.outputs or {}
    outputs.update({
        'approval_decision':  decision,
        'approver_email':     approver_email,
        'comment':            comment,
        'approved_by':        approved_emails,
        'approval_type':      approval_type,
    })
    node_exec.outputs = outputs

    if has_rejection:
        # Rejet → workflow arrêté
        node_exec.status        = 'FAILED'
        node_exec.error_message = f'Rejeté par {approver_email}: {comment}'
        node_exec.save(update_fields=['status', 'outputs', 'error_message'])

        execution.status        = 'FAILED'
        execution.error_message = f'Nœud "{node_exec.label}" rejeté par {approver_email}'
        execution.save(update_fields=['status', 'error_message'])

        _log_audit(execution, 'NODE_REJECTED', {
            'node': node_exec.label, 'approver': approver_email, 'comment': comment,
        })
        return Response({'message': 'Nœud rejeté. Exécution arrêtée.', 'gate_passed': False})

    if gate_passed:
        # Règle satisfaite → reprendre
        node_exec.status = 'DONE'
        node_exec.save(update_fields=['status', 'outputs'])

        context = execution.context or {}
        context.update(outputs)
        execution.status              = 'RUNNING'
        execution.context             = context
        execution.resume_from_node_id = ''
        execution.save(update_fields=['status', 'context', 'resume_from_node_id'])

        resume_after_approval(execution.id)

        _log_audit(execution, 'NODE_APPROVED', {
            'node': node_exec.label, 'approver': approver_email,
            'approved_count': len(approved_emails),
        })
        return Response({
            'message':     f'Nœud approuvé. Reprise de l\'exécution #{execution.id}.',
            'gate_passed': True,
        })

    # Pas encore suffisamment d'approbations (mode ALL)
    node_exec.save(update_fields=['outputs'])
    approved_count = node_exec.approvals.filter(decision='APPROVED').count()
    return Response({
        'message':         f'Approbation enregistrée ({approved_count}/{required_count}). En attente.',
        'gate_passed':     False,
        'approved_count':  approved_count,
        'required_count':  required_count,
    })


# ══════════════════════════════════════════════════════════════════════════════
# Form Submission
# ══════════════════════════════════════════════════════════════════════════════

@api_view(['POST'])
def submit_form_node(request, execution_id, node_id):
    """
    POST /api/v2/workflows/executions/{execution_id}/form/{node_id}/
    Body: { form_data: { "field1": "value1", ... }, submitted_by: "user@..." }

    Persiste les données du formulaire dans FormSubmission + reprend l'exécution.
    """
    execution = get_object_or_404(Execution, pk=execution_id)
    node_exec = get_object_or_404(NodeExecution, execution=execution, node_id=node_id)

    if node_exec.status != 'WAITING':
        return Response(
            {'error': f'Ce nœud n\'est pas en attente de formulaire (statut: {node_exec.status})'},
            status=status.HTTP_409_CONFLICT,
        )

    form_data    = request.data.get('form_data', {})
    submitted_by = request.data.get('submitted_by', '')

    # Persister les données du formulaire (traçabilité / audit)
    FormSubmission.objects.update_or_create(
        node_execution=node_exec,
        defaults={
            'form_data':    form_data,
            'submitted_by': submitted_by,
        },
    )

    node_exec.outputs = form_data
    node_exec.status  = 'DONE'
    node_exec.save(update_fields=['outputs', 'status'])

    context = execution.context or {}
    context.update(form_data)
    execution.context             = context
    execution.status              = 'RUNNING'
    execution.resume_from_node_id = ''
    execution.save(update_fields=['status', 'context', 'resume_from_node_id'])

    _log_audit(execution, 'FORM_SUBMITTED', {
        'node':         node_exec.label,
        'submitted_by': submitted_by,
        'fields':       list(form_data.keys()),
    })

    resume_after_approval(execution.id)
    return Response({'message': f'Formulaire soumis. Reprise de l\'exécution #{execution.id}.'})


# ══════════════════════════════════════════════════════════════════════════════
# Validation
# ══════════════════════════════════════════════════════════════════════════════

@api_view(['GET'])
def workflow_validate(request, pk):
    """GET /api/v2/workflows/{id}/validate/"""
    return Response(validate_workflow(pk))


# ══════════════════════════════════════════════════════════════════════════════
# Journal d'audit
# ══════════════════════════════════════════════════════════════════════════════

@api_view(['GET'])
def execution_audit_log(request, execution_id):
    """GET /api/v2/workflows/executions/{execution_id}/logs/"""
    execution = get_object_or_404(Execution, pk=execution_id)
    serializer = AuditLogSerializer(execution.audit_logs.all(), many=True)
    return Response(serializer.data)


# ══════════════════════════════════════════════════════════════════════════════
# Helper interne
# ══════════════════════════════════════════════════════════════════════════════

def _log_audit(execution, event_type: str, data: dict):
    from .models import AuditLog
    AuditLog.objects.create(execution=execution, event_type=event_type, data=data)
