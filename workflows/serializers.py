from rest_framework import serializers
from .models import Workflow, Execution, NodeExecution, Approval, FormSubmission, AuditLog


class WorkflowListSerializer(serializers.ModelSerializer):
    """
    Sérialiseur principal — expose canvas_nodes / canvas_edges pour le builder React
    et la liste dashboard.
    Ajoute `last_execution_status` calculé à partir de la dernière Execution.
    """
    date = serializers.DateTimeField(source='created_at', format='%Y-%m-%d', read_only=True)
    last_execution_status = serializers.SerializerMethodField()
    last_execution_id     = serializers.SerializerMethodField()

    class Meta:
        model  = Workflow
        fields = [
            'id', 'name', 'description', 'workflow_type',
            'priority', 'creator_name',
            'status', 'version', 'is_template', 'date',
            'canvas_nodes', 'canvas_edges',
            'last_execution_status', 'last_execution_id',
        ]

    def get_last_execution_status(self, obj):
        last = obj.executions.order_by('-started_at').first()
        return last.status if last else None

    def get_last_execution_id(self, obj):
        last = obj.executions.order_by('-started_at').first()
        return last.id if last else None


class WorkflowCreateSerializer(serializers.ModelSerializer):
    """
    Sérialiseur de création / mise à jour depuis le builder.
    Accepte canvas_nodes et canvas_edges directement.
    """

    class Meta:
        model  = Workflow
        fields = [
            'id', 'name', 'description', 'workflow_type',
            'priority', 'creator_name',
            'status', 'is_template', 'canvas_nodes', 'canvas_edges',
        ]


class NodeExecutionSerializer(serializers.ModelSerializer):
    class Meta:
        model  = NodeExecution
        fields = [
            'node_id', 'node_type', 'label', 'status',
            'outputs', 'error_message',
            'started_at', 'finished_at', 'step_number',
            'retry_count', 'max_retries', 'timeout_at',
        ]


class ExecutionStatusSerializer(serializers.ModelSerializer):
    """Utilisé par /status/ — pollé par React toutes les 2 secondes."""
    node_executions = NodeExecutionSerializer(many=True, read_only=True)

    class Meta:
        model  = Execution
        fields = [
            'id', 'status', 'started_at', 'finished_at',
            'error_message', 'resume_from_node_id',
            'input_variables', 'node_executions',
        ]


class ApprovalSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Approval
        fields = [
            'id', 'decision', 'approver_email', 'comment',
            'decided_at', 'required_count', 'approval_type',
        ]


class FormSubmissionSerializer(serializers.ModelSerializer):
    class Meta:
        model  = FormSubmission
        fields = ['id', 'submitted_by', 'form_data', 'submitted_at']


class AuditLogSerializer(serializers.ModelSerializer):
    class Meta:
        model  = AuditLog
        fields = ['event_type', 'actor', 'data', 'created_at']
