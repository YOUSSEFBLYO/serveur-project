"""
Formulaire (Form) — Executor.

Étape en milieu de workflow qui suspend l'exécution et demande
à l'utilisateur de remplir des champs supplémentaires avant de continuer.
Différent du Manuel Dispatch (trigger) : c'est une interruption mid-workflow.

Exemples d'usage :
  - Plan de rollback à valider avant MEP PROD
  - Notes de validation post-déploiement
  - Confirmation avec champs libres
"""
import json
import logging

from .base import BaseExecutor, SuspendExecution

logger = logging.getLogger(__name__)


class FormInputExecutor(BaseExecutor):
    """
    human.Form — Formulaire mid-workflow.

    Config du nœud :
        formTitle    : str  — Titre du formulaire affiché à l'utilisateur
        formFields   : text — JSON décrivant les champs à remplir
                              Ex: [{"name":"rollbackPlan","label":"Plan de rollback",
                                    "type":"text","required":true}]
        instructions : text — Instructions pour l'utilisateur
        assignee     : str  — Personne/équipe à notifier (email ou rôle)
        outputPrefix : str  — Préfixe des clés de sortie (défaut: form)
    """

    def run(self) -> dict:
        form_title    = self.cfg('formTitle',    'Formulaire à compléter').strip()
        raw_fields    = self.cfg('formFields',   '[]').strip()
        instructions  = self.cfg('instructions', '').strip()
        assignee      = self.cfg('assignee',     '').strip()
        output_prefix = self.cfg('outputPrefix', 'form').strip() or 'form'

        # Parse les champs du formulaire
        fields: list = []
        try:
            parsed = json.loads(raw_fields) if raw_fields else []
            if isinstance(parsed, list):
                fields = parsed
        except json.JSONDecodeError:
            logger.warning('[Form] formFields JSON invalide — formulaire vide utilisé')

        # Construire le message de suspension
        required_fields = [f for f in fields if f.get('required', False)]
        optional_fields = [f for f in fields if not f.get('required', False)]

        field_summary = ', '.join(
            f"« {f.get('label', f.get('name', '?'))} »"
            for f in (required_fields or fields)
        )

        summary_parts = [f'Formulaire : « {form_title} »']
        if assignee:
            summary_parts.append(f'Assigné à : {assignee}')
        if field_summary:
            summary_parts.append(f'Champs requis : {field_summary}')
        if instructions:
            summary_parts.append(f'Instructions : {instructions}')

        logger.info(
            f'[Form] Suspension pour formulaire "{form_title}" — '
            f'{len(fields)} champ(s) — assigné={assignee or "non assigné"}'
        )

        raise SuspendExecution(
            ' | '.join(summary_parts)
        )
