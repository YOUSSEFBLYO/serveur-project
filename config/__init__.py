# Expose l'app Celery pour que Django la charge au démarrage.
from .celery import app as celery_app

__all__ = ('celery_app',)
