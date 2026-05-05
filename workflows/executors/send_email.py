"""
Send Email — Executor.

Envoie le rapport HTML du pipeline via l'API Resend (https://resend.com).

Variables d'environnement (.env) :
    RESEND_API_KEY = re_123456...         # Clé API Resend
    EMAIL_FROM     = onboarding@resend.dev # Expéditeur (doit être vérifié sur Resend)

Config du nœud (canvas) :
    recipientEmail : str  — Destinataire (obligatoire)
    subject        : str  — Sujet de l'email

Inputs depuis le contexte partagé :
    report_json    : str  — JSON produit par GenerateReportExecutor
"""
import json
import logging
import urllib.request
import urllib.error
import time

from decouple import config

from .base import BaseExecutor

logger = logging.getLogger(__name__)


class SendEmailExecutor(BaseExecutor):
    """
    email.Send — Envoie le rapport HTML via SMTP.
    Utilise smtplib natif pour un contrôle total du protocole.
    """

    def run(self) -> dict:
        # ── 1. Destinataire ───────────────────────────────────────────────────
        recipient = (
            self.cfg('recipientEmail', '').strip()
            or self.ctx('recipientEmail', '').strip()
            or config('DEFAULT_RECIPIENT', default='').strip()
        )
        if not recipient:
            logger.warning('[SendEmail] Aucun destinataire configuré — email ignoré')
            return {
                'sent':    False,
                'mode':    'no_recipient',
                'overall': 'UNKNOWN',
            }

        # ── 2. Sujet ──────────────────────────────────────────────────────────
        subject = self.cfg('subject', "Rapport d'exécution CI/CD Pipeline").strip()

        # ── 3. Rapport ────────────────────────────────────────────────────────
        report_json = self.ctx('report_json', '{}')
        try:
            report = json.loads(report_json)
        except (json.JSONDecodeError, TypeError):
            report = {}

        overall   = report.get('overall', 'UNKNOWN')
        src       = report.get('source', {})
        build     = report.get('build', {})
        tests     = report.get('tests', {})
        gen_at    = report.get('generated_at', '?')

        # ── 4. Configuration Resend depuis .env ──────────────────────────────────
        resend_api_key = config('RESEND_API_KEY', default='').strip()
        from_addr      = config(
            'EMAIL_FROM',
            default='Workflow Engine <onboarding@resend.dev>'
        ).strip()

        # ── 5. Mode simulation si pas de clé API ──────────────────────────────
        if not resend_api_key:
            logger.warning('[SendEmail] RESEND_API_KEY non configurée → mode simulation')
            return {
                'sent':      False,
                'mode':      'simulation',
                'overall':   overall,
                'recipient': recipient,
                'message':   'Email simulé : configurez RESEND_API_KEY dans .env pour envoyer réellement.',
            }

        # ── 6. Construction du message ────────────────────────────────────────
        emoji     = '✅' if overall == 'SUCCESS' else '❌'
        full_subj = f'{emoji} {subject} — {overall}'

        html_body  = self._build_html(overall, emoji, src, build, tests, gen_at)
        plain_body = self._build_plain(overall, src, build, tests, gen_at)

        # ── 7. Envoi via Resend API ─────────────────────────────────────────────
        logger.info(f'[SendEmail] Envoi via Resend à {recipient}')

        try:
            res = self._send_resend(
                api_key=resend_api_key,
                from_addr=from_addr,
                to_addr=recipient,
                subject=full_subj,
                html=html_body,
                text=plain_body,
            )

            logger.info(f'[SendEmail] Email envoyé avec succès (ID: {res.get("id")})')
            return {
                'sent':      True,
                'recipient': recipient,
                'overall':   overall,
                'resend_id': res.get('id'),
                'mode':      'resend',
            }

        except Exception as exc:
            logger.error(f'[SendEmail] Erreur Resend : {exc}')
            return {
                'sent':      False,
                'mode':      'error',
                'recipient': recipient,
                'overall':   overall,
                'error':     str(exc)[:500],
            }

    # ─────────────────────────────────────────────────────────────────────────
    # Envoi via l'API REST de Resend
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _send_resend(
        api_key: str,
        from_addr: str,
        to_addr: str,
        subject: str,
        html: str,
        text: str,
    ) -> dict:
        """
        Envoie un email en utilisant l'API REST de Resend via urllib.request (sans dépendance externe).
        """
        url = "https://api.resend.com/emails"
        
        payload = {
            "from": from_addr,
            "to": [to_addr],
            "subject": subject,
            "html": html,
            "text": text
        }
        
        data = json.dumps(payload).encode('utf-8')
        
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "WorkflowEngine/1.0")
        
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                response_data = response.read().decode('utf-8')
                return json.loads(response_data)
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8')
            raise Exception(f"HTTP {e.code}: {error_body}") from e
        except Exception as e:
            raise Exception(f"Erreur de connexion à Resend: {str(e)}") from e

    # ─────────────────────────────────────────────────────────────────────────
    # Contenu de l'email
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _build_plain(overall: str, src: dict, build: dict, tests: dict, gen_at: str) -> str:
        """Construit la version texte brut de l'email."""
        sep = '=' * 55
        status_line = '✅ SUCCÈS' if overall == 'SUCCESS' else '❌ ÉCHEC'
        return (
            f"Workflow Engine — Rapport Django CI\n{sep}\n\n"
            f"Statut global : {status_line}\n\n"
            f"--- Source ---\n"
            f"  Commit  : {src.get('commit', '?')}\n"
            f"  Branche : {src.get('branch', '?')}\n"
            f"  Auteur  : {src.get('author', '?')}\n"
            f"  Message : {src.get('message', '?')}\n\n"
            f"--- Résultats des tests ---\n"
            f"  Passés   : {tests.get('passed', 0)}\n"
            f"  Échoués  : {tests.get('failed', 0)}\n"
            f"  Coverage : {tests.get('coverage', 0)}%\n"
            f"  Durée    : {tests.get('duration', 0)}s\n\n"
            f"{sep}\n"
            f"Généré le {gen_at} par Workflow Engine\n"
        )

    @staticmethod
    def _build_html(
        overall: str,
        emoji: str,
        src: dict,
        build: dict,
        tests: dict,
        gen_at: str,
    ) -> str:
        """Construit le rapport HTML pour Django CI pipeline."""
        color    = '#16a34a' if overall == 'SUCCESS' else '#dc2626'
        bg_badge = '#dcfce7' if overall == 'SUCCESS' else '#fee2e2'
        bld_ok   = build.get('status') == 'SUCCESS'
        bld_color = '#16a34a' if bld_ok else '#dc2626'
        test_output = str(tests.get('output', '')).strip()[:600]
        total = int(tests.get('passed', 0)) + int(tests.get('failed', 0))

        test_output_block = ''
        if test_output:
            test_output_block = f"""
    <!-- Sortie des tests -->
    <tr><td style="padding:16px 32px 0;">
      <h2 style="color:#1e293b;font-size:13px;font-weight:700;text-transform:uppercase;
                 letter-spacing:.8px;margin:0 0 10px;border-bottom:2px solid #e2e8f0;padding-bottom:8px;">
        📝 Sortie des tests
      </h2>
      <pre style="background:#0f172a;color:#e2e8f0;font-size:11px;padding:14px;border-radius:8px;
                  overflow:auto;white-space:pre-wrap;word-break:break-all;max-height:300px;">{test_output}</pre>
    </td></tr>"""

        return f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Workflow Engine — Rapport Django CI</title>
</head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;padding:32px 0;">
  <tr><td align="center">
  <table width="620" cellpadding="0" cellspacing="0"
         style="background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.10);">

    <!-- Header -->
    <tr><td style="background:linear-gradient(135deg,#1e1b4b 0%,#312e81 100%);padding:28px 32px;text-align:center;">
      <h1 style="color:#fff;margin:8px 0 4px;font-size:22px;font-weight:700;">⚙️ Workflow Engine</h1>
      <p style="color:#a5b4fc;margin:0;font-size:13px;">Rapport Django CI/CD</p>
    </td></tr>

    <!-- Badge statut global -->
    <tr><td style="padding:24px 32px 0;text-align:center;">
      <span style="display:inline-block;background:{bg_badge};color:{color};
                   border:1.5px solid {color};border-radius:999px;
                   padding:10px 32px;font-size:17px;font-weight:700;">
        {emoji} {overall}
      </span>
    </td></tr>

    <!-- Source -->
    <tr><td style="padding:24px 32px 0;">
      <h2 style="color:#1e293b;font-size:13px;font-weight:700;text-transform:uppercase;
                 letter-spacing:.8px;margin:0 0 12px;border-bottom:2px solid #e2e8f0;padding-bottom:8px;">
        📦 Source
      </h2>
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td style="color:#64748b;font-size:13px;padding:4px 0;width:100px;">Commit</td>
          <td style="color:#1e293b;font-size:13px;font-weight:600;font-family:monospace;">{src.get("commit", "?")}</td>
        </tr>
        <tr>
          <td style="color:#64748b;font-size:13px;padding:4px 0;">Branche</td>
          <td style="color:#1e293b;font-size:13px;">{src.get("branch", "main")}</td>
        </tr>
        <tr>
          <td style="color:#64748b;font-size:13px;padding:4px 0;">Auteur</td>
          <td style="color:#1e293b;font-size:13px;">{src.get("author", "?")}</td>
        </tr>
        <tr>
          <td style="color:#64748b;font-size:13px;padding:4px 0;">Message</td>
          <td style="color:#1e293b;font-size:13px;font-style:italic;">{src.get("message", "?")}</td>
        </tr>
      </table>
    </td></tr>

    <!-- Résultats Django Tests -->
    <tr><td style="padding:20px 32px 0;">
      <h2 style="color:#1e293b;font-size:13px;font-weight:700;text-transform:uppercase;
                 letter-spacing:.8px;margin:0 0 12px;border-bottom:2px solid #e2e8f0;padding-bottom:8px;">
        🧪 Tests Django
      </h2>
      <table width="100%" cellpadding="0" cellspacing="0"><tr>
        <td style="width:30%;text-align:center;background:#f0fdf4;border-radius:8px;padding:16px;">
          <div style="font-size:32px;font-weight:700;color:#16a34a;">{tests.get("passed", 0)}</div>
          <div style="font-size:11px;color:#64748b;margin-top:4px;">Passés ✓</div>
        </td>
        <td style="width:4%;"></td>
        <td style="width:30%;text-align:center;background:#fef2f2;border-radius:8px;padding:16px;">
          <div style="font-size:32px;font-weight:700;color:#dc2626;">{tests.get("failed", 0)}</div>
          <div style="font-size:11px;color:#64748b;margin-top:4px;">Échoués ✗</div>
        </td>
        <td style="width:4%;"></td>
        <td style="width:32%;text-align:center;background:#f8fafc;border-radius:8px;padding:16px;">
          <div style="font-size:32px;font-weight:700;color:#334155;">{total}</div>
          <div style="font-size:11px;color:#64748b;margin-top:4px;">Total</div>
        </td>
      </tr></table>
      <p style="color:#94a3b8;font-size:12px;margin:10px 0 0;text-align:right;">
        Durée d'exécution : <strong>{tests.get("duration", 0)}s</strong>
        &nbsp;|&nbsp; Statut : <strong style="color:{bld_color};">{build.get("status", "?")}</strong>
      </p>
    </td></tr>

    {test_output_block}

    <!-- Footer -->
    <tr><td style="padding:24px 32px 28px;text-align:center;border-top:1px solid #e2e8f0;margin-top:20px;">
      <p style="color:#94a3b8;font-size:11px;margin:0;">
        Généré le {gen_at} par <strong>Workflow Engine</strong>
      </p>
    </td></tr>

  </table>
  </td></tr>
</table>
</body>
</html>"""
