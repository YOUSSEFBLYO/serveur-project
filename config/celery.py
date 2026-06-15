
import os
from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

app = Celery('workflow_engine')

# Lit tous les paramètres CELERY_* depuis settings.py
app.config_from_object('django.conf:settings', namespace='CELERY')

# Découverte automatique des tâches dans chaque app Django
app.autodiscover_tasks()

# ══════════════════════════════════════════════════════════════════════════════
# CELERY BEAT SCHEDULE
# Exécute la tâche de vérification des crons toutes les minutes (60s)
# ══════════════════════════════════════════════════════════════════════════════
app.conf.beat_schedule = {
    'check-and-trigger-crons-every-minute': {
        'task': 'workflows.check_and_trigger_crons',
        'schedule': 60.0,
    },
}



