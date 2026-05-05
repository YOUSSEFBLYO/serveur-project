"""
Gate Task (GO / NO-GO) — Executor.

Porte de décision qualité : évalue des conditions sur le contexte
d'exécution et décide si le workflow peut continuer (GO) ou doit
s'arrêter (NO-GO). Peut fonctionner en mode automatique (seuil) ou
en mode manuel (intervention humaine requise).
"""
import logging
from .base import BaseExecutor, SuspendExecution

logger = logging.getLogger(__name__)


def _evaluate_condition(condition: str, context: dict) -> bool:
    """
    Évalue une condition simple sur le contexte.
    Supporte : {key} op {value}  où op ∈ {==, !=, >, <, >=, <=, contains}
    """
    condition = condition.strip()

    for op in ('>=', '<=', '!=', '==', '>', '<', 'contains'):
        if f' {op} ' in condition:
            left, right = condition.split(f' {op} ', 1)
            left  = left.strip()
            right = right.strip().strip('"').strip("'")

            ctx_val = str(context.get(left, '')).strip()
            if op == 'contains':
                return right.lower() in ctx_val.lower()
            try:
                # Comparaison numérique si possible
                l_num, r_num = float(ctx_val), float(right)
                return (
                    l_num == r_num if op == '==' else
                    l_num != r_num if op == '!=' else
                    l_num >  r_num if op == '>'  else
                    l_num <  r_num if op == '<'  else
                    l_num >= r_num if op == '>=' else
                    l_num <= r_num
                )
            except ValueError:
                return (
                    ctx_val == right if op == '==' else
                    ctx_val != right if op == '!=' else False
                )
    # Pas d'opérateur trouvé → on vérifie juste si la clé est truthy
    return bool(context.get(condition))


class GateTaskExecutor(BaseExecutor):
    """
    human.GateTask — Porte de décision GO / NO-GO.

    Config du nœud :
        mode         : enum — 'AUTO' | 'MANUAL'
        condition    : str  — Condition à évaluer (ex: 'test_pass_rate >= 80')
        noGoAction   : enum — 'STOP' | 'PAUSE' | 'SKIP'
        reviewerRole : str  — En mode MANUAL : rôle ou email du validateur
        description  : text — Description de la porte de décision
    """

    def run(self) -> dict:
        mode          = self.cfg('mode', 'AUTO').strip().upper() or 'AUTO'
        condition     = self.cfg('condition', '').strip()
        no_go_action  = self.cfg('noGoAction', 'STOP').strip().upper() or 'STOP'
        reviewer_role = self.cfg('reviewerRole', '').strip()
        description   = self.cfg('description', 'Porte qualité').strip()

        logger.info(
            f'[GateTask] Évaluation de la porte — mode={mode}  '
            f'condition="{condition}"  action_si_nogo={no_go_action}'
        )

        # ── Mode MANUAL ────────────────────────────────────────────────────────
        if mode == 'MANUAL':
            reviewer = reviewer_role or 'équipe qualité'
            logger.info(f'[GateTask] MANUAL — en attente de décision par : {reviewer}')
            raise SuspendExecution(
                f"Porte GO/NO-GO en attente de décision manuelle par « {reviewer} » "
                f"— {description}"
            )

        # ── Mode AUTO ──────────────────────────────────────────────────────────
        if not condition:
            logger.warning('[GateTask] Aucune condition définie → GO par défaut')
            return {'gate_result': 'GO', 'gate_condition': '', 'gate_evaluated': True}

        result = _evaluate_condition(condition, self.context)
        gate   = 'GO' if result else 'NO-GO'

        logger.info(
            f'[GateTask] Condition "{condition}" → {gate}'
        )

        if gate == 'NO-GO':
            msg = (
                f"Porte qualité NO-GO — condition non satisfaite : {condition}\n"
                f"Contexte disponible : {dict(list(self.context.items())[:10])}"
            )
            if no_go_action == 'STOP':
                raise RuntimeError(msg)
            elif no_go_action == 'PAUSE':
                raise SuspendExecution(f"Gate NO-GO — intervention requise. {msg}")
            # SKIP : on continue avec un avertissement
            logger.warning(f'[GateTask] NO-GO ignoré (mode SKIP) — {msg}')

        return {
            'gate_result':    gate,
            'gate_condition': condition,
            'gate_evaluated': True,
            'gate_mode':      mode,
        }
