"""
Condition / Branchement — Executor.

Évalue une expression logique sur le contexte du workflow et oriente
vers la branche « true » ou « false » selon le résultat.
Supporte les opérateurs : ==, !=, >, <, >=, <=, contains, startswith, endswith.

Exemples :
    {{environment}} == 'PROD'
    {{coverage}} >= 80
    {{branch}} contains 'hotfix'
"""
import logging
import re

from .base import BaseExecutor

logger = logging.getLogger(__name__)

_OP_PATTERN = re.compile(
    r'^\s*(.+?)\s*(==|!=|>=|<=|>|<|contains|startswith|endswith)\s*(.+?)\s*$',
    re.IGNORECASE
)
_VAR_PATTERN = re.compile(r'\{\{(\w+)\}\}')


def _resolve(expr: str, context: dict) -> str:
    """Remplace {{clé}} par la valeur du contexte."""
    def replace(m):
        key = m.group(1)
        return str(context.get(key, ''))
    return _VAR_PATTERN.sub(replace, expr)


def _coerce(value: str):
    """Tente de convertir en nombre, sinon retourne str sans guillemets."""
    stripped = value.strip().strip("'\"")
    try:
        return float(stripped) if '.' in stripped else int(stripped)
    except (ValueError, TypeError):
        return stripped


def _evaluate(condition: str, context: dict) -> bool:
    """Évalue la condition et retourne True/False."""
    resolved = _resolve(condition, context)
    m = _OP_PATTERN.match(resolved)
    if not m:
        # Évaluation booléenne simple
        val = resolved.strip().lower()
        if val in ('true', '1', 'yes', 'oui'):
            return True
        if val in ('false', '0', 'no', 'non', ''):
            return False
        raise RuntimeError(
            f"[Condition] Expression non reconnue : '{condition}'\n"
            "Format attendu : {{variable}} OPÉRATEUR valeur\n"
            "Opérateurs : ==, !=, >, <, >=, <=, contains, startswith, endswith"
        )

    left_raw, operator, right_raw = m.group(1), m.group(2).lower(), m.group(3)
    left  = _coerce(left_raw)
    right = _coerce(right_raw)

    if operator == '==':       return left == right
    if operator == '!=':       return left != right
    if operator == '>':        return float(left) > float(right)
    if operator == '<':        return float(left) < float(right)
    if operator == '>=':       return float(left) >= float(right)
    if operator == '<=':       return float(left) <= float(right)
    if operator == 'contains':    return str(right).lower() in str(left).lower()
    if operator == 'startswith':  return str(left).lower().startswith(str(right).lower())
    if operator == 'endswith':    return str(left).lower().endswith(str(right).lower())
    return False


class ConditionExecutor(BaseExecutor):
    """
    logic.Condition — Branchement conditionnel.

    Config du nœud :
        condition    : str  — Expression à évaluer (ex: {{environment}} == 'PROD')
        trueBranch   : str  — Label de la branche si VRAI (informatif)
        falseBranch  : str  — Label de la branche si FAUX (informatif)
        outputKey    : str  — Clé de sortie (défaut: condition_result)
        stopOnFalse  : bool — Arrêter le workflow si la condition est fausse
    """

    def run(self) -> dict:
        condition    = self.cfg('condition',   '').strip()
        true_branch  = self.cfg('trueBranch',  'Vrai').strip()
        false_branch = self.cfg('falseBranch', 'Faux').strip()
        output_key   = self.cfg('outputKey',   'condition_result').strip() or 'condition_result'
        stop_on_false = self.cfg('stopOnFalse', False)

        if not condition:
            raise RuntimeError(
                "[Condition] Aucune condition configurée.\n"
                "Renseignez le champ 'condition' (ex: {{environment}} == 'PROD')."
            )

        logger.info(f'[Condition] Évaluation : {condition}')
        logger.info(f'[Condition] Contexte disponible : {list(self.context.keys())}')

        result = _evaluate(condition, self.context)
        branch = true_branch if result else false_branch

        logger.info(
            f'[Condition] Résultat : {result}  → branche : "{branch}"'
        )

        if not result and stop_on_false:
            raise RuntimeError(
                f"[Condition] Condition non satisfaite : {condition}\n"
                f"La condition est évaluée à FAUX → branche '{false_branch}'.\n"
                "Le workflow est arrêté (stopOnFalse=true)."
            )

        return {
            output_key:             result,
            'condition_expression': condition,
            'condition_branch':     branch,
            'condition_true':       result,
        }
