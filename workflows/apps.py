# workflows/apps.py
from django.apps import AppConfig


class WorkflowsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "workflows"
    verbose_name = "Workflow Engine"

    def ready(self):
        import threading
        threading.Timer(2.0, self._cleanup_zombies).start()

    def _cleanup_zombies(self):
        """
        Au redémarrage du serveur Django, seules les exécutions RUNNING sont
        de vraies zombies (worker Celery crashé en plein milieu).
        Les PENDING n'ont jamais été démarrées → elles restent PENDING
        et seront reprises par Celery dès que Redis sera disponible.
        """
        try:
            from django.utils import timezone
            from .models import Execution
            import logging

            log = logging.getLogger(__name__)

            # Zombies réels : exécutions qui étaient EN COURS au moment du crash
            zombie_count = Execution.objects.filter(status='RUNNING').update(
                status='FAILED',
                finished_at=timezone.now(),
                error_message='Exécution interrompue par redémarrage du serveur (worker crashé).',
            )
            if zombie_count:
                log.warning(
                    f'[Startup] {zombie_count} exécution(s) zombie(s) RUNNING → FAILED.'
                )

            # PENDING : jamais démarrées → on les laisse en attente
            pending_count = Execution.objects.filter(status='PENDING').count()
            if pending_count:
                log.info(
                    f'[Startup] {pending_count} exécution(s) PENDING conservée(s) '
                    f'— seront reprises par Celery au redémarrage.'
                )

        except Exception:
            pass