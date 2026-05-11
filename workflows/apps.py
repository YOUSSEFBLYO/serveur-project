from django.apps import AppConfig


class WorkflowsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "workflows"
    verbose_name = "Workflow Engine"

    def ready(self):
        """
        Au démarrage du serveur, toute Execution restée à RUNNING ou PENDING
        depuis le démarrage précédent est marquée FAILED (zombie recovery).
        """
        try:
            from django.utils import timezone
            from .models import Execution
            zombies = Execution.objects.filter(status__in=['RUNNING', 'PENDING'])
            count   = zombies.update(
                status='FAILED',
                finished_at=timezone.now(),
                error_message='Exécution interrompue par redémarrage du serveur.',
            )
            if count:
                import logging
                logging.getLogger(__name__).warning(
                    f'[Startup] {count} exécution(s) zombie(s) marquée(s) FAILED.'
                )
        except Exception:
            pass
