"""
Parallel Join — Exécuteur (nœud de convergence).

Équivalent : Camunda Parallel Gateway (AND-join) | n8n Merge Node

Marque la fin d'une zone d'exécution parallèle.
L'orchestrateur s'assure que TOUTES les branches ont terminé
avant de laisser ce nœud s'exécuter.

Ce nœud fusionne les sorties de toutes les branches dans le contexte.

Config du nœud :
    (aucune config requise — la topologie des arêtes définit la convergence)
"""
import logging

from .base import BaseExecutor

logger = logging.getLogger(__name__)


class ParallelJoinExecutor(BaseExecutor):
    """
    parallel_join — Convergence de toutes les branches parallèles.
    À ce point, toutes les branches issues du Fork sont terminées.
    Ce nœud retourne les métadonnées de fusion.
    """

    def run(self) -> dict:
        label = self.node.label or 'Join Parallèle'
        logger.info(f'[ParallelJoin] "{label}" — toutes les branches convergées')
        return {
            'parallel_join': True,
            'join_label':    label,
            'join_node_id':  self.node.node_id,
        }
