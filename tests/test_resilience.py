"""
Tests de résilience du moteur de workflow.

Lancer avec :
    python manage.py test tests.test_resilience -v 2

Prérequis :
    - Redis sur localhost:6379 (Docker)
    - Worker Celery actif : celery -A config worker -l info
"""
import json
import time
import threading
from django.test import TestCase, Client
from django.urls import reverse

from workflows.models import Workflow, Execution, NodeExecution
from workflows.orchestrator import _canvas_to_proxies, _topological_order, _validate_graph


# ─── Helpers ──────────────────────────────────────────────────────────────────

def make_simple_workflow(name='WF-Test'):
    """Crée un workflow minimal (Trigger → Délai 1s → Notification simulée)."""
    canvas_nodes = [
        {'id': 'n1', 'data': {'type': 'trigger',      'label': 'Start',  'trigger_type': 'manual'}},
        {'id': 'n2', 'data': {'type': 'util.Delay',    'label': 'Wait',   'duration': 1, 'unit': 'seconds'}},
        {'id': 'n3', 'data': {'type': 'notification',  'label': 'Notify', 'channel': 'email', 'to': ''}},
    ]
    canvas_edges = [
        {'id': 'e1', 'source': 'n1', 'target': 'n2'},
        {'id': 'e2', 'source': 'n2', 'target': 'n3'},
    ]
    return Workflow.objects.create(
        name=name,
        canvas_nodes=canvas_nodes,
        canvas_edges=canvas_edges,
    )


def make_condition_workflow(name='WF-Condition'):
    """Workflow avec branchement conditionnel."""
    canvas_nodes = [
        {'id': 'n1', 'data': {'type': 'trigger',         'label': 'Start',     'trigger_type': 'manual', 'initiated_by': 'test@test.com'}},
        {'id': 'n2', 'data': {'type': 'logic.Condition', 'label': 'Check',     'expression': "1 == 1"}},
        {'id': 'n3', 'data': {'type': 'util.Delay',      'label': 'Branch True','duration': 0.1, 'unit': 'seconds'}},
        {'id': 'n4', 'data': {'type': 'util.Delay',      'label': 'Branch False','duration': 0.1, 'unit': 'seconds'}},
    ]
    canvas_edges = [
        {'id': 'e1', 'source': 'n1', 'target': 'n2'},
        {'id': 'e2', 'source': 'n2', 'target': 'n3', 'sourceHandle': 'true'},
        {'id': 'e3', 'source': 'n2', 'target': 'n4', 'sourceHandle': 'false'},
    ]
    return Workflow.objects.create(name=name, canvas_nodes=canvas_nodes, canvas_edges=canvas_edges)


# ─── Tests Orchestrateur (sans Celery) ────────────────────────────────────────

class TestOrchestrateurCore(TestCase):
    """Tests unitaires sur la logique de graphe — pas besoin de Redis."""

    def test_tri_topologique_lineaire(self):
        nodes, edges = _canvas_to_proxies(
            [{'id': 'a', 'data': {'type': 'trigger', 'label': 'A'}},
             {'id': 'b', 'data': {'type': 'util.Delay', 'label': 'B'}},
             {'id': 'c', 'data': {'type': 'notification', 'label': 'C'}}],
            [{'id': 'e1', 'source': 'a', 'target': 'b'},
             {'id': 'e2', 'source': 'b', 'target': 'c'}]
        )
        order = _topological_order(nodes, edges)
        self.assertEqual(order, ['a', 'b', 'c'])

    def test_detection_cycle(self):
        nodes, edges = _canvas_to_proxies(
            [{'id': 'a', 'data': {'type': 'trigger', 'label': 'A'}},
             {'id': 'b', 'data': {'type': 'util.Delay', 'label': 'B'}}],
            [{'id': 'e1', 'source': 'a', 'target': 'b'},
             {'id': 'e2', 'source': 'b', 'target': 'a'}]   # cycle !
        )
        result = _validate_graph(nodes, edges)
        self.assertTrue(result['has_cycle'])
        self.assertFalse(result['valid'])

    def test_workflow_vide(self):
        nodes, edges = _canvas_to_proxies([], [])
        result = _validate_graph(nodes, edges)
        self.assertFalse(result['valid'])
        self.assertIn('aucun nœud', result['warnings'][0].lower())

    def test_noeuds_deconnectes(self):
        """Un nœud sans arête doit quand même apparaître dans l'ordre."""
        nodes, edges = _canvas_to_proxies(
            [{'id': 'a', 'data': {'type': 'trigger', 'label': 'A'}},
             {'id': 'b', 'data': {'type': 'trigger', 'label': 'B'}}],  # déconnecté
            []
        )
        order = _topological_order(nodes, edges)
        self.assertIn('a', order)
        self.assertIn('b', order)


# ─── Test Résilience : Annulation ─────────────────────────────────────────────

class TestAnnulationExecution(TestCase):
    """Vérifie qu'une exécution peut être annulée proprement."""

    def setUp(self):
        self.wf = make_simple_workflow('WF-Cancel-Test')
        self.client = Client()

    def test_annulation_execution_pending(self):
        execution = Execution.objects.create(
            workflow=self.wf,
            status='PENDING',
        )
        from workflows.orchestrator import cancel_execution
        result = cancel_execution(execution.id)
        self.assertTrue(result)
        execution.refresh_from_db()
        self.assertEqual(execution.status, 'CANCELLED')

    def test_annulation_execution_deja_terminee(self):
        execution = Execution.objects.create(
            workflow=self.wf,
            status='SUCCESS',
        )
        from workflows.orchestrator import cancel_execution
        result = cancel_execution(execution.id)
        self.assertFalse(result)   # SUCCESS ne peut pas être annulée
        execution.refresh_from_db()
        self.assertEqual(execution.status, 'SUCCESS')   # inchangé


# ─── Test Résilience : Approbation multi-approbateurs ─────────────────────────

class TestApprovalMulti(TestCase):

    def setUp(self):
        self.wf = make_simple_workflow()
        self.execution = Execution.objects.create(workflow=self.wf, status='PAUSED')
        self.ne = NodeExecution.objects.create(
            execution=self.execution,
            node_id='gate1', node_type='human_task',
            label='Gate GO/NO-GO', status='WAITING',
        )

    def test_approbation_type_any(self):
        from workflows.models import Approval
        Approval.objects.create(
            node_execution=self.ne,
            decision='APPROVED',
            approver_email='alice@test.com',
            approval_type='ANY',
            required_count=1,
        )
        self.assertTrue(Approval.is_gate_passed(self.ne))

    def test_approbation_type_all_insuffisant(self):
        from workflows.models import Approval
        Approval.objects.create(
            node_execution=self.ne,
            decision='APPROVED',
            approver_email='alice@test.com',
            approval_type='ALL',
            required_count=2,
        )
        self.assertFalse(Approval.is_gate_passed(self.ne))

    def test_approbation_type_all_complet(self):
        from workflows.models import Approval
        for email in ['alice@test.com', 'bob@test.com']:
            Approval.objects.create(
                node_execution=self.ne,
                decision='APPROVED',
                approver_email=email,
                approval_type='ALL',
                required_count=2,
            )
        self.assertTrue(Approval.is_gate_passed(self.ne))

    def test_rejet_bloque_immediatement(self):
        from workflows.models import Approval
        Approval.objects.create(
            node_execution=self.ne,
            decision='APPROVED',
            approver_email='alice@test.com',
            approval_type='ALL',
            required_count=3,
        )
        Approval.objects.create(
            node_execution=self.ne,
            decision='REJECTED',
            approver_email='bob@test.com',
            approval_type='ALL',
            required_count=3,
        )
        self.assertFalse(Approval.is_gate_passed(self.ne))


# ─── Test Résilience : Concurrence (créations simultanées) ────────────────────

class TestConcurrenceCreation(TestCase):
    """Vérifie que 10 créations d'Execution simultanées ne causent pas d'erreur."""

    def setUp(self):
        self.wf = make_simple_workflow('WF-Concurrence')

    def test_10_executions_simultanees(self):
        errors  = []
        created = []

        def create_execution():
            try:
                ex = Execution.objects.create(workflow=self.wf, status='PENDING')
                created.append(ex.id)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=create_execution) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f'Erreurs de concurrence : {errors}')
        self.assertEqual(len(created), 10)


# ─── Test Résilience : Recovery zombie ────────────────────────────────────────

class TestZombieRecovery(TestCase):
    """Simule un crash serveur — les exécutions RUNNING doivent être marquées FAILED."""

    def setUp(self):
        self.wf = make_simple_workflow('WF-Zombie')

    def test_zombie_marque_failed_au_demarrage(self):
        zombie = Execution.objects.create(workflow=self.wf, status='RUNNING')

        # Simuler le ready() de apps.py
        from django.utils import timezone
        count = Execution.objects.filter(status__in=['RUNNING', 'PENDING']).update(
            status='FAILED',
            finished_at=timezone.now(),
            error_message='Interrompue par redémarrage serveur.',
        )
        self.assertGreaterEqual(count, 1)
        zombie.refresh_from_db()
        self.assertEqual(zombie.status, 'FAILED')


# ─── Test API (endpoint resilience) ───────────────────────────────────────────

class TestAPIResilience(TestCase):

    def setUp(self):
        self.client = Client()
        self.wf = make_simple_workflow('WF-API')

    def test_validate_workflow_valide(self):
        resp = self.client.get(f'/api/v2/workflows/{self.wf.id}/validate/')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['valid'])
        self.assertEqual(data['node_count'], 3)

    def test_status_sans_execution(self):
        resp = self.client.get(f'/api/v2/workflows/{self.wf.id}/status/')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIsNone(data['execution_id'])

    def test_approve_noeud_non_en_attente(self):
        """Approuver un nœud qui n'est pas WAITING doit retourner 409."""
        execution = Execution.objects.create(workflow=self.wf, status='RUNNING')
        ne = NodeExecution.objects.create(
            execution=execution, node_id='n1',
            node_type='human_task', label='Gate',
            status='DONE',  # pas WAITING
        )
        resp = self.client.post(
            f'/api/v2/workflows/executions/{execution.id}/approve/n1/',
            data=json.dumps({'decision': 'APPROVED', 'approver_email': 'test@test.com'}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 409)

    def test_cancel_execution_running(self):
        execution = Execution.objects.create(workflow=self.wf, status='RUNNING')
        resp = self.client.delete(f'/api/v2/workflows/executions/{execution.id}/cancel/')
        self.assertEqual(resp.status_code, 200)
        execution.refresh_from_db()
        self.assertEqual(execution.status, 'CANCELLED')

    def test_cancel_execution_deja_terminee(self):
        execution = Execution.objects.create(workflow=self.wf, status='SUCCESS')
        resp = self.client.delete(f'/api/v2/workflows/executions/{execution.id}/cancel/')
        self.assertEqual(resp.status_code, 409)   # conflict

    def test_workflow_archive_non_lançable(self):
        self.wf.status = 'ARCHIVED'
        self.wf.save()
        resp = self.client.post(
            f'/api/v2/workflows/{self.wf.id}/launch/',
            data='{}', content_type='application/json',
        )
        self.assertEqual(resp.status_code, 400)
