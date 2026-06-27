"""
Notification — Email (SMTP) | Teams (webhook) | Slack (webhook).

Envoie une notification via le connecteur choisi.
Les variables {{ctx_key}} dans subject et body sont résolues depuis le contexte.

Config du nœud (canvas React Flow) :
    connector  : str  — email | teams | slack  (défaut: email)
    to         : str  — destinataire(s) e-mail (si connector=email)
    webhookUrl : str  — URL du webhook entrant (si connector=teams|slack)
    subject    : str  — sujet / titre, supporte {{ctx_key}}
    body       : str  — corps du message, supporte {{ctx_key}}

Variables d'environnement (.env) — pour le connecteur email uniquement :
    SMTP_HOST          — Serveur SMTP              (défaut: 172.29.18.131)
    SMTP_PORT          — Port SMTP                 (défaut: 25)
    SMTP_TLS           — Activer STARTTLS          (défaut: False)
    SMTP_USER          — Login SMTP optionnel      (défaut: vide)
    SMTP_PASSWORD      — Mot de passe SMTP optionnel (défaut: vide)
    EMAIL_FROM         — Expéditeur affiché        (défaut: workflow-engine@localhost)
    DEFAULT_RECIPIENT  — Destinataire de secours   (défaut: vide)
"""
import json
import logging
import smtplib
import socket
import ssl
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

from decouple import config as env_config

from .base import BaseExecutor

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_vars(text: str, context: dict) -> str:
    """Remplace {{key}} par la valeur du contexte d'exécution."""
    if not text:
        return text or ''
    for key, value in context.items():
        text = text.replace(f'{{{{{key}}}}}', str(value))
    return text


def _parse_recipients(raw: str) -> list[str]:
    """Éclate une chaîne 'a@b.com, c@d.com' en liste propre."""
    return [addr.strip() for addr in raw.split(',') if addr.strip()]


# ─────────────────────────────────────────────────────────────────────────────
# Exécuteur
# ─────────────────────────────────────────────────────────────────────────────

class NotificationExecutor(BaseExecutor):
    """
    Envoie une notification via Email (SMTP), Microsoft Teams ou Slack.

    Les champs to/subject/body/webhookUrl sont lus depuis la configuration
    du nœud (canvas) et les variables {{ctx_key}} sont résolues depuis le
    contexte d'exécution.
    """

    # ── Lecture de la config SMTP depuis .env ─────────────────────────────
    _SMTP_HOST     = env_config('SMTP_HOST',     default='172.29.18.131')
    _SMTP_PORT     = int(env_config('SMTP_PORT', default='25'))
    _SMTP_TLS      = env_config('SMTP_TLS',      default='False').lower() in ('true', '1', 'yes')
    _SMTP_USER     = env_config('SMTP_USER',     default='')
    _SMTP_PASSWORD = env_config('SMTP_PASSWORD', default='')
    _EMAIL_FROM    = env_config('EMAIL_FROM',    default='y.belghali@attijariwafa.com')
    _DEFAULT_TO    = env_config('DEFAULT_RECIPIENT', default='')

    def run(self) -> dict:
        connector = str(self.cfg('channel') or self.cfg('connector', 'email')).lower().strip()

        subject = _resolve_vars(
            self.cfg('subject', 'Notification Workflow').strip(),
            self.context,
        )
        body = _resolve_vars(
            (self.cfg('body', '') or self.cfg('message', '')).strip()
            or 'Exécution du workflow terminée.',
            self.context,
        )

        if connector == 'teams':
            return {'connector': 'teams', 'teams': self._send_teams(subject, body)}
        elif connector == 'slack':
            return {'connector': 'slack', 'slack': self._send_slack(subject, body)}
        else:
            to_raw = (
                self.cfg('to', '').strip()
                or self.cfg('recipient', '').strip()
                or self.ctx('recipient', '').strip()
                or self._DEFAULT_TO
            )
            return {'connector': 'email', 'email': self._send_email(to_raw, subject, body)}

    # ─────────────────────────────────────────────────────────────────────────
    # Envoi SMTP
    # ─────────────────────────────────────────────────────────────────────────

    def _send_email(self, to_raw: str, subject: str, body: str) -> dict:
        recipients = _parse_recipients(to_raw)

        if not recipients:
            logger.warning('[Notification/Email] Aucun destinataire configuré — email ignoré.')
            return {'sent': False, 'reason': 'no_recipient'}

        # ── Détection si le corps est un rapport HTML complet ────────────────
        body_lower = body.lower().strip()
        is_html_report = '<html' in body_lower or '<body' in body_lower or '<!doctype' in body_lower

        # ── Construction du message MIME (mixed = corps + pièce jointe) ──────
        if is_html_report:
            # Le rapport HTML est envoyé en pièce jointe pour éviter
            # que Exchange/Outlook ne bloque le contenu HTML externe.
            msg = MIMEMultipart('mixed')
            msg['From']    = self._EMAIL_FROM
            msg['To']      = ', '.join(recipients)
            msg['Subject'] = subject

            # Corps texte simple lisible dans la prévisualisation Outlook
            plain_intro = (
                f"Bonjour,\n\n"
                f"Veuillez trouver ci-joint le rapport d'analyse généré par Workflow Engine.\n"
                f"Ouvrez la pièce jointe 'rapport_workflow.html' dans votre navigateur\n"
                f"pour consulter le tableau de bord complet.\n\n"
                f"Sujet : {subject}\n"
                f"---\nWorkflow Engine — Notification automatique"
            )
            msg.attach(MIMEText(plain_intro, 'plain', 'utf-8'))

            # Pièce jointe HTML
            attachment = MIMEApplication(body.encode('utf-8'), _subtype='octet-stream')
            attachment.add_header(
                'Content-Disposition',
                'attachment',
                filename='rapport_workflow.html',
            )
            msg.attach(attachment)

        else:
            # Corps texte simple : pas de rapport IA, message standard
            msg = MIMEMultipart('alternative')
            msg['From']    = self._EMAIL_FROM
            msg['To']      = ', '.join(recipients)
            msg['Subject'] = subject
            msg.attach(MIMEText(body, 'plain', 'utf-8'))

        # ── Connexion et envoi SMTP ───────────────────────────────────────
        logger.info(
            f'[Notification/Email] Connexion SMTP → {self._SMTP_HOST}:{self._SMTP_PORT} '
            f'(TLS={self._SMTP_TLS}) — destinataires: {recipients}'
        )

        try:
            with smtplib.SMTP(
                host=self._SMTP_HOST,
                port=self._SMTP_PORT,
                timeout=15,
            ) as server:
                server.ehlo_or_helo_if_needed()

                if self._SMTP_TLS:
                    server.starttls()
                    server.ehlo()

                if self._SMTP_USER and self._SMTP_PASSWORD:
                    server.login(self._SMTP_USER, self._SMTP_PASSWORD)

                server.sendmail(
                    from_addr=self._EMAIL_FROM,
                    to_addrs=recipients,
                    msg=msg.as_string(),
                )

            logger.info(
                f'[Notification/Email] ✅ E-mail envoyé à {recipients} '
                f'(sujet: "{subject}")'
            )
            return {
                'sent':       True,
                'recipients': recipients,
                'subject':    subject,
                'smtp_host':  self._SMTP_HOST,
                'smtp_port':  self._SMTP_PORT,
            }

        except (smtplib.SMTPException, socket.timeout, OSError) as exc:
            logger.error(f'[Notification/Email] ❌ Erreur SMTP : {exc}')
            return {
                'sent':  False,
                'error': str(exc)[:400],
            }

    # ─────────────────────────────────────────────────────────────────────────
    # Microsoft Teams — Webhook entrant
    # ─────────────────────────────────────────────────────────────────────────

    def _send_teams(self, subject: str, body: str) -> dict:
        webhook_url = _resolve_vars(
            (self.cfg('webhookUrl', '') or '').strip(), self.context
        )
        if not webhook_url:
            logger.warning('[Notification/Teams] webhookUrl est vide — envoi ignoré.')
            return {'sent': False, 'reason': 'no_webhook_url'}

        is_html = '<html' in body.lower() or '<table' in body.lower()

        if is_html:
            payload = {
                "type": "message",
                "attachments": [{
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard",
                        "version": "1.4",
                        "body": [
                            {"type": "TextBlock", "text": subject, "weight": "Bolder", "size": "Medium"},
                            {"type": "TextBlock", "text": body[:2000], "wrap": True},
                        ]
                    }
                }]
            }
        else:
            payload = {
                "type": "message",
                "attachments": [{
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard",
                        "version": "1.4",
                        "body": [
                            {"type": "TextBlock", "text": subject, "weight": "Bolder", "size": "Medium"},
                            {"type": "TextBlock", "text": body, "wrap": True},
                        ]
                    }
                }]
            }

        return self._post_webhook(webhook_url, payload, 'Teams')

    # ─────────────────────────────────────────────────────────────────────────
    # Slack — Webhook entrant
    # ─────────────────────────────────────────────────────────────────────────

    def _send_slack(self, subject: str, body: str) -> dict:
        webhook_url = _resolve_vars(
            (self.cfg('webhookUrl', '') or '').strip(), self.context
        )
        if not webhook_url:
            logger.warning('[Notification/Slack] webhookUrl est vide — envoi ignoré.')
            return {'sent': False, 'reason': 'no_webhook_url'}

        payload = {
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": subject[:150]}
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": body[:3000]}
                },
                {
                    "type": "context",
                    "elements": [
                        {"type": "mrkdwn", "text": "📤 _Envoyé par Workflow Engine_"}
                    ]
                }
            ]
        }

        return self._post_webhook(webhook_url, payload, 'Slack')

    # ─────────────────────────────────────────────────────────────────────────
    # Helper commun pour les webhooks
    # ─────────────────────────────────────────────────────────────────────────

    def _post_webhook(self, url: str, payload: dict, provider: str) -> dict:
        data = json.dumps(payload).encode('utf-8')
        req = Request(
            url, data=data, method='POST',
            headers={'Content-Type': 'application/json'}
        )

        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        logger.info(f'[Notification/{provider}] POST → {url[:80]}...')

        try:
            with urlopen(req, timeout=15, context=ssl_ctx) as resp:
                status = resp.status
                resp_body = resp.read().decode('utf-8', errors='replace')[:200]

            logger.info(f'[Notification/{provider}] ✅ HTTP {status}')
            return {'sent': True, 'status': status, 'response': resp_body}

        except HTTPError as exc:
            err = exc.read().decode('utf-8', errors='replace')[:300]
            logger.error(f'[Notification/{provider}] ❌ HTTP {exc.code}: {err}')
            return {'sent': False, 'error': f'HTTP {exc.code}: {err}'}
        except (URLError, socket.timeout, OSError) as exc:
            logger.error(f'[Notification/{provider}] ❌ {exc}')
            return {'sent': False, 'error': str(exc)[:300]}
