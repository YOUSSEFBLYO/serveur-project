from django.urls import path
from . import views

urlpatterns = [
    # ── Workflow CRUD ────────────────────────────────────────────────────────
    path('',            views.workflow_list,   name='workflow-list'),
    path('<int:pk>/',   views.workflow_detail, name='workflow-detail'),

    # ── Execution ────────────────────────────────────────────────────────────
    path('<int:pk>/launch/',   views.workflow_launch,   name='workflow-launch'),
    path('<int:pk>/status/',   views.workflow_status,   name='workflow-status'),
    path('<int:pk>/validate/', views.workflow_validate, name='workflow-validate'),

    # ── Gate Task Approval ───────────────────────────────────────────────────
    path('executions/<int:execution_id>/approve/<str:node_id>/', views.approve_node,         name='approve-node'),
    path('executions/<int:execution_id>/logs/',                  views.execution_audit_log,  name='execution-logs'),
]
