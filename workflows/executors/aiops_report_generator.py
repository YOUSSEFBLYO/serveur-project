"""
AIOps — Report Generator (nœud aiops.ReportGenerator)

Prend l'analyse produite par LogClassifierExecutor depuis le contexte
et génère un rapport formaté (HTML email ou Markdown) prêt à être envoyé
par le nœud Notification suivant.

Config du nœud (canvas React Flow) :
    title          : str  — Titre du rapport  (défaut: Rapport AIOps)
    input_key      : str  — Clé du contexte contenant l'analyse IA  (défaut: ai_analysis)
    logs_key       : str  — Clé du contexte contenant les logs bruts (défaut: es_logs)
    output_format  : str  — html | markdown | text  (défaut: html)
    include_raw_logs: bool — Inclure les N premiers logs bruts  (défaut: False)
    max_raw_logs   : int  — Nombre de logs bruts à inclure si activé  (défaut: 10)
    output_key     : str  — Clé de sortie du rapport  (défaut: aiops_report)
    report_to      : str  — Pré-rempli dans le contexte pour le nœud Notification suivant
"""
import json
import logging
from datetime import datetime, timezone

from .base import BaseExecutor

logger = logging.getLogger(__name__)


def _resolve_vars(text: str, context: dict) -> str:
    """Remplace {{key}} par la valeur du contexte d'exécution."""
    if not text:
        return text or ''
    for key, value in context.items():
        text = text.replace(f'{{{{{key}}}}}', str(value))
    return text


# ─────────────────────────────────────────────────────────────────────────────
# Icônes et couleurs par niveau de santé
# ─────────────────────────────────────────────────────────────────────────────

_HEALTH_CONFIG = {
    'HEALTHY':  {'icon': '✅', 'color': '#10b981', 'bg': '#ecfdf5', 'label': 'Sain'},
    'DEGRADED': {'icon': '⚠️', 'color': '#f59e0b', 'bg': '#fffbeb', 'label': 'Dégradé'},
    'CRITICAL': {'icon': '🚨', 'color': '#ef4444', 'bg': '#fef2f2', 'label': 'Critique'},
    'UNKNOWN':  {'icon': '❓', 'color': '#6b7280', 'bg': '#f9fafb', 'label': 'Inconnu'},
}

_SEVERITY_COLORS = {
    'CRITICAL': '#ef4444',
    'ERROR':    '#f97316',
    'WARNING':  '#f59e0b',
    'INFO':     '#3b82f6',
}


class ReportGeneratorExecutor(BaseExecutor):
    """
    Génère un rapport AIOps formaté depuis l'analyse IA et les métadonnées ES.

    Produit :
      - aiops_report      : str  → rapport formaté (HTML, Markdown ou texte)
      - aiops_report_html : str  → toujours disponible en HTML (pour l'email)
      - aiops_subject     : str  → sujet d'email pré-formaté
      - report_ready      : bool → True (signal pour le nœud suivant)
    """

    def run(self) -> dict:
        # ── Paramètres ────────────────────────────────────────────────────────
        title       = _resolve_vars(
            self.cfg('title', 'Rapport AIOps') or 'Rapport AIOps',
            self.context,
        )
        input_key   = (self.cfg('input_key',  'ai_analysis') or 'ai_analysis').strip()
        logs_key    = (self.cfg('logs_key',   'es_logs')     or 'es_logs').strip()
        out_format  = (self.cfg('output_format', 'html')     or 'html').strip().lower()
        output_key  = (self.cfg('output_key', 'aiops_report') or 'aiops_report').strip()
        include_raw = str(self.cfg('include_raw_logs', False)).lower() in ('true', '1', 'yes')
        max_raw     = int(self.cfg('max_raw_logs', 10) or 10)
        report_to   = _resolve_vars(
            (self.cfg('report_to', '') or '').strip(), self.context
        )

        # ── Récupération depuis le contexte ───────────────────────────────────
        analysis: dict = self.context.get(input_key, {})
        logs: list     = self.context.get(logs_key, [])

        # Métadonnées Elasticsearch (injectées par ElasticsearchFetchExecutor)
        es_index      = self.context.get('es_index',      'inconnu')
        es_fetched    = self.context.get('es_fetched',    len(logs))
        es_total      = self.context.get('es_total',      len(logs))
        es_time_range = self.context.get('es_time_range', 'inconnu')
        es_took_ms    = self.context.get('es_took_ms',    0)

        if not analysis:
            logger.warning(
                f'[ReportGenerator] Aucune analyse trouvée dans le contexte '
                f'(clé: "{input_key}") — rapport vide généré.'
            )

        health      = analysis.get('overall_health', 'UNKNOWN')
        hcfg        = _HEALTH_CONFIG.get(health, _HEALTH_CONFIG['UNKNOWN'])
        now_str     = datetime.now(timezone.utc).strftime('%d/%m/%Y à %H:%M UTC')
        needs_action = analysis.get('needs_immediate_action', False)

        # ── Génération du rapport ─────────────────────────────────────────────
        if out_format == 'markdown':
            report = self._build_markdown(
                title, analysis, hcfg, now_str,
                es_index, es_fetched, es_total, es_time_range, es_took_ms,
                logs if include_raw else [], max_raw
            )
        elif out_format == 'text':
            report = self._build_text(
                title, analysis, hcfg, now_str,
                es_index, es_fetched, es_total, es_time_range,
            )
        else:
            report = self._build_html(
                title, analysis, hcfg, now_str,
                es_index, es_fetched, es_total, es_time_range, es_took_ms,
                logs if include_raw else [], max_raw,
                needs_action
            )

        html_report = (
            report if out_format == 'html'
            else self._build_html(
                title, analysis, hcfg, now_str,
                es_index, es_fetched, es_total, es_time_range, es_took_ms,
                logs if include_raw else [], max_raw, needs_action
            )
        )

        # Sujet d'email pré-formaté
        subject = (
            f"{hcfg['icon']} [{health}] {title} — {now_str}"
            if needs_action
            else f"{hcfg['icon']} {title} — {health} — {now_str}"
        )

        logger.info(
            f'[ReportGenerator] ✅ Rapport "{title}" généré '
            f'(format={out_format}, health={health}, '
            f'anomalies={len(analysis.get("anomalies", []))})'
        )

        result = {
            output_key:         report,
            'aiops_report':     report,
            'aiops_report_html': html_report,
            'aiops_subject':    subject,
            'report_ready':     True,
            'report_title':     title,
            'report_health':    health,
            'report_generated_at': now_str,
            # Pré-remplit les champs du nœud Notification suivant
            'subject':          subject,
            'body':             report if out_format != 'html' else analysis.get('summary', ''),
        }
        if report_to:
            result['to'] = report_to

        return result

    # ── Builders ──────────────────────────────────────────────────────────────

    def _build_html(self, title, analysis, hcfg, now_str,
                    es_index, es_fetched, es_total, es_time_range, es_took_ms,
                    raw_logs, max_raw, needs_action) -> str:

        anomalies  = analysis.get('anomalies', [])
        top_errors = analysis.get('top_errors', [])
        actions    = analysis.get('immediate_actions', [])
        summary    = analysis.get('summary', 'Aucun résumé disponible.')

        # ── Bannière urgence ──────────────────────────────────────────────────
        urgency_banner = ''
        if needs_action:
            urgency_banner = f"""
            <tr>
              <td style="background:#fef2f2;border-left:4px solid #ef4444;
                          padding:14px 24px;margin-bottom:0;">
                <strong style="color:#ef4444;">🚨 ACTION IMMÉDIATE REQUISE</strong><br>
                <ul style="margin:8px 0 0;padding-left:20px;color:#7f1d1d;font-size:13px;">
                  {''.join(f'<li>{a}</li>' for a in actions)}
                </ul>
              </td>
            </tr>"""

        # ── Compteurs ─────────────────────────────────────────────────────────
        counters_html = ''.join([
            self._counter_cell('CRITICAL', analysis.get('critical_count', 0), '#ef4444'),
            self._counter_cell('ERROR',    analysis.get('error_count', 0),    '#f97316'),
            self._counter_cell('WARNING',  analysis.get('warning_count', 0),  '#f59e0b'),
            self._counter_cell('INFO',     analysis.get('info_count', 0),     '#3b82f6'),
        ])

        # ── Anomalies ─────────────────────────────────────────────────────────
        anomalies_html = ''
        if anomalies:
            rows = ''
            for a in anomalies:
                sev   = a.get('severity', 'WARNING')
                color = _SEVERITY_COLORS.get(sev, '#6b7280')
                rows += f"""
                <tr style="border-bottom:1px solid #f1f5f9;">
                  <td style="padding:10px 12px;">
                    <span style="background:{color};color:#fff;border-radius:4px;
                                 padding:2px 8px;font-size:11px;font-weight:700;">
                      {sev}
                    </span>
                  </td>
                  <td style="padding:10px 12px;color:#1e293b;font-size:13px;">
                    {a.get('description', '')}
                  </td>
                  <td style="padding:10px 12px;color:#64748b;font-size:12px;">
                    {a.get('affected_service', '—')}
                  </td>
                  <td style="padding:10px 12px;color:#64748b;font-size:12px;">
                    ×{a.get('count', 1)}
                  </td>
                  <td style="padding:10px 12px;color:#059669;font-size:12px;font-style:italic;">
                    {a.get('recommendation', '—')}
                  </td>
                </tr>"""

            anomalies_html = f"""
            <tr><td style="padding:24px 32px 8px;">
              <h3 style="color:#1e293b;font-size:15px;margin:0 0 12px;">
                🔍 Anomalies détectées ({len(anomalies)})
              </h3>
              <table width="100%" cellpadding="0" cellspacing="0"
                     style="border-collapse:collapse;border:1px solid #e2e8f0;border-radius:8px;overflow:hidden;">
                <thead>
                  <tr style="background:#f8fafc;">
                    <th style="padding:10px 12px;text-align:left;font-size:12px;color:#64748b;">Sévérité</th>
                    <th style="padding:10px 12px;text-align:left;font-size:12px;color:#64748b;">Description</th>
                    <th style="padding:10px 12px;text-align:left;font-size:12px;color:#64748b;">Service</th>
                    <th style="padding:10px 12px;text-align:left;font-size:12px;color:#64748b;">Occurrences</th>
                    <th style="padding:10px 12px;text-align:left;font-size:12px;color:#64748b;">Recommandation</th>
                  </tr>
                </thead>
                <tbody>{rows}</tbody>
              </table>
            </td></tr>"""

        # ── Top Errors ────────────────────────────────────────────────────────
        top_errors_html = ''
        if top_errors:
            err_items = ''
            for e in top_errors[:5]:
                level = e.get('level', 'ERROR')
                color = _SEVERITY_COLORS.get(level, '#f97316')
                err_items += f"""
                <tr style="border-bottom:1px solid #f8fafc;">
                  <td style="padding:8px 12px;font-size:12px;color:{color};font-weight:600;">
                    {level}
                  </td>
                  <td style="padding:8px 12px;font-size:12px;color:#475569;font-family:monospace;">
                    {str(e.get('message',''))[:120]}
                  </td>
                  <td style="padding:8px 12px;font-size:12px;color:#94a3b8;text-align:right;">
                    ×{e.get('count',1)}
                  </td>
                </tr>"""

            top_errors_html = f"""
            <tr><td style="padding:8px 32px 24px;">
              <h3 style="color:#1e293b;font-size:15px;margin:0 0 12px;">
                📋 Erreurs fréquentes
              </h3>
              <table width="100%" cellpadding="0" cellspacing="0"
                     style="border-collapse:collapse;border:1px solid #e2e8f0;border-radius:8px;">
                <tbody>{err_items}</tbody>
              </table>
            </td></tr>"""

        # ── Logs bruts (optionnel) ────────────────────────────────────────────
        raw_logs_html = ''
        if raw_logs:
            items = ''
            for log in raw_logs[:max_raw]:
                lvl   = log.get('level', 'INFO').upper()
                color = _SEVERITY_COLORS.get(lvl, '#6b7280')
                msg   = str(log.get('message', log))[:200]
                ts    = log.get('timestamp', '')
                svc   = log.get('service', '')
                items += f"""
                <tr style="border-bottom:1px solid #f8fafc;">
                  <td style="padding:6px 10px;font-size:11px;color:{color};
                              font-weight:700;white-space:nowrap;">{lvl}</td>
                  <td style="padding:6px 10px;font-size:11px;color:#94a3b8;
                              white-space:nowrap;">{ts[:19]}</td>
                  <td style="padding:6px 10px;font-size:11px;color:#64748b;">{svc}</td>
                  <td style="padding:6px 10px;font-size:11px;color:#475569;
                              font-family:monospace;">{msg}</td>
                </tr>"""

            raw_logs_html = f"""
            <tr><td style="padding:8px 32px 24px;">
              <h3 style="color:#1e293b;font-size:15px;margin:0 0 12px;">
                📄 Logs bruts (premiers {min(len(raw_logs), max_raw)})
              </h3>
              <table width="100%" cellpadding="0" cellspacing="0"
                     style="border-collapse:collapse;border:1px solid #e2e8f0;
                             border-radius:8px;font-family:monospace;">
                <tbody>{items}</tbody>
              </table>
            </td></tr>"""

        return f"""<!DOCTYPE html>
<html lang="fr">
<head><meta charset="utf-8"><title>{title}</title></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="padding:32px 0;">
    <tr><td align="center">
      <table width="700" cellpadding="0" cellspacing="0"
             style="background:#ffffff;border-radius:12px;
                    box-shadow:0 4px 24px rgba(0,0,0,.10);overflow:hidden;">

        <!-- En-tête -->
        <tr>
          <td style="background:linear-gradient(135deg,#0f172a,#1e40af);
                      padding:28px 32px;">
            <h1 style="color:#fff;margin:0;font-size:20px;font-weight:700;">
              🤖 {title}
            </h1>
            <p style="color:#93c5fd;margin:6px 0 0;font-size:13px;">
              Généré le {now_str} · Index: <code style="color:#e0f2fe;">{es_index}</code>
              · Période: <code style="color:#e0f2fe;">{es_time_range}</code>
            </p>
          </td>
        </tr>

        <!-- Bannière urgence -->
        {urgency_banner}

        <!-- Santé globale -->
        <tr>
          <td style="padding:24px 32px 12px;">
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td style="background:{hcfg['bg']};border:1px solid {hcfg['color']};
                            border-radius:10px;padding:16px 20px;">
                  <span style="font-size:28px;">{hcfg['icon']}</span>
                  <strong style="color:{hcfg['color']};font-size:18px;margin-left:10px;">
                    {hcfg['label']}
                  </strong>
                  <p style="color:#475569;margin:10px 0 0;font-size:14px;line-height:1.6;">
                    {summary}
                  </p>
                </td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- Compteurs -->
        <tr>
          <td style="padding:8px 32px 16px;">
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                {counters_html}
                <td style="text-align:right;padding:0 0 0 12px;vertical-align:middle;">
                  <span style="font-size:11px;color:#94a3b8;">
                    {es_fetched}/{es_total} logs · {es_took_ms}ms
                  </span>
                </td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- Anomalies -->
        {anomalies_html}

        <!-- Top Errors -->
        {top_errors_html}

        <!-- Logs bruts -->
        {raw_logs_html}

        <!-- Footer -->
        <tr>
          <td style="padding:16px 32px;background:#f8fafc;
                      border-top:1px solid #e2e8f0;text-align:center;">
            <p style="color:#94a3b8;font-size:11px;margin:0;">
              Rapport généré automatiquement par
              <strong>Workflow Engine — AIOps</strong>
            </p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""

    def _counter_cell(self, label: str, count: int, color: str) -> str:
        return f"""
        <td style="padding:4px 6px;">
          <div style="background:{color}15;border:1px solid {color}40;
                       border-radius:8px;padding:10px 14px;text-align:center;">
            <div style="font-size:22px;font-weight:800;color:{color};">{count}</div>
            <div style="font-size:10px;color:{color};font-weight:600;
                         text-transform:uppercase;letter-spacing:.5px;">{label}</div>
          </div>
        </td>"""

    def _build_markdown(self, title, analysis, hcfg, now_str,
                         es_index, es_fetched, es_total, es_time_range, es_took_ms,
                         raw_logs, max_raw) -> str:
        lines = [
            f"# {hcfg['icon']} {title}",
            f"",
            f"**Généré le** {now_str} · **Index** `{es_index}` · **Période** `{es_time_range}`",
            f"",
            f"## État global : {hcfg['icon']} {hcfg['label']}",
            f"",
            analysis.get('summary', ''),
            f"",
            f"| CRITICAL | ERROR | WARNING | INFO | Logs analysés |",
            f"|----------|-------|---------|------|---------------|",
            f"| {analysis.get('critical_count',0)} | {analysis.get('error_count',0)} | "
            f"{analysis.get('warning_count',0)} | {analysis.get('info_count',0)} | "
            f"{es_fetched}/{es_total} |",
            f"",
        ]
        if analysis.get('anomalies'):
            lines.append("## 🔍 Anomalies détectées")
            lines.append("")
            for a in analysis['anomalies']:
                lines.append(
                    f"- **[{a.get('severity','?')}]** {a.get('description','')} "
                    f"— *{a.get('affected_service','?')}* ×{a.get('count',1)}"
                )
                if a.get('recommendation'):
                    lines.append(f"  → {a['recommendation']}")
            lines.append("")

        if analysis.get('needs_immediate_action') and analysis.get('immediate_actions'):
            lines.append("## 🚨 Actions immédiates requises")
            lines.append("")
            for act in analysis['immediate_actions']:
                lines.append(f"- {act}")
            lines.append("")

        if raw_logs:
            lines.append(f"## 📄 Logs bruts (premiers {min(len(raw_logs),max_raw)})")
            lines.append("")
            lines.append("```")
            for log in raw_logs[:max_raw]:
                lines.append(
                    f"[{log.get('level','?')}] {log.get('timestamp','')} "
                    f"[{log.get('service','')}] {str(log.get('message',''))[:150]}"
                )
            lines.append("```")

        lines.append(f"\n---\n*Rapport généré automatiquement par Workflow Engine — AIOps*")
        return '\n'.join(lines)

    def _build_text(self, title, analysis, hcfg, now_str,
                     es_index, es_fetched, es_total, es_time_range) -> str:
        lines = [
            f"{'='*60}",
            f"  {title}",
            f"  Généré le {now_str}",
            f"  Index: {es_index}  |  Période: {es_time_range}",
            f"{'='*60}",
            f"",
            f"ÉTAT GLOBAL : {hcfg['icon']} {hcfg['label']}",
            f"",
            analysis.get('summary', ''),
            f"",
            f"CRITICAL: {analysis.get('critical_count',0)}  "
            f"ERROR: {analysis.get('error_count',0)}  "
            f"WARNING: {analysis.get('warning_count',0)}  "
            f"INFO: {analysis.get('info_count',0)}",
            f"Logs analysés: {es_fetched}/{es_total}",
            f"",
        ]
        if analysis.get('anomalies'):
            lines.append("ANOMALIES DÉTECTÉES :")
            for a in analysis['anomalies']:
                lines.append(
                    f"  [{a.get('severity','?')}] {a.get('description','')} "
                    f"({a.get('affected_service','?')}) ×{a.get('count',1)}"
                )
                if a.get('recommendation'):
                    lines.append(f"    → {a['recommendation']}")
            lines.append("")

        if analysis.get('needs_immediate_action') and analysis.get('immediate_actions'):
            lines.append("ACTIONS IMMÉDIATES :")
            for act in analysis['immediate_actions']:
                lines.append(f"  ! {act}")

        lines.append(f"\n{'─'*60}")
        lines.append("Rapport généré par Workflow Engine — AIOps")
        return '\n'.join(lines)
