"""
Configuration Celery — Workflow Engine Kraken.

Broker  : Redis (Docker) → redis://localhost:6379/0
Backend : Redis           → redis://localhost:6379/1

Démarrer le worker :
    celery -A config worker -l info -c 4

Monitoring (optionnel) :
    celery -A config flower
"""
import os
from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

app = Celery('workflow_engine')

# Lit tous les paramètres CELERY_* depuis settings.py
app.config_from_object('django.conf:settings', namespace='CELERY')

# Découverte automatique des tâches dans chaque app Django
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    print(f'Request: {self.request!r}')
