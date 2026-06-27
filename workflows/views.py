import logging

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Workflow, Execution, NodeExecution, Approval, FormSubmission
from django.utils.dateparse import parse_date
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


@api_view(['GET'])
def workflow_export(request, pk):
    """
    GET /api/v2/workflows/{id}/export/
    Exporte le workflow complet en JSON pour sauvegarde ou partage.
    """
    workflow = get_object_or_404(Workflow, pk=pk)
    
    # On exporte les champs essentiels et les canvas_nodes/edges
    export_data = {
        "export_version": 1,
        "name": workflow.name,
        "description": workflow.description,
        "workflow_type": workflow.workflow_type,
        "priority": workflow.priority,
        "is_template": workflow.is_template,
        "canvas_nodes": workflow.canvas_nodes,
        "canvas_edges": workflow.canvas_edges,
    }
    return Response(export_data)


@api_view(['POST'])
def workflow_import(request):
    """
    POST /api/v2/workflows/import/
    Importe un JSON et crée un nouveau workflow.
    """
    data = request.data

    # Validation simple
    if not isinstance(data, dict) or "canvas_nodes" not in data:
        return Response({"detail": "Format d'export invalide. 'canvas_nodes' est requis."}, status=status.HTTP_400_BAD_REQUEST)

    # Ajout du préfixe [Importé] pour éviter les conflits de noms, ou on prend celui fourni
    name = data.get('name', 'Workflow Importé')
    if not name.startswith('[Importé]'):
        name = f"[Importé] {name}"

    workflow_data = {
        "name": name,
        "description": data.get('description', ''),
        "workflow_type": data.get('workflow_type', 'Déploiement'),
        "priority": data.get('priority', 'MEDIUM'),
        "is_template": data.get('is_template', False),
        "canvas_nodes": data.get('canvas_nodes', []),
        "canvas_edges": data.get('canvas_edges', []),
        "creator_name": request.user.username if request.user.is_authenticated else "Admin"
    }

    serializer = WorkflowCreateSerializer(data=workflow_data)
    if serializer.is_valid():
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ══════════════════════════════════════════════════════════════════════════════
# Exécution & Status
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

    try:
        launch_execution_async(execution.id)
    except Exception as broker_error:
        # Redis / Celery indisponible → annuler l'exécution créée
        execution.status        = 'FAILED'
        execution.error_message = f'Broker indisponible : {broker_error}'
        execution.save(update_fields=['status', 'error_message'])
        return Response(
            {'error': f'Impossible de joindre le broker Celery : {broker_error}'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return Response({
        'execution_id': execution.id,
        'workflow_id':  workflow.id,
        'status':       execution.status,
        'message':      'Exécution envoyée au worker Celery.',
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

    # ── Fix race condition — select_for_update() verrouille la ligne Execution ──
    # Si deux approbateurs cliquent simultanément en mode ANY, un seul thread
    # entre dans le bloc atomique et lance Celery ; le second trouve node_exec.status='DONE'
    # et retourne 409 immédiatement.
    from django.db import transaction

    with transaction.atomic():
        # Verrouiller l'Execution pour éviter deux lancements simultanés
        execution = Execution.objects.select_for_update().get(pk=execution_id)
        node_exec = get_object_or_404(NodeExecution, execution=execution, node_id=node_id)

        if node_exec.status not in ('WAITING',):
            return Response(
                {'error': f'Ce nœud n\'est pas en attente d\'approbation (statut: {node_exec.status})'},
                status=status.HTTP_409_CONFLICT,
            )

        # Créer l'enregistrement d'approbation
        Approval.objects.create(
            node_execution=node_exec,
            decision=decision,
            approver_email=approver_email,
            comment=comment,
            required_count=required_count,
            approval_type=approval_type,
        )

        # Évaluer la règle d'approbation
        gate_passed   = Approval.is_gate_passed(node_exec)
        has_rejection = node_exec.approvals.filter(decision='REJECTED').exists()

        # Enrichir les outputs du nœud
        approved_emails = list(
            node_exec.approvals.filter(decision='APPROVED').values_list('approver_email', flat=True)
        )
        outputs = node_exec.outputs or {}
        outputs.update({
            'approval_decision': decision,
            'approver_email':    approver_email,
            'comment':           comment,
            'approved_by':       approved_emails,
            'approval_type':     approval_type,
        })
        node_exec.outputs = outputs

        if has_rejection:
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
            # Marquer DONE avant de sortir du bloc atomique → le second thread verra DONE
            node_exec.status = 'DONE'
            node_exec.save(update_fields=['status', 'outputs'])

            ctx = execution.context or {}
            ctx.update(outputs)
            execution.status              = 'RUNNING'
            execution.context             = ctx
            execution.resume_from_node_id = ''
            execution.save(update_fields=['status', 'context', 'resume_from_node_id'])

            _log_audit(execution, 'NODE_APPROVED', {
                'node': node_exec.label, 'approver': approver_email,
                'approved_count': len(approved_emails),
            })

        # ── Lancer Celery EN DEHORS du bloc atomic pour éviter un deadlock ──
        # (Celery essaierait de lire l'Execution avant que la transaction ne soit commitée)

    if gate_passed and not has_rejection:
        resume_after_approval(execution.id)
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
# Historique global des exécutions
# ══════════════════════════════════════════════════════════════════════════════

@api_view(['GET'])
def execution_list(request):
    """
    GET /api/v2/workflows/executions/
    Retourne l'historique complet de toutes les exécutions.

    Query params optionnels :
        status       = RUNNING | SUCCESS | FAILED | PAUSED | CANCELLED | PENDING
        workflow_id  = <int>
        date_from    = YYYY-MM-DD
        date_to      = YYYY-MM-DD
        limit        = <int>  (défaut 100)
    """
    qs = Execution.objects.select_related('workflow').order_by('-started_at')

    # ── Filtres ──────────────────────────────────────────────────────────────
    status_filter = request.query_params.get('status')
    if status_filter:
        qs = qs.filter(status=status_filter.upper())

    workflow_id = request.query_params.get('workflow_id')
    if workflow_id:
        qs = qs.filter(workflow_id=workflow_id)

    date_from = request.query_params.get('date_from')
    if date_from:
        parsed = parse_date(date_from)
        if parsed:
            qs = qs.filter(started_at__date__gte=parsed)

    date_to = request.query_params.get('date_to')
    if date_to:
        parsed = parse_date(date_to)
        if parsed:
            qs = qs.filter(started_at__date__lte=parsed)

    try:
        limit = int(request.query_params.get('limit', 100))
        limit = min(max(limit, 1), 500)   # borne entre 1 et 500
    except ValueError:
        limit = 100

    qs = qs[:limit]

    # ── Sérialisation manuelle (pas de sérialiseur dédié pour rester léger) ─
    data = []
    for ex in qs:
        # Durée en secondes
        duration_s = None
        if ex.started_at and ex.finished_at:
            duration_s = int((ex.finished_at - ex.started_at).total_seconds())

        data.append({
            'id':             ex.id,
            'workflow_id':    ex.workflow_id,
            'workflow_name':  ex.workflow.name,
            'workflow_type':  ex.workflow.workflow_type,
            'status':         ex.status,
            'triggered_by':   ex.triggered_by,
            'started_at':     ex.started_at,
            'finished_at':    ex.finished_at,
            'duration_s':     duration_s,
            'error_message':  ex.error_message,
            'node_count':     ex.node_executions.count(),
        })

    return Response({'count': len(data), 'results': data})


@api_view(['GET'])
def execution_detail(request, execution_id):
    """
    GET /api/v2/workflows/executions/{execution_id}/detail/
    Retourne les NodeExecution + AuditLog d'une exécution passée.
    """
    execution = get_object_or_404(Execution, pk=execution_id)

    node_execs = [
        {
            'node_id':       ne.node_id,
            'label':         ne.label,
            'node_type':     ne.node_type,
            'status':        ne.status,
            'outputs':       ne.outputs,
            'error_message': ne.error_message,
            'started_at':    ne.started_at,
            'finished_at':   ne.finished_at,
            'step_number':   ne.step_number,
            'retry_count':   ne.retry_count,
        }
        for ne in execution.node_executions.order_by('step_number')
    ]

    audit_logs = AuditLogSerializer(execution.audit_logs.all(), many=True).data

    duration_s = None
    if execution.started_at and execution.finished_at:
        duration_s = int((execution.finished_at - execution.started_at).total_seconds())

    return Response({
        'id':             execution.id,
        'workflow_id':    execution.workflow_id,
        'workflow_name':  execution.workflow.name,
        'status':         execution.status,
        'triggered_by':   execution.triggered_by,
        'started_at':     execution.started_at,
        'finished_at':    execution.finished_at,
        'duration_s':     duration_s,
        'error_message':  execution.error_message,
        'input_variables': execution.input_variables,
        'node_executions': node_execs,
        'audit_logs':      audit_logs,
    })


# ══════════════════════════════════════════════════════════════════════════════
# Rapport AIOps — rapport HTML d’une exécution
# ══════════════════════════════════════════════════════════════════════════════

@api_view(['GET'])
def execution_report(request, execution_id):
    """
    GET /api/v2/workflows/executions/{execution_id}/report/
    Cherche le rapport HTML généré par un nœud ReportGenerator dans les outputs.
    Retourne : { has_report, html, node_label, health, generated_at }
    """
    execution = get_object_or_404(Execution, pk=execution_id)

    # Cherche le premier nœud de type aiops.ReportGenerator (ou variantes) ou contenant un rapport
    report_node = None
    for ne in execution.node_executions.order_by('step_number'):
        outputs = ne.outputs or {}
        # Vérifie s'il y a un rapport HTML direct ou si c'est un nœud ReportGenerator avec un rapport
        has_report_keys = any(k in outputs for k in ('aiops_report_html', 'report_html', 'aiops_report', 'report'))
        is_report_node = ne.node_type in ('aiops.ReportGenerator', 'aiops_report_generator', 'AIOpsReportGenerator')
        if has_report_keys or is_report_node:
            report_node = ne
            break

    if not report_node:
        return Response({
            'has_report': False,
            'html':        None,
            'node_label':  None,
        })

    outputs = report_node.outputs or {}
    html_content = (
        outputs.get('aiops_report_html')
        or outputs.get('report_html')
        or outputs.get('aiops_report')
        or outputs.get('report')
        or ''
    )

    health_status = outputs.get('report_health') or outputs.get('health') or 'UNKNOWN'

    return Response({
        'has_report':   bool(html_content),
        'html':         html_content,
        'node_label':   report_node.label,
        'health':       health_status,
        'generated_at': report_node.finished_at,
        'execution_id': execution_id,
        'workflow_name': execution.workflow.name,
    })


# ══════════════════════════════════════════════════════════════════════════════
# Helper interne
# ══════════════════════════════════════════════════════════════════════════════

def _log_audit(execution, event_type: str, data: dict):
    from .models import AuditLog
    AuditLog.objects.create(execution=execution, event_type=event_type, data=data)
