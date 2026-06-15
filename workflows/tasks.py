import logging
from celery import shared_task
from django.utils import timezone
from croniter import croniter

from .models import Workflow, Execution
from .orchestrator import launch_execution_async

logger = logging.getLogger(__name__)

@shared_task(name='workflows.check_and_trigger_crons')
def check_and_trigger_crons():
    """
    Tâche Celery Beat exécutée toutes les minutes.
    Parcourt tous les workflows et lance ceux dont le trigger cron correspond à la minute actuelle.
    """
    logger.info("[CRON] Début de la vérification des triggers cron...")
    now = timezone.now()
    
    # On arrondit à la minute près pour que croniter fonctionne parfaitement
    current_minute = now.replace(second=0, microsecond=0)

    # Filtrer les workflows qui ne sont pas des templates
    workflows = Workflow.objects.filter(is_template=False)
    triggered_count = 0

    for wf in workflows:
        if not getattr(wf, 'canvas_nodes', None):
            continue
            
        for node in wf.canvas_nodes:
            data = node.get('data', {})
            
            # Rechercher un nœud trigger configuré en 'cron'
            if data.get('type') == 'trigger' and data.get('trigger_type') == 'cron':
                cron_expr = data.get('cron_expression')
                
                if cron_expr and croniter.is_valid(cron_expr):
                    # croniter.match(expression, datetime) renvoie True si ça matche
                    if croniter.match(cron_expr, current_minute):
                        logger.info(f"[CRON] Match pour '{wf.name}' (id={wf.id}) - Expression: {cron_expr}")
                        
                        # Sécurité : éviter un double déclenchement dans la même minute
                        recent_exec = Execution.objects.filter(
                            workflow=wf,
                            triggered_by='CRON (Système)',
                            started_at__gte=current_minute
                        ).exists()
                        
                        if not recent_exec:
                            # 1. Création de l'exécution
                            execution = Execution.objects.create(
                                workflow=wf,
                                triggered_by='CRON (Système)',
                                input_variables={},
                                context={}
                            )
                            
                            # 2. Lancement asynchrone via l'orchestrateur existant
                            try:
                                launch_execution_async(execution.id)
                                triggered_count += 1
                                logger.info(f"[CRON] Exécution #{execution.id} lancée pour '{wf.name}'.")
                            except Exception as e:
                                logger.error(f"[CRON] Erreur lancement {wf.id}: {e}")
                                execution.status = 'FAILED'
                                execution.error_message = f'Broker Celery indisponible: {e}'
                                execution.save(update_fields=['status', 'error_message'])
                        else:
                            logger.info(f"[CRON] Déjà exécuté pour '{wf.name}' cette minute, on ignore.")
                break  # On suppose 1 seul nœud trigger par workflow
                
    logger.info(f"[CRON] Fin de la vérification. {triggered_count} workflow(s) déclenché(s).")
