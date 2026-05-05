"""
Teams Notification — Executor.

Envoie une notification enrichie (Adaptive Card) vers un canal Microsoft Teams
via un Incoming Webhook. Supporte les templates prédéfinis (succès, échec,
déploiement, alerte) et les messages personnalisés.
"""
import json
import logging
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

from .base import BaseExecutor

logger = logging.getLogger(__name__)


# ── Templates de cartes Adaptive Card ───────────────────────────────────────

def _build_card(template: str, context: dict, title: str,
                message: str, color: str) -> dict:
    """Construit une Adaptive Card Teams selon le template choisi."""

    # Mapping template → emoji + couleur
    template_meta = {
        'SUCCESS':    {'emoji': '✅', 'color': 'Good'},
        'FAILURE':    {'emoji': '❌', 'color': 'Attention'},
        'DEPLOYMENT': {'emoji': '🚀', 'color': 'Accent'},
        'ALERT':      {'emoji': '⚠️', 'color': 'Warning'},
        'INFO':       {'emoji': 'ℹ️', 'color': 'Default'},
    }
    meta = template_meta.get(template.upper(), template_meta['INFO'])

    # Variables dynamiques depuis le contexte
    branch      = context.get('trigger_branch', context.get('branch', '—'))
    commit_sha  = str(context.get('commit_sha', '—'))[:8]
    environment = context.get('trigger_env', '—')
    repo        = context.get('trigger_repo', context.get('argocd_url', '—'))

    facts = []
    if branch != '—':
        facts.append({'title': 'Branche', 'value': branch})
    if commit_sha != '—':
        facts.append({'title': 'Commit', 'value': commit_sha})
    if environment != '—':
        facts.append({'title': 'Environnement', 'value': environment})
    if repo and repo != '—':
        facts.append({'title': 'Dépôt/URL', 'value': repo[:80]})

    # Ajouter métriques de qualité si présentes
    if 'sonar_quality_gate' in context:
        facts.append({'title': 'Quality Gate', 'value': context['sonar_quality_gate']})
    if 'sonar_coverage' in context:
        facts.append({'title': 'Couverture', 'value': f"{context['sonar_coverage']}%"})
    if 'docker_image' in context:
        facts.append({'title': 'Image Docker', 'value': context['docker_image']})

    # Adaptive Card (format Teams)
    card = {
        'type':        'message',
        'attachments': [{
            'contentType': 'application/vnd.microsoft.card.adaptive',
            'content': {
                '$schema': 'http://adaptivecards.io/schemas/adaptive-card.json',
                'type':    'AdaptiveCard',
                'version': '1.4',
                'body': [
                    {
                        'type':  'TextBlock',
                        'text':  f'{meta["emoji"]} {title}',
                        'size':  'Large',
                        'weight':'Bolder',
                        'color': meta['color'],
                        'wrap':  True,
                    },
                    {
                        'type': 'TextBlock',
                        'text': message or 'Notification du Workflow Engine.',
                        'wrap': True,
                    },
                    *(
                        [{
                            'type':  'FactSet',
                            'facts': [{'title': f['title'], 'value': f['value']}
                                      for f in facts],
                        }] if facts else []
                    ),
                ],
                'msteams': {'width': 'Full'},
            },
        }],
    }
    return card


class TeamsNotificationExecutor(BaseExecutor):
    """
    integration.Teams — Notification Microsoft Teams via Incoming Webhook.

    Config du nœud :
        webhookUrl   : str  — URL de l'Incoming Webhook Teams (requis)
        template     : enum — 'SUCCESS' | 'FAILURE' | 'DEPLOYMENT' | 'ALERT' | 'INFO'
        title        : str  — Titre de la notification
        message      : text — Message principal (supporte les variables {{ctx_key}})
        mentionAll   : bool — Mentionner @channel (si disponible)
        color        : str  — Couleur hexadécimale de l'accent (optionnel)
    """

    def run(self) -> dict:
        webhook_url  = self.cfg('webhookUrl', '').strip()
        template     = self.cfg('template', 'INFO').strip().upper() or 'INFO'
        title        = self.cfg('title', 'Notification Workflow').strip()
        message      = self.cfg('message', '').strip()
        mention_all  = self.cfg('mentionAll', False)
        color        = self.cfg('color', '').strip()

        if not webhook_url:
            raise RuntimeError(
                "[Teams] 'webhookUrl' non configuré.\n"
                "Créez un Incoming Webhook dans Teams → Canal → Connecteurs."
            )

        # Substitution des variables de contexte dans le message
        msg_rendered = message
        for k, v in self.context.items():
            msg_rendered = msg_rendered.replace(f'{{{{{k}}}}}', str(v))

        if mention_all:
            msg_rendered = f'<at>channel</at> {msg_rendered}'

        # Titre par défaut selon le template
        if not title:
            title_map = {
                'SUCCESS':    'Workflow terminé avec succès',
                'FAILURE':    'Échec du Workflow',
                'DEPLOYMENT': 'Déploiement effectué',
                'ALERT':      'Alerte Workflow',
                'INFO':       'Information Workflow',
            }
            title = title_map.get(template, 'Notification Workflow')

        card = _build_card(template, self.context, title, msg_rendered, color)

        logger.info(
            f'[Teams] Envoi notification — template={template}  '
            f'webhook={webhook_url[:50]}...'
        )

        try:
            body = json.dumps(card).encode('utf-8')
            req  = Request(
                webhook_url, data=body, method='POST',
                headers={'Content-Type': 'application/json'},
            )
            with urlopen(req, timeout=30) as resp:
                status = resp.status
                logger.info(f'[Teams] Notification envoyée — status={status}')

        except HTTPError as exc:
            raise RuntimeError(
                f"[Teams] Erreur HTTP {exc.code} lors de l'envoi.\n"
                f"Réponse : {exc.read().decode('utf-8', errors='replace')[:300]}"
            )
        except URLError as exc:
            raise RuntimeError(
                f"[Teams] Impossible de joindre le webhook Teams.\n"
                f"URL: {webhook_url}\nDétail: {exc}"
            )

        return {
            'teams_sent':     True,
            'teams_template': template,
            'teams_title':    title,
            'teams_channel':  webhook_url.split('/')[4] if '/' in webhook_url else '?',
        }
