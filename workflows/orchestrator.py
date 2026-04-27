"""
Orchestrator — Pure Python threading implementation.
Replaces Celery + Redis with native threading + DB polling.
No external dependencies required.
"""
import logging
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── Django setup guard (needed when called from threads) ──────────────────────
import django
from django.db import connection


def _close_db():
    """Close DB connection after thread ends (avoids connection leaks)."""
    connection.close()


# ═════════════════════════════════════════════════════════════════════════════
# Graph utilities
# ═════════════════════════════════════════════════════════════════════════════

def _topological_order(nodes: list, edges: list) -> list:
    """Kahn's algorithm — returns node_ids in execution order."""
    node_ids  = [n.node_id for n in nodes]
    in_degree = {nid: 0 for nid in node_ids}
    graph     = defaultdict(list)

    for edge in edges:
        s, t = edge.source, edge.target
        if s in in_degree and t in in_degree:
            graph[s].append(t)
            in_degree[t] += 1

    queue = deque([nid for nid, deg in in_degree.items() if deg == 0])
    order = []
    while queue:
        nid = queue.popleft()
        order.append(nid)
        for neighbor in graph[nid]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    # Append disconnected nodes at the end
    for nid in node_ids:
        if nid not in order:
            order.append(nid)

    return order


def _validate_graph(nodes: list, edges: list) -> dict:
    """Validate the graph for cycles and connectivity."""
    order = _topological_order(nodes, edges)
    has_cycle = len(order) != len(nodes)
    warnings = []
    if has_cycle:
        warnings.append("Cycle détecté dans le graphe.")
    if not nodes:
        warnings.append("Le workflow ne contient aucun nœud.")
    return {
        'valid':      not has_cycle and bool(nodes),
        'node_count': len(nodes),
        'edge_count': len(edges),
        'has_cycle':  has_cycle,
        'warnings':   warnings,
    }


# ═════════════════════════════════════════════════════════════════════════════
# Audit helper
# ═════════════════════════════════════════════════════════════════════════════

def _log_event(execution, event_type: str, data: dict = None):
    from .models import AuditLog
    AuditLog.objects.create(
        execution=execution,
        event_type=event_type,
        data=data or {},
    )


# ═════════════════════════════════════════════════════════════════════════════
# Core orchestrator — runs in a background thread
# ═════════════════════════════════════════════════════════════════════════════

def _run_workflow(execution_id: int):
    """
    Main execution loop. Runs in a background Python thread.
    Reads graph, executes nodes in topological order,
    saves status to DB at every step (frontend polls /status/).
    """
    try:
        from .models import Workflow, WorkflowNode, WorkflowEdge, Execution, NodeExecution, AuditLog
        from .executors import get_executor
        from .executors.base import SuspendExecution

        execution = Execution.objects.get(pk=execution_id)
        workflow  = execution.workflow

        execution.status = 'RUNNING'
        execution.save(update_fields=['status'])
        workflow.status = 'RUNNING'
        workflow.save(update_fields=['status'])

        nodes = list(workflow.nodes.all())
        edges = list(workflow.edges.all())
        total = len(nodes)

        ordered_ids = _topological_order(nodes, edges)
        node_map    = {n.node_id: n for n in nodes}

        # Create NodeExecution records (or get existing ones for resume)
        node_exec_map = {}
        for i, nid in enumerate(ordered_ids):
            node = node_map.get(nid)
            if node:
                ne, _ = NodeExecution.objects.get_or_create(
                    execution=execution,
                    node_id=node.node_id,
                    defaults={
                        'node_type':   node.node_type,
                        'label':       node.label,
                        'status':      'PENDING',
                        'step_number': i,
                    }
                )
                node_exec_map[nid] = ne

        _log_event(execution, 'EXECUTION_STARTED', {
            'workflow': workflow.name,
            'total_nodes': total,
        })

        # ── Cumulative runtime context ────────────────────────────────────
        context = dict(execution.context or {})
        context['instance_id'] = str(execution.id)

        overall_success = True

        for step, node_id in enumerate(ordered_ids, start=1):
            node = node_map.get(node_id)
            if node is None:
                continue

            # Skip already-processed nodes (resume after approval)
            if node.status in ['DONE', 'SUCCESS', 'SKIPPED']:
                continue

            node_exec = node_exec_map.get(node_id)

            # ── Mark RUNNING ──────────────────────────────────────────────
            node.status     = 'RUNNING'
            node.started_at = datetime.now(timezone.utc)
            node.save(update_fields=['status', 'started_at'])

            if node_exec:
                node_exec.status     = 'RUNNING'
                node_exec.started_at = node.started_at
                node_exec.save(update_fields=['status', 'started_at'])

            _log_event(execution, 'NODE_STARTED', {
                'node': node.label,
                'type': node.node_type,
                'step': step,
            })

            # Small delay so the frontend animation is visible
            time.sleep(1.5)

            # ── Run executor ──────────────────────────────────────────────
            executor_cls = get_executor(node.node_type)
            executor     = executor_cls(node, context)

            try:
                outputs = executor.run()
                node.status        = 'DONE'
                node.outputs       = outputs or {}
                node.error_message = ''
                # Propagate outputs to downstream nodes
                context.update(node.outputs)

            except SuspendExecution as exc:
                # Gate Task — pause here, wait for human approval
                logger.info(f'Node {node.label} suspended: {exc}')
                node.status        = 'WAITING'
                node.error_message = str(exc)
                node.save(update_fields=['status', 'error_message'])

                if node_exec:
                    node_exec.status        = 'WAITING'
                    node_exec.error_message = str(exc)
                    node_exec.save(update_fields=['status', 'error_message'])

                # Pause execution — workflow stays RUNNING but thread exits
                execution.status = 'PAUSED'
                execution.save(update_fields=['status'])
                _log_event(execution, 'NODE_SUSPENDED', {
                    'node': node.label,
                    'reason': str(exc),
                })
                logger.info(f'Workflow #{workflow.id} paused on node "{node.label}"')
                return  # Thread exits, frontend resumes polling

            except Exception as exc:
                logger.exception(f'Node "{node.label}" failed: {exc}')
                node.status        = 'FAILED'
                node.error_message = str(exc)
                overall_success    = False

            # ── Save final node state ─────────────────────────────────────
            if node.status in ['DONE', 'FAILED']:
                node.finished_at = datetime.now(timezone.utc)
                node.save(update_fields=['status', 'outputs', 'error_message', 'finished_at'])

            if node_exec:
                node_exec.status        = node.status
                node_exec.outputs       = node.outputs
                node_exec.error_message = node.error_message
                node_exec.finished_at   = getattr(node, 'finished_at', None)
                node_exec.save(update_fields=['status', 'outputs', 'error_message', 'finished_at'])

            _log_event(execution, f'NODE_{node.status}', {
                'node':    node.label,
                'type':    node.node_type,
                'step':    step,
                'outputs': node.outputs,
            })

            # Stop on failure — skip all remaining nodes
            if node.status == 'FAILED':
                remaining_ids = ordered_ids[ordered_ids.index(node_id) + 1:]
                for rem_id in remaining_ids:
                    rem = node_map.get(rem_id)
                    if rem:
                        rem.status = 'SKIPPED'
                        rem.save(update_fields=['status'])
                    ne = node_exec_map.get(rem_id)
                    if ne:
                        ne.status = 'SKIPPED'
                        ne.save(update_fields=['status'])
                break

        # ── Finalize workflow ─────────────────────────────────────────────
        final_status = 'SUCCESS' if overall_success else 'FAILED'
        workflow.status   = final_status
        workflow.save(update_fields=['status'])

        execution.status      = final_status
        execution.finished_at = datetime.now(timezone.utc)
        execution.save(update_fields=['status', 'finished_at'])

        _log_event(execution, 'EXECUTION_COMPLETE', {'status': final_status})
        logger.info(f'Workflow #{workflow.id} execution #{execution.id} → {final_status}')

    except Exception as e:
        logger.exception(f'Fatal orchestrator error for execution #{execution_id}: {e}')
    finally:
        _close_db()  # Always release DB connection when thread ends


# ═════════════════════════════════════════════════════════════════════════════
# Public API — called from views.py
# ═════════════════════════════════════════════════════════════════════════════

def launch_execution_async(execution_id: int):
    """Launch the workflow orchestrator in a background thread (no Celery needed)."""
    thread = threading.Thread(
        target=_run_workflow,
        args=(execution_id,),
        name=f'wf-execution-{execution_id}',
        daemon=True,  # Thread dies if the server process shuts down
    )
    thread.start()
    logger.info(f'Execution #{execution_id} launched in thread {thread.name}')
    return thread


def resume_after_approval(execution_id: int):
    """
    Resume a PAUSED workflow after a Gate Task is approved.
    Simply relaunch the thread — the orchestrator skips DONE nodes automatically.
    """
    logger.info(f'Resuming execution #{execution_id} after approval')
    launch_execution_async(execution_id)


def validate_workflow(workflow_id: int) -> dict:
    """Validate the workflow graph without executing it."""
    from .models import Workflow
    try:
        workflow = Workflow.objects.get(pk=workflow_id)
        nodes = list(workflow.nodes.all())
        edges = list(workflow.edges.all())
        return _validate_graph(nodes, edges)
    except Workflow.DoesNotExist:
        return {'valid': False, 'warnings': ['Workflow introuvable'], 'node_count': 0, 'edge_count': 0}
