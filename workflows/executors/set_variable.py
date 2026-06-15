"""
Set Variable — définit des variables dans le contexte runtime.

Exemple de configuration :
  variables: {"env": "PROD", "version": "{{version}}-final", "deploy": true}

Chaque variable est résolue avec {{}} puis injectée dans le contexte.
"""
import json
import logging

from .base import BaseExecutor

logger = logging.getLogger(__name__)


def _resolve_vars(text: str, context: dict) -> str:
    if not text:
        return text
    for key, value in context.items():
        text = text.replace(f'{{{{{key}}}}}', str(value))
    return text


class SetVariableExecutor(BaseExecutor):

    def run(self) -> dict:
        variables_raw = (self.cfg('variables', '{}') or '{}').strip()

        try:
            variables = json.loads(variables_raw)
        except (json.JSONDecodeError, TypeError):
            raise RuntimeError(
                f"[SetVariable] JSON invalide dans 'variables': {variables_raw}"
            )

        resolved = {}
        for key, value in variables.items():
            if isinstance(value, str):
                resolved[key] = _resolve_vars(value, self.context)
            else:
                resolved[key] = value

        logger.info(f'[SetVariable] Variables définies: {list(resolved.keys())}')
        return resolved
