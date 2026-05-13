"""
Orchestrator — Celery + Redis.

Flux :
  canvas_nodes / canvas_edges (JSON brut)
      → _NodeProxy / _EdgeProxy (objets légers en mémoire)
      → tri topologique de Kahn
      → executor.run() pour chaque nœud
      → NodeExecution (DB) mis à jour à chaque étape
      → frontend poll /status/ toutes les 2s → NodeExecution

Exécution asynchrone :
  - launch_execution_async() envoie une tâche Celery (Redis broker)
  - Le worker Celery exécute _run_workflow() en arrière-plan
  - threading reste utilisé UNIQUEMENT pour les branches parallèles (Fork/Join)
"""
import logging
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

import django
from django.db import connection


def _close_db():
    """Ferme la connexion DB après la fin du thread (évite les fuites)."""
    connection.close()


# ═════════════════════════════════════════════════════════════════════════════
# Proxies légers
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class _NodeProxy:
    node_id:       str
    node_type:     str
    label:         str
    config:        dict
    order:         int = 0
    status:        str  = 'PENDING'
    outputs:       dict = field(default_factory=dict)
    error_message: str  = ''
    started_at:    Any  = None
    finished_at:   Any  = None


@dataclass
class _EdgeProxy:
    source:       str
    target:       str
    sourceHandle: str = ''
    edge_id:      str = ''
    label:        str = ''


def _canvas_to_proxies(canvas_nodes: list, canvas_edges: list):
    nodes = [
        _NodeProxy(
            node_id=cn.get('id', f'node_{i}'),
            node_type=cn.get('data', {}).get('type', 'unknown'),
            label=cn.get('data', {}).get('label', f'Nœud {i + 1}'),
            config=cn.get('data', {}),
            order=i,
        )
        for i, cn in enumerate(canvas_nodes)
    ]
    edges = [
        _EdgeProxy(
            edge_id=ce.get('id', ''),
            source=ce.get('source', ''),
            target=ce.get('target', ''),
            sourceHandle=ce.get('sourceHandle', ''),
            label=ce.get('label', ''),
        )
        for ce in canvas_edges
    ]
    return nodes, edges


# ═════════════════════════════════════════════════════════════════════════════
# Utilitaires de graphe
# ═════════════════════════════════════════════════════════════════════════════

def _topological_order(nodes: list, edges: list) -> list:
    """Algorithme de Kahn — retourne les node_id dans l'ordre d'exécution."""
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

    for nid in node_ids:
        if nid not in order:
            order.append(nid)

    return order


def _validate_graph(nodes: list, edges: list) -> dict:
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


def _reachable_from(start_node_id: str, edges: list) -> set:
    """Retourne tous les node_id atteignables depuis start_node_id (BFS)."""
    visited = set()
    queue = deque([start_node_id])
    while queue:
        curr = queue.popleft()
        if curr in visited:
            continue
        visited.add(curr)
        for edge in edges:
            if edge.source == curr:
                queue.append(edge.target)
    return visited


def _skip_downstream(start_node_id: str, edges: list, node_map: dict,
                     node_exec_map: dict, ordered_ids: list,
                     protected: set = None):
    """
    Marque récursivement un nœud et ses descendants comme SKIPPED.
    `protected` = ensemble de node_id qui ne doivent PAS être ignorés.
    """
    if protected is None:
        protected = set()

    queue = deque([start_node_id])
    visited = set()
    while queue:
        curr = queue.popleft()
        if curr in visited or curr in protected:
            continue
        visited.add(curr)

        node = node_map.get(curr)
        if node and node.status in ('PENDING', 'RUNNING', 'WAITING'):
            node.status = 'SKIPPED'

        ne = node_exec_map.get(curr)
        if ne and ne.status in ('PENDING', 'RUNNING', 'WAITING'):
            ne.status = 'SKIPPED'
            ne.save(update_fields=['status'])

        for edge in edges:
            if edge.source == curr and edge.target not in protected:
                queue.append(edge.target)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers Parallélisme (Fork / Join)
# ═══════════════════════════════════════════════════════════════════════════════

def _find_join_node(branch_starts: list, edges: list, node_map: dict) -> str | None:
    if not branch_starts:
        return None

    reachable_sets = [_reachable_from(start, edges) for start in branch_starts]

    common = reachable_sets[0]
    for rs in reachable_sets[1:]:
        common = common & rs

    for node_id in common:
        node = node_map.get(node_id)
        if node and node.node_type == 'parallel_join':
            return node_id

    return None


def _collect_branch_nodes(start_id: str, join_id: str | None,
                          edges: list, node_map: dict) -> list:
    result = []
    visited = set()
    queue = deque([start_id])

    while queue:
        curr = queue.popleft()
        if curr in visited:
            continue
        if curr == join_id:
            continue
        visited.add(curr)

        node = node_map.get(curr)
        if node:
            result.append(node)

        for edge in edges:
            if edge.source == curr:
                queue.append(edge.target)

    return result


def _execute_parallel_branches(
    fork_node_id: str,
    edges: list,
    node_map: dict,
    node_exec_map: dict,
    context: dict,
    execution,
    handled_in_parallel: set,
    suspended_info: list,          # [FIX] liste partagée pour signaler une suspension
):
    """
    Exécute toutes les branches sortant d'un parallel_fork en threads séparés.
    Si une branche rencontre SuspendExecution → marque le nœud WAITING
    et remonte l'info dans suspended_info (ne marque plus FAILED).
    """
    from .executors import get_executor
    from .executors.base import SuspendExecution

    branch_starts = [e.target for e in edges if e.source == fork_node_id]

    if not branch_starts:
        logger.warning(f'[Parallel] Fork {fork_node_id} sans branches sortantes')
        return

    join_node_id = _find_join_node(branch_starts, edges, node_map)
    logger.info(
        f'[Parallel] Fork {fork_node_id} → {len(branch_starts)} branche(s)  '
        f'Join: {join_node_id or "(non trouvé)"}'
    )

    branch_node_lists = [
        _collect_branch_nodes(start, join_node_id, edges, node_map)
        for start in branch_starts
    ]

    for branch in branch_node_lists:
        for bn in branch:
            handled_in_parallel.add(bn.node_id)

    branch_contexts = [{} for _ in branch_node_lists]
    lock = threading.Lock()

    def run_branch(idx: int, branch_nodes: list, base_ctx: dict):
        local_ctx = dict(base_ctx)
        for bn in branch_nodes:
            ne = node_exec_map.get(bn.node_id)

            now = datetime.now(timezone.utc)
            bn.status     = 'RUNNING'
            bn.started_at = now
            if ne:
                ne.status     = 'RUNNING'
                ne.started_at = now
                ne.save(update_fields=['status', 'started_at'])

            _log_event(execution, 'NODE_STARTED', {
                'node': bn.label, 'type': bn.node_type, 'parallel_branch': idx,
            })

            executor_cls = get_executor(bn.node_type)
            executor     = executor_cls(bn, local_ctx)

            try:
                outputs        = executor.run()
                bn.status      = 'DONE'
                bn.outputs     = outputs or {}
                bn.error_message = ''
                local_ctx.update(bn.outputs)

            except SuspendExecution as exc:
                # [FIX] Branche suspendue → WAITING, pas FAILED
                logger.info(
                    f'[Parallel] Branche {idx} — nœud "{bn.label}" suspendu : {exc}'
                )
                bn.status        = 'WAITING'
                bn.error_message = str(exc)

                if ne:
                    ne.status        = 'WAITING'
                    ne.error_message = str(exc)
                    ne.save(update_fields=['status', 'error_message'])

                _log_event(execution, 'NODE_SUSPENDED', {
                    'node': bn.label, 'type': bn.node_type,
                    'parallel_branch': idx, 'reason': str(exc),
                })

                with lock:
                    suspended_info.append({
                        'node_id':  bn.node_id,
                        'node':     bn.label,
                        'reason':   str(exc),
                        'branch':   idx,
                    })
                return   # Arrêt de cette branche, pas de propagation

            except Exception as exc:
                logger.error(f'[Parallel] Branche {idx} — nœud "{bn.label}" échoué: {exc}')
                bn.status        = 'FAILED'
                bn.error_message = str(exc)

            finished = datetime.now(timezone.utc) if bn.status in ('DONE', 'FAILED') else None
            bn.finished_at = finished
            if ne:
                ne.status        = bn.status
                ne.outputs       = bn.outputs
                ne.error_message = bn.error_message
                ne.finished_at   = finished
                ne.save(update_fields=['status', 'outputs', 'error_message', 'finished_at'])

            _log_event(execution, f'NODE_{bn.status}', {
                'node': bn.label, 'type': bn.node_type, 'parallel_branch': idx,
            })

        with lock:
            branch_contexts[idx] = local_ctx

    threads = [
        threading.Thread(
            target=run_branch,
            args=(i, branch, dict(context)),
            name=f'wf-parallel-branch-{i}',
            daemon=True,
        )
        for i, branch in enumerate(branch_node_lists)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for bctx in branch_contexts:
        context.update(bctx)

    logger.info(
        f'[Parallel] Toutes les branches terminées '
        f'(suspensions: {len(suspended_info)})'
    )


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


def _is_cancelled(execution_id: int) -> bool:
    """Vérifie en DB si l'exécution a été annulée entre deux nœuds."""
    from .models import Execution
    try:
        return Execution.objects.filter(
            pk=execution_id, status='CANCELLED'
        ).exists()
    except Exception:
        return False


# ═════════════════════════════════════════════════════════════════════════════
# Boucle d'exécution principale — thread daemon
# ═════════════════════════════════════════════════════════════════════════════

def _run_workflow(execution_id: int):
    """
    Boucle principale d'exécution.
    - Ne touche PLUS à workflow.status (statut de définition, pas d'exécution)
    - Suppression des time.sleep() visuels (le polling /status/ toutes les 2s suffit)
    - Stocke resume_from_node_id sur suspension
    - Vérifie CANCELLED à chaque étape
    """
    try:
        from .models import Execution, NodeExecution
        from .executors import get_executor
        from .executors.base import SuspendExecution

        execution = Execution.objects.get(pk=execution_id)
        workflow  = execution.workflow

        # ── Fix idempotence — guard contre le rejeu Celery (acks_late + Redis AOF) ──
        # Avec acks_late=True, si le worker crashe après avoir démarré la tâche mais
        # avant d'acquitter, Celery remet la tâche dans la file au redémarrage.
        # Si l'exécution est déjà dans un état terminal, on sort immédiatement.
        TERMINAL_STATES = {'SUCCESS', 'FAILED', 'CANCELLED'}
        if execution.status in TERMINAL_STATES:
            logger.warning(
                f'[Orchestrator] Exécution #{execution_id} déjà en état terminal '
                f'"{execution.status}" — tâche Celery ignorée (rejeu détecté).'
            )
            return

        execution.status = 'RUNNING'
        execution.save(update_fields=['status'])

        # ── Construction des proxies depuis le JSON brut ──────────────────
        nodes, edges = _canvas_to_proxies(
            workflow.canvas_nodes or [],
            workflow.canvas_edges or [],
        )
        total = len(nodes)

        ordered_ids = _topological_order(nodes, edges)
        node_map    = {n.node_id: n for n in nodes}

        # ── Création des NodeExecution (get_or_create pour la reprise) ────
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
            'workflow':    workflow.name,
            'total_nodes': total,
        })

        # ── Contexte runtime (input_variables injectées en premier) ───────
        context = dict(execution.input_variables or {})
        context.update(execution.context or {})
        context['instance_id'] = str(execution.id)

        overall_success      = True
        handled_in_parallel: set  = set()

        for step, node_id in enumerate(ordered_ids, start=1):

            # Vérification CANCELLED entre chaque nœud
            if _is_cancelled(execution_id):
                logger.info(f'[Orchestrator] Exécution #{execution_id} annulée en cours de route.')
                return

            node = node_map.get(node_id)
            if node is None:
                continue

            node_exec = node_exec_map.get(node_id)

            if node_id in handled_in_parallel:
                continue

            if node_exec and node_exec.status in ('DONE', 'SUCCESS', 'SKIPPED'):
                continue

            # ── Marquer RUNNING ───────────────────────────────────────────
            now = datetime.now(timezone.utc)
            node.status     = 'RUNNING'
            node.started_at = now

            if node_exec:
                node_exec.status     = 'RUNNING'
                node_exec.started_at = now
                node_exec.save(update_fields=['status', 'started_at'])

            _log_event(execution, 'NODE_STARTED', {
                'node': node.label, 'type': node.node_type, 'step': step,
            })

            # ── Exécution du nœud ─────────────────────────────────────────
            executor_cls = get_executor(node.node_type)
            executor     = executor_cls(node, context)

            try:
                outputs = executor.run()
                node.status        = 'DONE'
                node.outputs       = outputs or {}
                node.error_message = ''
                context.update(node.outputs)

                # ── Parallélisme (Fork) ───────────────────────────────────
                if node.node_type == 'parallel_fork':
                    suspended_info: list = []
                    _execute_parallel_branches(
                        fork_node_id=node_id,
                        edges=edges,
                        node_map=node_map,
                        node_exec_map=node_exec_map,
                        context=context,
                        execution=execution,
                        handled_in_parallel=handled_in_parallel,
                        suspended_info=suspended_info,
                    )

                    # [FIX] Si une branche s'est suspendue → pause le workflow
                    if suspended_info:
                        first = suspended_info[0]
                        execution.resume_from_node_id = first['node_id']
                        execution.status = 'PAUSED'
                        execution.save(update_fields=['status', 'resume_from_node_id'])

                        _log_event(execution, 'EXECUTION_PAUSED', {
                            'reason': 'Branche parallèle suspendue',
                            'suspended_nodes': [s['node_id'] for s in suspended_info],
                        })
                        logger.info(
                            f'[Orchestrator] Workflow #{workflow.id} pausé '
                            f'(branche parallèle suspendue sur "{first["node"]}")'
                        )
                        return

                # ── Branchement conditionnel (Condition XOR) ──────────────
                elif node.node_type == 'logic.Condition':
                    is_true = outputs.get('condition_true', False)
                    active_handle   = 'true'  if is_true else 'false'
                    inactive_handle = 'false' if is_true else 'true'
                    logger.info(
                        f'[Orchestrator] Condition "{node.label}" → {is_true} '
                        f'(handle actif: "{active_handle}")'
                    )

                    active_targets = [
                        e.target for e in edges
                        if e.source == node_id
                        and (not e.sourceHandle or e.sourceHandle == active_handle)
                    ]
                    protected = set()
                    for t in active_targets:
                        protected |= _reachable_from(t, edges)

                    for edge in edges:
                        if edge.source == node_id and edge.sourceHandle == inactive_handle:
                            _skip_downstream(
                                edge.target, edges, node_map,
                                node_exec_map, ordered_ids,
                                protected=protected,
                            )

                # ── Switch multi-branches ─────────────────────────────────
                elif node.node_type == 'switch':
                    matched_handle = outputs.get('switch_matched_handle', 'default')
                    logger.info(
                        f'[Orchestrator] Switch "{node.label}" → handle "{matched_handle}"'
                    )

                    active_targets = [
                        e.target for e in edges
                        if e.source == node_id
                        and (not e.sourceHandle or e.sourceHandle == matched_handle)
                    ]
                    protected = set()
                    for t in active_targets:
                        protected |= _reachable_from(t, edges)

                    for edge in edges:
                        if (
                            edge.source == node_id
                            and edge.sourceHandle
                            and edge.sourceHandle != matched_handle
                        ):
                            _skip_downstream(
                                edge.target, edges, node_map,
                                node_exec_map, ordered_ids,
                                protected=protected,
                            )

            except SuspendExecution as exc:
                # Gate / Form / ManualTask → suspension du workflow
                logger.info(f'[Orchestrator] Nœud "{node.label}" suspendu : {exc}')
                node.status        = 'WAITING'
                node.error_message = str(exc)

                if node_exec:
                    node_exec.status        = 'WAITING'
                    node_exec.error_message = str(exc)
                    node_exec.save(update_fields=['status', 'error_message'])

                # [FIX] Stocker le node_id de reprise dans l'exécution
                execution.resume_from_node_id = node_id
                execution.status = 'PAUSED'
                execution.save(update_fields=['status', 'resume_from_node_id'])

                _log_event(execution, 'NODE_SUSPENDED', {
                    'node':   node.label,
                    'reason': str(exc),
                })
                logger.info(
                    f'[Orchestrator] Workflow #{workflow.id} pausé sur "{node.label}"'
                )
                return   # Le thread s'arrête ; reprise via approve/form API

            except Exception as exc:
                logger.exception(f'[Orchestrator] Nœud "{node.label}" échoué : {exc}')
                node.status        = 'FAILED'
                node.error_message = str(exc)
                overall_success    = False

            # ── Persistance NodeExecution ─────────────────────────────────
            finished = datetime.now(timezone.utc) if node.status in ('DONE', 'FAILED') else None
            node.finished_at = finished

            if node_exec:
                node_exec.status        = node.status
                node_exec.outputs       = node.outputs
                node_exec.error_message = node.error_message
                node_exec.finished_at   = finished
                node_exec.save(update_fields=['status', 'outputs', 'error_message', 'finished_at'])

            _log_event(execution, f'NODE_{node.status}', {
                'node':    node.label,
                'type':    node.node_type,
                'step':    step,
                'outputs': node.outputs,
            })

            if node.status == 'FAILED':
                _skip_downstream(node_id, edges, node_map, node_exec_map, ordered_ids)
                break

        # ── Finalisation ──────────────────────────────────────────────────
        final_status = 'SUCCESS' if overall_success else 'FAILED'

        execution.status              = final_status
        execution.finished_at         = datetime.now(timezone.utc)
        execution.resume_from_node_id = ''
        if not overall_success:
            # Consolider le message d'erreur global
            failed_nodes = [
                n for n in nodes if n.status == 'FAILED'
            ]
            execution.error_message = ' | '.join(
                f'{n.label}: {n.error_message}' for n in failed_nodes
            )
        execution.save(update_fields=[
            'status', 'finished_at', 'resume_from_node_id', 'error_message'
        ])

        _log_event(execution, 'EXECUTION_COMPLETE', {'status': final_status})
        logger.info(
            f'[Orchestrator] Workflow #{workflow.id} — '
            f'Exécution #{execution.id} → {final_status}'
        )

    except Exception as e:
        logger.exception(f'[Orchestrator] Erreur fatale pour exécution #{execution_id}: {e}')
        try:
            from .models import Execution
            Execution.objects.filter(pk=execution_id).update(
                status='FAILED',
                error_message=str(e),
                finished_at=datetime.now(timezone.utc),
            )
        except Exception:
            pass
    finally:
        _close_db()


# ═════════════════════════════════════════════════════════════════════════════
# Tâche Celery — unité de travail envoyée au worker Redis
# ═════════════════════════════════════════════════════════════════════════════

from celery import shared_task

@shared_task(
    bind=True,
    name='workflows.run_workflow',
    max_retries=0,          # pas de retry automatique — la logique métier gère les échecs
    ignore_result=True,     # on ne lit pas le résultat Celery (DB = source de vérité)
    acks_late=True,         # acquitter après exécution (fiabilité si worker crashe)
)
def run_workflow_task(self, execution_id: int):
    """
    Tâche Celery — exécute un workflow complet.
    Appelée par launch_execution_async() et resume_after_approval().
    """
    logger.info(f'[Celery] Tâche run_workflow_task démarrée — execution_id={execution_id}  task_id={self.request.id}')
    _run_workflow(execution_id)
    logger.info(f'[Celery] Tâche run_workflow_task terminée — execution_id={execution_id}')


# ═════════════════════════════════════════════════════════════════════════════
# API publique — appelée depuis views.py
# ═════════════════════════════════════════════════════════════════════════════

def launch_execution_async(execution_id: int):
    """Envoie l'exécution du workflow à Celery (Redis broker)."""
    task = run_workflow_task.delay(execution_id)
    logger.info(f'[Orchestrator] Exécution #{execution_id} envoyée à Celery — task_id={task.id}')
    return task


def resume_after_approval(execution_id: int):
    """
    Reprend un workflow PAUSED après approbation ou soumission de formulaire.
    L'orchestrateur saute les nœuds déjà à DONE/SKIPPED.
    """
    logger.info(f'[Orchestrator] Reprise de l\'exécution #{execution_id} via Celery')
    launch_execution_async(execution_id)


def cancel_execution(execution_id: int) -> bool:
    """
    Annule une exécution en cours ou en pause.
    Le worker Celery détectera CANCELLED à sa prochaine vérification entre nœuds.
    Retourne True si l'annulation a été enregistrée.
    """
    from .models import Execution
    from datetime import datetime, timezone

    updated = Execution.objects.filter(
        pk=execution_id,
        status__in=['RUNNING', 'PAUSED', 'PENDING'],
    ).update(
        status='CANCELLED',
        finished_at=datetime.now(timezone.utc),
    )
    if updated:
        logger.info(f'[Orchestrator] Exécution #{execution_id} annulée.')
    return bool(updated)


def validate_workflow(workflow_id: int) -> dict:
    """Valide le graphe du workflow sans l'exécuter."""
    from .models import Workflow
    try:
        workflow = Workflow.objects.get(pk=workflow_id)
        nodes, edges = _canvas_to_proxies(
            workflow.canvas_nodes or [],
            workflow.canvas_edges or [],
        )
        return _validate_graph(nodes, edges)
    except Workflow.DoesNotExist:
        return {
            'valid':      False,
            'warnings':   ['Workflow introuvable'],
            'node_count': 0,
            'edge_count': 0,
        }
