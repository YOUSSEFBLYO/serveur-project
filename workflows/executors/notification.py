"""
Notification — Exécuteur unifié (nœud 7/7).

Fusionne :
  Email (Resend) + Teams (Webhook Adaptive Card)

Envoie un message avec des variables dynamiques {{ctx_key}} résolues.
Equivalent n8n : Send Email Node + Teams Node — Equivalent Camunda : Send Task.

Config du nœud :
    channel    : enum — 'email' | 'teams' | 'both'  (défaut email)
    recipient  : str  — adresse email destinataire
    subject    : str  — sujet (email)
    message    : text — corps du message, supporte {{ctx_key}}
    webhookUrl : str  — URL du webhook Teams (channel teams/both)

Variables d'environnement utilisées :
    RESEND_API_KEY   — Clé API Resend (email)
    EMAIL_FROM       — Expéditeur Resend
"""
import json
import logging
import urllib.request
import urllib.error

from decouple import config as env_config

from .base import BaseExecutor

logger = logging.getLogger(__name__)


def _resolve_vars(text: str, context: dict) -> str:
    """Remplace {{key}} par la valeur du contexte."""
    if not text:
        return text
    for key, value in context.items():
        text = text.replace(f'{{{{{key}}}}}', str(value))
    return text


class NotificationExecutor(BaseExecutor):
    """
    notification — Envoie un message (email, Teams ou les deux).
    Les variables {{ctx_key}} dans subject et message sont résolues automatiquement.
    """

    def run(self) -> dict:
        channel = str(self.cfg('channel', 'email')).lower().strip()

        # Conditions d'envoi (nouveaux champs)
        on_success_only = self.cfg('on_success_only', False)
        on_failure_only = self.cfg('on_failure_only', False)
        wf_status = self.context.get('workflow_status', '')
        if on_success_only and wf_status == 'FAILED':
            logger.info('[Notification] on_success_only=true et statut FAILED — ignoré')
            return {'channel': channel, 'skipped': True, 'reason': 'on_success_only'}
        if on_failure_only and wf_status != 'FAILED':
            logger.info('[Notification] on_failure_only=true et statut non FAILED — ignoré')
            return {'channel': channel, 'skipped': True, 'reason': 'on_failure_only'}

        results: dict = {'channel': channel}

        if channel == 'email':
            # Nouveau nom: body — ancien: message (rétrocompat)
            subject = _resolve_vars(self.cfg('subject', 'Notification Workflow').strip(), self.context)
            body    = _resolve_vars((self.cfg('body', '') or self.cfg('message', 'Exécution terminée.')).strip(), self.context)
            results['email'] = self._send_email(subject, body)

        elif channel == 'teams':
            title   = _resolve_vars(self.cfg('title', 'Notification Workflow').strip(), self.context)
            message = _resolve_vars(self.cfg('message', 'Exécution terminée.').strip(), self.context)
            color   = self.cfg('color', 'green').strip()
            results['teams'] = self._send_teams(title, message, color)

        elif channel == 'slack':
            message = _resolve_vars(self.cfg('message', 'Exécution terminée.').strip(), self.context)
            results['slack'] = self._send_slack(message)

        return results

    # ─────────────────────────────────────────────────────────────────────────
    # Email via Resend API
    # ─────────────────────────────────────────────────────────────────────────

    def _send_email(self, subject: str, message: str) -> dict:
        # Nouveau nom: to — ancien: recipient (rétrocompat)
        recipient     = (
            self.cfg('to', '').strip()
            or self.cfg('recipient', '').strip()
            or self.ctx('recipient', '').strip()
            or env_config('DEFAULT_RECIPIENT', default='').strip()
        )
        resend_api_key = env_config('RESEND_API_KEY', default='').strip()
        from_addr      = env_config(
            'EMAIL_FROM', default='Workflow Engine <onboarding@resend.dev>'
        ).strip()

        if not recipient:
            logger.warning('[Notification/Email] Aucun destinataire — email ignoré')
            return {'sent': False, 'reason': 'no_recipient'}

        if not resend_api_key:
            logger.warning('[Notification/Email] RESEND_API_KEY manquante → simulation')
            return {
                'sent':      False,
                'mode':      'simulation',
                'recipient': recipient,
                'subject':   subject,
                'message':   'Configurez RESEND_API_KEY dans .env pour envoyer réellement.',
            }

        html_body = f"""
        <div style="font-family:Arial,sans-serif;max-width:600px;margin:auto;
                    background:#fff;border-radius:8px;overflow:hidden;
                    box-shadow:0 2px 12px rgba(0,0,0,.1);">
          <div style="background:linear-gradient(135deg,#1e1b4b,#312e81);
                      padding:24px;text-align:center;">
            <h1 style="color:#fff;margin:0;font-size:20px;">⚙️ Workflow Engine</h1>
          </div>
          <div style="padding:24px;">
            <h2 style="color:#1e293b;font-size:16px;margin-top:0;">{subject}</h2>
            <p style="color:#475569;line-height:1.6;white-space:pre-wrap;">{message}</p>
          </div>
          <div style="padding:12px 24px;background:#f8fafc;text-align:center;">
            <p style="color:#94a3b8;font-size:11px;margin:0;">
              Envoyé par <strong>Workflow Engine</strong>
            </p>
          </div>
        </div>"""

        payload = {
            'from':    from_addr,
            'to':      [recipient],
            'subject': subject,
            'html':    html_body,
            'text':    message,
        }

        try:
            data = json.dumps(payload).encode('utf-8')
            req  = urllib.request.Request(
                'https://api.resend.com/emails',
                data=data, method='POST',
            )
            req.add_header('Authorization', f'Bearer {resend_api_key}')
            req.add_header('Content-Type', 'application/json')

            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode('utf-8'))
                logger.info(f'[Notification/Email] Envoyé à {recipient} (ID: {result.get("id")})')
                return {'sent': True, 'recipient': recipient, 'resend_id': result.get('id')}

        except urllib.error.HTTPError as exc:
            err = exc.read().decode('utf-8')[:300]
            logger.error(f'[Notification/Email] Erreur HTTP {exc.code}: {err}')
            return {'sent': False, 'error': f'HTTP {exc.code}: {err}'}
        except Exception as exc:
            logger.error(f'[Notification/Email] Erreur: {exc}')
            return {'sent': False, 'error': str(exc)[:300]}

    # ─────────────────────────────────────────────────────────────────────────
    # Teams via Webhook Adaptive Card
    # ─────────────────────────────────────────────────────────────────────────

    def _send_teams(self, title: str, message: str, color: str = 'green') -> dict:
        # Nouveau nom: webhook_url — ancien: webhookUrl (rétrocompat)
        webhook_url = (
            self.cfg('webhook_url', '').strip()
            or self.cfg('webhookUrl', '').strip()
            or env_config('TEAMS_WEBHOOK_URL', default='').strip()
        )

        if not webhook_url:
            logger.warning('[Notification/Teams] webhook_url manquante → ignoré')
            return {'sent': False, 'reason': 'no_webhook_url'}

        color_map = {'green': '00b300', 'red': 'cc0000', 'orange': 'ff8c00', 'blue': '0078d4'}
        accent = color_map.get(color, '0078d4')

        card = {
            'type':        'message',
            'attachments': [{
                'contentType': 'application/vnd.microsoft.card.adaptive',
                'content': {
                    '$schema': 'http://adaptivecards.io/schemas/adaptive-card.json',
                    'type':    'AdaptiveCard',
                    'version': '1.4',
                    'body': [
                        {'type': 'TextBlock', 'text': f'⚙️ {title}', 'weight': 'Bolder', 'size': 'Medium', 'wrap': True, 'color': accent},
                        {'type': 'TextBlock', 'text': message, 'wrap': True},
                    ],
                }
            }]
        }

        try:
            data = json.dumps(card).encode('utf-8')
            req  = urllib.request.Request(webhook_url, data=data, method='POST',
                                          headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode('utf-8')
                logger.info(f'[Notification/Teams] Envoyé (réponse: {raw[:50]})')
                return {'sent': True, 'teams_response': raw[:200]}
        except urllib.error.HTTPError as exc:
            err = exc.read().decode('utf-8')[:300]
            logger.error(f'[Notification/Teams] Erreur HTTP {exc.code}: {err}')
            return {'sent': False, 'error': f'HTTP {exc.code}: {err}'}
        except Exception as exc:
            logger.error(f'[Notification/Teams] Erreur: {exc}')
            return {'sent': False, 'error': str(exc)[:300]}

    def _send_slack(self, message: str) -> dict:
        webhook_url = (
            self.cfg('webhook_url', '').strip()
            or env_config('SLACK_WEBHOOK_URL', default='').strip()
        )

        if not webhook_url:
            logger.warning('[Notification/Slack] webhook_url manquante → ignoré')
            return {'sent': False, 'reason': 'no_webhook_url'}

        payload = {'text': message}
        try:
            data = json.dumps(payload).encode('utf-8')
            req  = urllib.request.Request(webhook_url, data=data, method='POST',
                                          headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode('utf-8')
                logger.info(f'[Notification/Slack] Envoyé (réponse: {raw[:50]})')
                return {'sent': True}
        except Exception as exc:
            logger.error(f'[Notification/Slack] Erreur: {exc}')
            return {'sent': False, 'error': str(exc)[:300]}
