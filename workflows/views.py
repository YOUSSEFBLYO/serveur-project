import logging

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Workflow, WorkflowNode, WorkflowEdge, Execution, NodeExecution, Approval
from .serializers import (
    WorkflowListSerializer,
    WorkflowCreateSerializer,
    ExecutionStatusSerializer,
    ApprovalSerializer,
    AuditLogSerializer,
)
from .orchestrator import launch_execution_async, resume_after_approval, validate_workflow

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Workflow CRUD
# ══════════════════════════════════════════════════════════════════════════════

@api_view(['GET', 'POST'])
def workflow_list(request):
    """
    GET  /api/v2/workflows/   → List all workflows (dashboard)
    POST /api/v2/workflows/   → Create a new workflow
    """
    if request.method == 'GET':
        is_template_param = request.query_params.get('is_template')
        if is_template_param is not None:
            is_template = is_template_param.lower() == 'true'
            workflows = Workflow.objects.filter(is_template=is_template)
        else:
            # Default to backward compatibility or return all if not specified
            workflows = Workflow.objects.all()
        serializer = WorkflowListSerializer(workflows, many=True)
        return Response(serializer.data)

    # POST — create
    serializer = WorkflowCreateSerializer(data=request.data)
    if serializer.is_valid():
        # Auto-prefix WF- if missing
        name = serializer.validated_data.get('name', '')
        if not name.startswith('WF-'):
            serializer.validated_data['name'] = f'WF-{name}'

        workflow = serializer.save()
        _sync_canvas_to_nodes(workflow)
        return Response(WorkflowListSerializer(workflow).data, status=status.HTTP_201_CREATED)

    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@api_view(['GET', 'PUT', 'DELETE'])
def workflow_detail(request, pk):
    """
    GET    /api/v2/workflows/{id}/  → Get workflow details
    PUT    /api/v2/workflows/{id}/  → Update workflow
    DELETE /api/v2/workflows/{id}/  → Delete workflow
    """
    workflow = get_object_or_404(Workflow, pk=pk)

    if request.method == 'GET':
        return Response(WorkflowListSerializer(workflow).data)

    if request.method == 'PUT':
        serializer = WorkflowCreateSerializer(workflow, data=request.data, partial=True)
        if serializer.is_valid():
            workflow = serializer.save()
            _sync_canvas_to_nodes(workflow)
            return Response(WorkflowListSerializer(workflow).data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    if request.method == 'DELETE':
        workflow.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ══════════════════════════════════════════════════════════════════════════════
# Execution
# ══════════════════════════════════════════════════════════════════════════════

@api_view(['POST'])
def workflow_launch(request, pk):
    """
    POST /api/v2/workflows/{id}/launch/
    Creates an Execution and launches the orchestrator in a background thread.
    Returns immediately — React starts polling /status/.
    """
    workflow = get_object_or_404(Workflow, pk=pk)

    # Reset node statuses for fresh execution
    workflow.nodes.all().update(status='PENDING', outputs={}, error_message='')

    execution = Execution.objects.create(
        workflow=workflow,
        triggered_by=request.data.get('triggered_by', 'canvas'),
        context=request.data.get('context', {}),
    )

    # 🚀 Launch in background thread (replaces Celery)
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
    Polled by React every 2 seconds to animate the canvas in real-time.
    Returns current execution state + all node statuses.
    """
    workflow = get_object_or_404(Workflow, pk=pk)

    # Get the most recent execution
    execution = workflow.executions.order_by('-started_at').first()

    if not execution:
        return Response({
            'workflow_status': workflow.status,
            'execution_id':    None,
            'nodes':           [],
        })

    node_execs = execution.node_executions.select_related().all()

    nodes_data = [
        {
            'node_id':       ne.node_id,
            'label':         ne.label,
            'status':        ne.status,
            'outputs':       ne.outputs,
            'error_message': ne.error_message,
            'step':          ne.step_number,
        }
        for ne in node_execs
    ]

    return Response({
        'workflow_status': execution.status,
        'execution_id':    execution.id,
        'started_at':      execution.started_at,
        'finished_at':     execution.finished_at,
        'total':           node_execs.count(),
        'nodes':           nodes_data,
    })


# ══════════════════════════════════════════════════════════════════════════════
# Gate Task Approval
# ══════════════════════════════════════════════════════════════════════════════

@api_view(['POST'])
def approve_node(request, execution_id, node_id):
    """
    POST /api/v2/workflows/executions/{execution_id}/approve/{node_id}/
    Body: { decision: 'APPROVED' | 'REJECTED', approver_email: '...', comment: '...' }

    - If APPROVED: resumes the paused execution thread from the next node.
    - If REJECTED: marks the workflow as FAILED.
    """
    execution  = get_object_or_404(Execution, pk=execution_id)
    node_exec  = get_object_or_404(NodeExecution, execution=execution, node_id=node_id)

    decision        = request.data.get('decision', 'APPROVED')
    approver_email  = request.data.get('approver_email', '')
    comment         = request.data.get('comment', '')

    # Save the approval record
    Approval.objects.update_or_create(
        node_execution=node_exec,
        defaults={
            'decision':       decision,
            'approver_email': approver_email,
            'comment':        comment,
        }
    )

    if decision == 'APPROVED':
        # Mark node as DONE and resume thread
        node_exec.status = 'DONE'
        node_exec.save(update_fields=['status'])

        # Also update the WorkflowNode status
        wf_node = execution.workflow.nodes.filter(node_id=node_id).first()
        if wf_node:
            wf_node.status = 'DONE'
            wf_node.save(update_fields=['status'])

        # Un-pause execution
        execution.status = 'RUNNING'
        execution.save(update_fields=['status'])

        # Relaunch orchestrator — it will skip DONE nodes and continue
        resume_after_approval(execution.id)

        return Response({'message': f'Nœud approuvé. Reprise de l\'exécution #{execution.id}.'})

    else:
        # REJECTED — fail the workflow
        node_exec.status = 'FAILED'
        node_exec.error_message = f'Rejeté par {approver_email}: {comment}'
        node_exec.save(update_fields=['status', 'error_message'])

        execution.status = 'FAILED'
        execution.save(update_fields=['status'])

        execution.workflow.status = 'FAILED'
        execution.workflow.save(update_fields=['status'])

        return Response({'message': 'Nœud rejeté. Exécution arrêtée.'})


# ══════════════════════════════════════════════════════════════════════════════
# Validation
# ══════════════════════════════════════════════════════════════════════════════

@api_view(['GET'])
def workflow_validate(request, pk):
    """
    GET /api/v2/workflows/{id}/validate/
    Returns graph validation result without executing.
    """
    result = validate_workflow(pk)
    return Response(result)


# ══════════════════════════════════════════════════════════════════════════════
# Audit Log
# ══════════════════════════════════════════════════════════════════════════════

@api_view(['GET'])
def execution_audit_log(request, execution_id):
    """
    GET /api/v2/workflows/executions/{execution_id}/logs/
    """
    execution = get_object_or_404(Execution, pk=execution_id)
    logs = execution.audit_logs.all()
    serializer = AuditLogSerializer(logs, many=True)
    return Response(serializer.data)


# ══════════════════════════════════════════════════════════════════════════════
# Internal Helper
# ══════════════════════════════════════════════════════════════════════════════

def _sync_canvas_to_nodes(workflow: Workflow):
    """
    Extracts nodes/edges from canvas_nodes/canvas_edges JSON
    and populates the WorkflowNode / WorkflowEdge relational tables.
    Called after create/update so the orchestrator can use FK queries.
    """
    canvas_nodes = workflow.canvas_nodes or []
    canvas_edges = workflow.canvas_edges or []

    if not canvas_nodes:
        return

    # Clear old records
    workflow.nodes.all().delete()
    workflow.edges.all().delete()

    for i, cn in enumerate(canvas_nodes):
        node_data = cn.get('data', {})
        WorkflowNode.objects.create(
            workflow=workflow,
            node_id=cn.get('id', f'node_{i}'),
            node_type=node_data.get('type', 'unknown'),
            label=node_data.get('label', ''),
            config=node_data,
            position=cn.get('position', {'x': 0, 'y': 0}),
            order=i,
        )

    for ce in canvas_edges:
        WorkflowEdge.objects.create(
            workflow=workflow,
            edge_id=ce.get('id', ''),
            source=ce.get('source', ''),
            target=ce.get('target', ''),
            label=ce.get('label', ''),
        )
