from django.contrib import admin
from .models import Workflow, Execution, NodeExecution, Approval, AuditLog


@admin.register(Workflow)
class WorkflowAdmin(admin.ModelAdmin):
    list_display    = ['name', 'workflow_type', 'priority', 'creator_name', 'status', 'is_template', 'created_at']
    list_filter     = ['status', 'workflow_type', 'priority', 'is_template']
    search_fields   = ['name', 'creator_name']
    readonly_fields = ['created_at', 'updated_at']


class NodeExecutionInline(admin.TabularInline):
    model       = NodeExecution
    extra       = 0
    fields      = ['node_id', 'label', 'status', 'step_number', 'started_at', 'finished_at']
    readonly_fields = ['started_at', 'finished_at']


@admin.register(Execution)
class ExecutionAdmin(admin.ModelAdmin):
    list_display    = ['id', 'workflow', 'status', 'triggered_by', 'started_at', 'finished_at']
    list_filter     = ['status']
    readonly_fields = ['started_at', 'finished_at']
    inlines         = [NodeExecutionInline]


@admin.register(NodeExecution)
class NodeExecutionAdmin(admin.ModelAdmin):
    list_display  = ['label', 'node_type', 'status', 'execution', 'step_number', 'started_at']
    list_filter   = ['status', 'node_type']
    search_fields = ['label', 'node_id']


@admin.register(Approval)
class ApprovalAdmin(admin.ModelAdmin):
    list_display = ['node_execution', 'decision', 'approver_email', 'decided_at']
    list_filter  = ['decision']


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display    = ['event_type', 'actor', 'execution', 'created_at']
    list_filter     = ['event_type']
    readonly_fields = ['created_at']
