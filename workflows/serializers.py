from rest_framework import serializers
from .models import Workflow, WorkflowNode, WorkflowEdge, Execution, NodeExecution, Approval, AuditLog


class WorkflowNodeSerializer(serializers.ModelSerializer):
    class Meta:
        model  = WorkflowNode
        fields = ['node_id', 'node_type', 'label', 'config', 'position', 'status', 'outputs', 'error_message', 'order']


class WorkflowEdgeSerializer(serializers.ModelSerializer):
    class Meta:
        model  = WorkflowEdge
        fields = ['edge_id', 'source', 'target', 'label']


class WorkflowListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for the dashboard list view."""

    date   = serializers.DateTimeField(source='created_at', format='%Y-%m-%d', read_only=True)
    nodes  = WorkflowNodeSerializer(many=True, read_only=True)
    edges  = WorkflowEdgeSerializer(many=True, read_only=True)

    class Meta:
        model  = Workflow
        fields = [
            'id', 'name', 'description', 'workflow_type', 'solution',
            'category', 'priority', 'itop_reference', 'creator_name',
            'status', 'is_template', 'date', 'canvas_nodes', 'canvas_edges',
            'nodes', 'edges',
        ]


class WorkflowCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating / updating a workflow from the builder."""

    nodes = WorkflowNodeSerializer(many=True, required=False)
    edges = WorkflowEdgeSerializer(many=True, required=False)

    class Meta:
        model  = Workflow
        fields = [
            'id', 'name', 'description', 'workflow_type', 'solution',
            'category', 'priority', 'itop_reference', 'creator_name',
            'is_template', 'canvas_nodes', 'canvas_edges', 'nodes', 'edges',
        ]

    def create(self, validated_data):
        nodes_data = validated_data.pop('nodes', [])
        edges_data = validated_data.pop('edges', [])

        workflow = Workflow.objects.create(**validated_data)

        for i, nd in enumerate(nodes_data):
            WorkflowNode.objects.create(workflow=workflow, order=i, **nd)
        for ed in edges_data:
            WorkflowEdge.objects.create(workflow=workflow, **ed)

        return workflow

    def update(self, instance, validated_data):
        nodes_data = validated_data.pop('nodes', None)
        edges_data = validated_data.pop('edges', None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        if nodes_data is not None:
            instance.nodes.all().delete()
            for i, nd in enumerate(nodes_data):
                WorkflowNode.objects.create(workflow=instance, order=i, **nd)

        if edges_data is not None:
            instance.edges.all().delete()
            for ed in edges_data:
                WorkflowEdge.objects.create(workflow=instance, **ed)

        return instance


class NodeExecutionSerializer(serializers.ModelSerializer):
    class Meta:
        model  = NodeExecution
        fields = ['node_id', 'node_type', 'label', 'status', 'outputs', 'error_message', 'started_at', 'finished_at', 'step_number']


class ExecutionStatusSerializer(serializers.ModelSerializer):
    """Used by the /status/ endpoint polled by React every 2 seconds."""
    node_executions = NodeExecutionSerializer(many=True, read_only=True)

    class Meta:
        model  = Execution
        fields = ['id', 'status', 'started_at', 'finished_at', 'node_executions']


class ApprovalSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Approval
        fields = ['decision', 'approver_email', 'comment', 'decided_at']


class AuditLogSerializer(serializers.ModelSerializer):
    class Meta:
        model  = AuditLog
        fields = ['event_type', 'actor', 'data', 'created_at']
