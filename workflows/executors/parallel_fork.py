"""
Parallel Fork — Exécuteur (nœud de parallélisme).

Équivalent : Camunda Parallel Gateway (AND-split) | n8n Split in Batches

Marque le début d'une zone d'exécution parallèle.
L'orchestrateur détecte ce nœud et exécute toutes les branches
sortantes simultanément dans des threads séparés.

Il converge ensuite sur un nœud 'parallel_join'.

Config du nœud :
    (aucune config requise — la topologie des arêtes définit les branches)
"""
import logging
import time

from .base import BaseExecutor

logger = logging.getLogger(__name__)


class ParallelForkExecutor(BaseExecutor):
    """
    parallel_fork — Marqueur de début de zone parallèle.
    L'orchestrateur se charge de l'exécution réelle des branches.
    Ce nœud retourne juste les métadonnées de fork.
    """

    def run(self) -> dict:
        label = self.node.label or 'Fork Parallèle'
        logger.info(f'[ParallelFork] "{label}" — déclenchement exécution parallèle')
        time.sleep(0.3)
        return {
            'parallel_fork':  True,
            'fork_label':     label,
            'fork_node_id':   self.node.node_id,
        }
