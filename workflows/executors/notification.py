"""
Notification — Exécuteur Email via SMTP.

Envoie un e-mail en utilisant le serveur SMTP interne (environnement de dev).
Les variables {{ctx_key}} dans subject et body sont résolues depuis le contexte.

Config du nœud (canvas React Flow) :
    connector  : str  — seule valeur supportée : 'email'  (ignoré si absent)
    to         : str  — adresse(s) destinataire(s), séparées par ','
    subject    : str  — sujet de l'e-mail, supporte {{ctx_key}}
    body       : str  — corps du message en texte brut, supporte {{ctx_key}}

Variables d'environnement (.env) :
    SMTP_HOST          — Serveur SMTP              (défaut: 172.29.18.131)
    SMTP_PORT          — Port SMTP                 (défaut: 25)
    SMTP_TLS           — Activer STARTTLS          (défaut: False)
    SMTP_USER          — Login SMTP optionnel      (défaut: vide)
    SMTP_PASSWORD      — Mot de passe SMTP optionnel (défaut: vide)
    EMAIL_FROM         — Expéditeur affiché        (défaut: workflow-engine@localhost)
    DEFAULT_RECIPIENT  — Destinataire de secours   (défaut: vide)
"""
import logging
import smtplib
import socket
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

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
    Envoie un e-mail via le serveur SMTP configuré dans .env.

    Seul le connecteur 'email' est actif. Les champs to/subject/body
    sont lus depuis la configuration du nœud (canvas) et les variables
    {{ctx_key}} sont résolues depuis le contexte d'exécution.
    """

    # ── Lecture de la config SMTP depuis .env ─────────────────────────────
    _SMTP_HOST     = env_config('SMTP_HOST',     default='172.29.18.131')
    _SMTP_PORT     = int(env_config('SMTP_PORT', default='25'))
    _SMTP_TLS      = env_config('SMTP_TLS',      default='False').lower() in ('true', '1', 'yes')
    _SMTP_USER     = env_config('SMTP_USER',     default='')
    _SMTP_PASSWORD = env_config('SMTP_PASSWORD', default='')
    _EMAIL_FROM    = env_config('EMAIL_FROM',    default='workflow-engine@localhost')
    _DEFAULT_TO    = env_config('DEFAULT_RECIPIENT', default='')

    def run(self) -> dict:
        # Le seul connecteur disponible est 'email'.
        connector = str(self.cfg('channel') or self.cfg('connector', 'email')).lower().strip()

        if connector != 'email':
            logger.warning(
                f'[Notification] Connecteur "{connector}" non supporté — seul "email" est actif.'
            )
            return {'sent': False, 'reason': f'connector_not_supported: {connector}'}

        # ── Résolution des champs depuis la config du nœud ────────────────
        to_raw  = (
            self.cfg('to', '').strip()
            or self.cfg('recipient', '').strip()   # rétrocompat
            or self.ctx('recipient', '').strip()   # depuis le contexte
            or self._DEFAULT_TO
        )
        subject = _resolve_vars(
            self.cfg('subject', 'Notification Workflow').strip(),
            self.context,
        )
        body = _resolve_vars(
            (self.cfg('body', '') or self.cfg('message', '')).strip()
            or 'Exécution du workflow terminée.',
            self.context,
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

        # ── Construction du message MIME ──────────────────────────────────
        msg = MIMEMultipart('alternative')
        msg['From']    = self._EMAIL_FROM
        msg['To']      = ', '.join(recipients)
        msg['Subject'] = subject

        # Partie texte brut
        msg.attach(MIMEText(body, 'plain', 'utf-8'))

        # Partie HTML (mise en page simple)
        html_body = f"""\
<!DOCTYPE html>
<html lang="fr">
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="padding:32px 0;">
    <tr>
      <td align="center">
        <table width="600" cellpadding="0" cellspacing="0"
               style="background:#ffffff;border-radius:10px;
                      box-shadow:0 4px 16px rgba(0,0,0,.08);overflow:hidden;">
          <!-- En-tête -->
          <tr>
            <td style="background:linear-gradient(135deg,#1e1b4b,#4f46e5);
                        padding:28px 32px;text-align:center;">
              <h1 style="color:#ffffff;margin:0;font-size:20px;font-weight:700;
                          letter-spacing:.5px;">⚙️ Workflow Engine</h1>
            </td>
          </tr>
          <!-- Contenu -->
          <tr>
            <td style="padding:32px;">
              <h2 style="color:#1e293b;font-size:17px;margin-top:0;">{subject}</h2>
              <p style="color:#475569;line-height:1.7;font-size:14px;
                         white-space:pre-wrap;">{body}</p>
            </td>
          </tr>
          <!-- Pied de page -->
          <tr>
            <td style="padding:16px 32px;background:#f8fafc;text-align:center;
                        border-top:1px solid #e2e8f0;">
              <p style="color:#94a3b8;font-size:11px;margin:0;">
                Envoyé automatiquement par <strong>Workflow Engine</strong>
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""
        msg.attach(MIMEText(html_body, 'html', 'utf-8'))

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
