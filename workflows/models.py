from django.db import models


# ══════════════════════════════════════════════════════════════════════════════
# Workflow Definition
# ══════════════════════════════════════════════════════════════════════════════

class Workflow(models.Model):
    """Top-level workflow definition — stores the React Flow canvas state."""

    STATUS_CHOICES = [
        ('DRAFT',   'Brouillon'),
        ('RUNNING', 'En cours'),
        ('SUCCESS', 'Terminé'),
        ('FAILED',  'Échoué'),
        ('PAUSED',  'En pause'),
    ]

    TYPE_CHOICES = [
        ('Mises en production',          'Mises en production'),
        ('Onboarding applicatif',        'Onboarding applicatif'),
        ('Gestion des incidents majeurs','Gestion des incidents majeurs'),
        ('Validation sécurité',          'Validation sécurité'),
        ('Ouverture de flux',            'Ouverture de flux'),
        ('Gouvernance des changements',  'Gouvernance des changements'),
        ('Workflow métier custom',       'Workflow métier custom'),
    ]

    CATEGORY_CHOICES = [
        ('DevOps',        'DevOps'),
        ('QA',            'QA'),
        ('Développement', 'Développement'),
        ('Sécurité',      'Sécurité'),
        ('Exploitation',  'Exploitation'),
    ]

    name           = models.CharField(max_length=255)
    description    = models.TextField(blank=True, default='')
    workflow_type  = models.CharField(max_length=100, choices=TYPE_CHOICES, default='Mises en production')
    solution       = models.CharField(max_length=100, default='Kraken')
    category       = models.CharField(max_length=100, choices=CATEGORY_CHOICES, default='DevOps')
    priority       = models.CharField(max_length=20, default='Non')
    itop_reference = models.CharField(max_length=100, blank=True, default='')
    creator_name   = models.CharField(max_length=255, default='Admin')
    status         = models.CharField(max_length=20, choices=STATUS_CHOICES, default='DRAFT')
    is_template    = models.BooleanField(default=False)
    created_at     = models.DateTimeField(auto_now_add=True)
    updated_at     = models.DateTimeField(auto_now=True)

    # Raw React Flow canvas state (nodes + edges JSON)
    canvas_nodes = models.JSONField(default=list)
    canvas_edges = models.JSONField(default=list)

    class Meta:
        ordering = ['-created_at']
        verbose_name        = 'Workflow'
        verbose_name_plural = 'Workflows'

    def __str__(self):
        return f'{self.name} [{self.status}]'


# ══════════════════════════════════════════════════════════════════════════════
# Canvas Nodes & Edges (extracted from JSON for DB queries)
# ══════════════════════════════════════════════════════════════════════════════

class WorkflowNode(models.Model):
    """Individual node within a workflow canvas."""

    STATUS_CHOICES = [
        ('PENDING', 'En attente'),
        ('RUNNING', 'En cours'),
        ('DONE',    'Terminé'),
        ('FAILED',  'Échoué'),
        ('SKIPPED', 'Ignoré'),
        ('WAITING', 'En attente d\'approbation'),
    ]

    workflow      = models.ForeignKey(Workflow, on_delete=models.CASCADE, related_name='nodes')
    node_id       = models.CharField(max_length=255)   # React Flow node id
    node_type     = models.CharField(max_length=100)   # e.g. "argocd.Deploy"
    label         = models.CharField(max_length=255, blank=True, default='')
    config        = models.JSONField(default=dict)     # node properties / data
    position      = models.JSONField(default=dict)     # {x, y}
    status        = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    outputs       = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True, default='')
    started_at    = models.DateTimeField(null=True, blank=True)
    finished_at   = models.DateTimeField(null=True, blank=True)
    order         = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return f'{self.label} [{self.status}] — wf#{self.workflow_id}'


class WorkflowEdge(models.Model):
    """Edge connecting two canvas nodes."""

    workflow = models.ForeignKey(Workflow, on_delete=models.CASCADE, related_name='edges')
    edge_id  = models.CharField(max_length=255)
    source   = models.CharField(max_length=255)
    target   = models.CharField(max_length=255)
    label    = models.CharField(max_length=255, blank=True, default='')

    def __str__(self):
        return f'{self.source} → {self.target}'


# ══════════════════════════════════════════════════════════════════════════════
# Execution (one run of a workflow)
# ══════════════════════════════════════════════════════════════════════════════

class Execution(models.Model):
    """A single execution instance of a Workflow — preserves history."""

    STATUS_CHOICES = [
        ('PENDING', 'En attente'),
        ('RUNNING', 'En cours'),
        ('SUCCESS', 'Terminé'),
        ('FAILED',  'Échoué'),
        ('PAUSED',  'En pause'),
    ]

    workflow     = models.ForeignKey(Workflow, on_delete=models.CASCADE, related_name='executions')
    status       = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    started_at   = models.DateTimeField(auto_now_add=True)
    finished_at  = models.DateTimeField(null=True, blank=True)
    triggered_by = models.CharField(max_length=255, default='canvas')
    context      = models.JSONField(default=dict)   # runtime variables / params

    class Meta:
        ordering = ['-started_at']
        verbose_name        = 'Exécution'
        verbose_name_plural = 'Exécutions'

    def __str__(self):
        return f'Exécution #{self.pk} de "{self.workflow.name}" [{self.status}]'


class NodeExecution(models.Model):
    """State of a single node within an Execution run."""

    STATUS_CHOICES = [
        ('PENDING', 'En attente'),
        ('RUNNING', 'En cours'),
        ('DONE',    'Terminé'),
        ('FAILED',  'Échoué'),
        ('SKIPPED', 'Ignoré'),
        ('WAITING', 'En attente d\'approbation'),
    ]

    execution     = models.ForeignKey(Execution, on_delete=models.CASCADE, related_name='node_executions')
    node_id       = models.CharField(max_length=255)
    node_type     = models.CharField(max_length=100)
    label         = models.CharField(max_length=255, blank=True)
    status        = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    outputs       = models.JSONField(default=dict)
    error_message = models.TextField(blank=True, default='')
    started_at    = models.DateTimeField(null=True, blank=True)
    finished_at   = models.DateTimeField(null=True, blank=True)
    step_number   = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['step_number', 'id']

    def __str__(self):
        return f'{self.label} [{self.status}] in Execution #{self.execution_id}'


class Approval(models.Model):
    """GO / NO-GO decision for a Gate/Bloquant task."""

    DECISION_CHOICES = [
        ('APPROVED', 'Approuvé'),
        ('REJECTED', 'Rejeté'),
    ]

    node_execution = models.OneToOneField(NodeExecution, on_delete=models.CASCADE, related_name='approval')
    decision       = models.CharField(max_length=20, choices=DECISION_CHOICES)
    approver_email = models.EmailField(blank=True, default='')
    comment        = models.TextField(blank=True, default='')
    decided_at     = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.decision} by {self.approver_email}'


class AuditLog(models.Model):
    """Immutable event log for every execution step."""

    execution  = models.ForeignKey(Execution, on_delete=models.CASCADE, related_name='audit_logs')
    event_type = models.CharField(max_length=100)   # e.g. 'NODE_STARTED', 'NODE_DONE', 'WF_FAILED'
    actor      = models.CharField(max_length=255, default='system')
    data       = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        verbose_name        = 'Journal d\'audit'
        verbose_name_plural = 'Journaux d\'audit'

    def __str__(self):
        return f'[{self.event_type}] {self.created_at:%Y-%m-%d %H:%M:%S}'