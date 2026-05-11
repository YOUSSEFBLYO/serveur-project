from django.db import models


# ══════════════════════════════════════════════════════════════════════════════
# Workflow Definition
# ══════════════════════════════════════════════════════════════════════════════

class Workflow(models.Model):
    """
    Définition d'un workflow — JAMAIS le statut d'exécution ici.
    Les statuts RUNNING / SUCCESS / FAILED appartiennent à Execution.
    """

    # Statut de la définition uniquement (pas de l'exécution)
    DEFINITION_STATUS_CHOICES = [
        ('DRAFT',     'Brouillon'),
        ('PUBLISHED', 'Publié'),
        ('ARCHIVED',  'Archivé'),
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

    PRIORITY_CHOICES = [
        ('LOW',      'Faible'),
        ('MEDIUM',   'Moyen'),
        ('HIGH',     'Élevé'),
        ('CRITICAL', 'Critique'),
    ]

    name          = models.CharField(max_length=255)
    description   = models.TextField(blank=True, default='')
    workflow_type = models.CharField(max_length=100, choices=TYPE_CHOICES, default='Mises en production')
    priority      = models.CharField(max_length=20, choices=PRIORITY_CHOICES, default='MEDIUM')
    creator_name  = models.CharField(max_length=255, default='Admin')

    # Statut de la définition — PAS de l'exécution
    status         = models.CharField(max_length=20, choices=DEFINITION_STATUS_CHOICES, default='DRAFT')

    # Versioning : chaque sauvegarde majeure incrémente version
    version        = models.PositiveIntegerField(default=1)

    is_template    = models.BooleanField(default=False)
    created_at     = models.DateTimeField(auto_now_add=True)
    updated_at     = models.DateTimeField(auto_now=True)

    # Source de vérité unique — état brut React Flow
    canvas_nodes   = models.JSONField(default=list)
    canvas_edges   = models.JSONField(default=list)

    class Meta:
        ordering = ['-created_at']
        verbose_name        = 'Workflow'
        verbose_name_plural = 'Workflows'

    def __str__(self):
        return f'{self.name} v{self.version} [{self.status}]'


# ══════════════════════════════════════════════════════════════════════════════
# Execution — une instance d'exécution d'un workflow
# ══════════════════════════════════════════════════════════════════════════════

class Execution(models.Model):
    """Une exécution = un lancement d'un Workflow. Conserve l'historique complet."""

    STATUS_CHOICES = [
        ('PENDING',   'En attente'),
        ('RUNNING',   'En cours'),
        ('SUCCESS',   'Terminé avec succès'),
        ('FAILED',    'Échoué'),
        ('PAUSED',    'En pause — tâche humaine en attente'),
        ('CANCELLED', 'Annulé manuellement'),
    ]

    workflow     = models.ForeignKey(Workflow, on_delete=models.CASCADE, related_name='executions')
    status       = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    started_at   = models.DateTimeField(auto_now_add=True)
    finished_at  = models.DateTimeField(null=True, blank=True)
    triggered_by = models.CharField(max_length=255, default='canvas')

    # Variables d'entrée passées au lancement (distinctes du contexte runtime)
    input_variables = models.JSONField(default=dict)

    # Contexte runtime cumulatif — partagé entre tous les nœuds
    context         = models.JSONField(default=dict)

    # Message d'erreur global quand le workflow échoue
    error_message   = models.TextField(blank=True, default='')

    # Node_id du dernier nœud suspendu — permet une reprise précise
    resume_from_node_id = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        ordering = ['-started_at']
        verbose_name        = 'Exécution'
        verbose_name_plural = 'Exécutions'

    def __str__(self):
        return f'Exécution #{self.pk} de "{self.workflow.name}" [{self.status}]'


# ══════════════════════════════════════════════════════════════════════════════
# NodeExecution — état par nœud pour une exécution donnée
# ══════════════════════════════════════════════════════════════════════════════

class NodeExecution(models.Model):
    """État d'un nœud pour une exécution — lu par le polling /status/."""

    STATUS_CHOICES = [
        ('PENDING',  'En attente'),
        ('RUNNING',  'En cours'),
        ('DONE',     'Terminé'),
        ('FAILED',   'Échoué'),
        ('SKIPPED',  'Ignoré'),
        ('WAITING',  'En attente d\'action humaine'),
        ('RETRYING', 'Nouvelle tentative en cours'),
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

    # Retry — suivi des tentatives automatiques
    retry_count   = models.PositiveIntegerField(default=0)
    max_retries   = models.PositiveIntegerField(default=0)

    # SLA — deadline au-delà de laquelle le nœud est marqué FAILED
    timeout_at    = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['step_number', 'id']

    def __str__(self):
        return f'{self.label} [{self.status}] — Exécution #{self.execution_id}'


# ══════════════════════════════════════════════════════════════════════════════
# Approval — décision GO / NO-GO (multi-approbateurs supporté)
# ══════════════════════════════════════════════════════════════════════════════

class Approval(models.Model):
    """
    Décision d'un approbateur sur un nœud human_task.

    ForeignKey (pas OneToOne) → supporte plusieurs approbateurs par nœud.
    Logique :
      - approval_type=ANY  : le premier APPROVED suffit
      - approval_type=ALL  : required_count approbateurs doivent approuver
    """

    DECISION_CHOICES = [
        ('APPROVED', 'Approuvé'),
        ('REJECTED', 'Rejeté'),
    ]

    APPROVAL_TYPE_CHOICES = [
        ('ANY', 'Au moins un approbateur'),
        ('ALL', 'Tous les approbateurs requis'),
    ]

    node_execution = models.ForeignKey(
        NodeExecution, on_delete=models.CASCADE, related_name='approvals'
    )
    decision       = models.CharField(max_length=20, choices=DECISION_CHOICES)
    approver_email = models.EmailField(blank=True, default='')
    comment        = models.TextField(blank=True, default='')
    decided_at     = models.DateTimeField(auto_now_add=True)

    # Paramètres de la règle d'approbation (hérités du nœud)
    required_count = models.PositiveIntegerField(default=1)
    approval_type  = models.CharField(
        max_length=10, choices=APPROVAL_TYPE_CHOICES, default='ANY'
    )

    class Meta:
        ordering = ['decided_at']

    def __str__(self):
        return f'{self.decision} par {self.approver_email}'

    @classmethod
    def is_gate_passed(cls, node_execution: 'NodeExecution') -> bool:
        """
        Vérifie si la règle d'approbation est satisfaite pour ce nœud.
        Retourne True si le workflow peut reprendre.
        """
        approvals = cls.objects.filter(node_execution=node_execution)

        # Un rejet bloque immédiatement, quel que soit le type
        if approvals.filter(decision='REJECTED').exists():
            return False

        approved_count = approvals.filter(decision='APPROVED').count()
        if not approved_count:
            return False

        first = approvals.first()
        if first.approval_type == 'ANY':
            return True

        # ALL : il faut au moins required_count approbations
        return approved_count >= first.required_count


# ══════════════════════════════════════════════════════════════════════════════
# FormSubmission — données de formulaire persistées
# ══════════════════════════════════════════════════════════════════════════════

class FormSubmission(models.Model):
    """
    Données soumises via un nœud human_task en mode 'form'.
    Persistées séparément du contexte pour traçabilité et audit.
    """

    node_execution = models.OneToOneField(
        NodeExecution, on_delete=models.CASCADE, related_name='form_submission'
    )
    submitted_by   = models.EmailField(blank=True, default='')
    form_data      = models.JSONField(default=dict)
    submitted_at   = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'Formulaire — {self.node_execution.label} soumis par {self.submitted_by}'


# ══════════════════════════════════════════════════════════════════════════════
# AuditLog — journal immuable d'événements
# ══════════════════════════════════════════════════════════════════════════════

class AuditLog(models.Model):
    """Journal immuable — un enregistrement par événement d'exécution."""

    execution  = models.ForeignKey(Execution, on_delete=models.CASCADE, related_name='audit_logs')
    event_type = models.CharField(max_length=100)   # NODE_STARTED | NODE_DONE | WF_FAILED …
    actor      = models.CharField(max_length=255, default='system')
    data       = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        verbose_name        = 'Journal d\'audit'
        verbose_name_plural = 'Journaux d\'audit'

    def __str__(self):
        return f'[{self.event_type}] {self.created_at:%Y-%m-%d %H:%M:%S}'
