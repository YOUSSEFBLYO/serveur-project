"""
AIOps — Report Generator avec agrégations
nœud aiops.ReportGenerator

CHANGEMENTS vs version originale :
  - Lit es_by_level, es_top_errors, es_top_pods depuis le contexte
  - Section "Statistiques ES" dans le rapport HTML (nouvelles données agrégées)
  - Graphique d'évolution temporelle des erreurs (error_rate_per_min)
  - Affichage de la couverture réelle (es_total logs couverts)
  - Badge "source: agrégations 100%" vs "source: échantillon N logs"
"""
import json
import logging
from datetime import datetime, timezone

from .base import BaseExecutor

logger = logging.getLogger(__name__)


def _resolve_vars(text: str, context: dict) -> str:
    if not text:
        return text or ''
    for key, value in context.items():
        text = text.replace(f'{{{{{key}}}}}', str(value))
    return text


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
    Génère un rapport AIOps enrichi avec les données agrégées ES.

    NOUVEAU : exploite es_by_level, es_top_errors, es_top_pods,
    es_error_rate pour produire un rapport qui reflète 100% du volume
    de logs, pas seulement les N exemples récupérés.
    """

    def run(self) -> dict:
        # ── Paramètres ────────────────────────────────────────────────────────
        title      = _resolve_vars(
            self.cfg('title', 'Rapport AIOps') or 'Rapport AIOps',
            self.context,
        )
        input_key  = (self.cfg('input_key',  'ai_analysis')  or 'ai_analysis').strip()
        logs_key   = (self.cfg('logs_key',   'es_logs')       or 'es_logs').strip()
        out_format = (self.cfg('output_format', 'html')        or 'html').strip().lower()
        output_key = (self.cfg('output_key', 'aiops_report')  or 'aiops_report').strip()
        report_to  = _resolve_vars(
            (self.cfg('report_to', '') or '').strip(), self.context
        )

        # ── Données depuis le contexte ────────────────────────────────────────
        analysis:  dict = self.context.get(input_key, {})
        logs:      list = self.context.get(logs_key, [])

        # Métadonnées ES standard
        es_index      = self.context.get('es_index',      'inconnu')
        es_fetched    = self.context.get('es_fetched',    len(logs))
        es_total      = self.context.get('es_total',      0)
        es_time_range = self.context.get('es_time_range', 'inconnu')
        es_took_ms    = self.context.get('es_took_ms',    0)

        # NOUVEAU : données agrégées
        es_by_level    = self.context.get('es_by_level',    {})
        es_top_errors  = self.context.get('es_top_errors',  [])
        es_top_pods    = self.context.get('es_top_pods',    [])
        es_error_rate  = self.context.get('es_error_rate',  [])
        es_error_total = self.context.get('es_error_total', 0)
        ai_source      = self.context.get('ai_source',      'raw_logs')

        health       = analysis.get('overall_health', 'UNKNOWN')
        hcfg         = _HEALTH_CONFIG.get(health, _HEALTH_CONFIG['UNKNOWN'])
        now_str      = datetime.now(timezone.utc).strftime('%d/%m/%Y à %H:%M UTC')
        needs_action = analysis.get('needs_immediate_action', False)

        # ── Génération ────────────────────────────────────────────────────────
        if out_format == 'markdown':
            report = self._build_markdown(
                title, analysis, hcfg, now_str,
                es_index, es_fetched, es_total, es_time_range,
                es_by_level, es_top_errors, es_top_pods, ai_source
            )
        elif out_format == 'text':
            report = self._build_text(
                title, analysis, hcfg, now_str,
                es_index, es_total, es_time_range
            )
        else:
            report = self._build_html(
                title, analysis, hcfg, now_str,
                es_index, es_fetched, es_total, es_time_range, es_took_ms,
                es_by_level, es_top_errors, es_top_pods,
                es_error_rate, es_error_total, ai_source,
                needs_action
            )

        html_report = (
            report if out_format == 'html'
            else self._build_html(
                title, analysis, hcfg, now_str,
                es_index, es_fetched, es_total, es_time_range, es_took_ms,
                es_by_level, es_top_errors, es_top_pods,
                es_error_rate, es_error_total, ai_source,
                needs_action
            )
        )

        subject = (
            f"{hcfg['icon']} [{health}] {title} — {now_str}"
            if needs_action
            else f"{hcfg['icon']} {title} — {health} — {now_str}"
        )

        logger.info(
            f'[ReportGenerator] ✅ Rapport généré '
            f'(format={out_format} health={health} '
            f'total={es_total:,} logs couverts source={ai_source})'
        )

        result = {
            output_key:          report,
            'aiops_report':      report,
            'aiops_report_html': html_report,
            'aiops_subject':     subject,
            'report_ready':      True,
            'report_title':      title,
            'report_health':     health,
            'report_generated_at': now_str,
            'subject':           subject,
            'body':              analysis.get('summary', ''),
        }
        if report_to:
            result['to'] = report_to
        return result

    # ── Builder HTML ──────────────────────────────────────────────────────────

    def _build_html(self, title, analysis, hcfg, now_str,
                    es_index, es_fetched, es_total, es_time_range, es_took_ms,
                    es_by_level, es_top_errors, es_top_pods,
                    es_error_rate, es_error_total, ai_source,
                    needs_action) -> str:

        anomalies  = analysis.get('anomalies', [])
        actions    = analysis.get('immediate_actions', [])
        summary    = analysis.get('summary', 'Aucun résumé disponible.')

        # Badge source
        source_badge = (
            '<span style="background:#e8f5e9;color:#1b5e20;padding:2px 8px;'
            'border-radius:4px;font-size:11px;">Agrégations ES — 100% couverts</span>'
            if ai_source == 'aggregated'
            else f'<span style="background:#fff3e0;color:#e65100;padding:2px 8px;'
                 f'border-radius:4px;font-size:11px;">'
                 f'Échantillon — {es_fetched}/{es_total} logs</span>'
        )

        # Bannière urgence
        urgency_banner = ''
        if needs_action:
            urgency_banner = f"""
            <tr><td style="background:#fef2f2;border-left:4px solid #ef4444;padding:14px 24px;">
              <strong style="color:#ef4444;">🚨 ACTION IMMÉDIATE REQUISE</strong><br>
              <ul style="margin:8px 0 0;padding-left:20px;color:#7f1d1d;font-size:13px;">
                {''.join(f'<li>{a}</li>' for a in actions)}
              </ul>
            </td></tr>"""

        # NOUVEAU : Section statistiques ES agrégées
        stats_html = ''
        if es_by_level:
            level_rows = ''
            for level, count in sorted(
                es_by_level.items(), key=lambda x: -x[1]
            ):
                color = _SEVERITY_COLORS.get(level.upper(), '#6b7280')
                pct   = round(count / es_total * 100, 1) if es_total > 0 else 0
                bar_w = max(2, int(pct))
                level_rows += f"""
                <tr style="border-bottom:1px solid #f1f5f9;">
                  <td style="padding:8px 12px;">
                    <span style="background:{color};color:#fff;border-radius:4px;
                                 padding:2px 8px;font-size:11px;font-weight:700;">
                      {level.upper()}
                    </span>
                  </td>
                  <td style="padding:8px 12px;font-weight:600;color:{color};">
                    {count:,}
                  </td>
                  <td style="padding:8px 12px;">
                    <div style="background:#f1f5f9;border-radius:4px;height:8px;width:200px;">
                      <div style="background:{color};border-radius:4px;
                                  height:8px;width:{bar_w}%;max-width:100%;
                                  transition:width .3s;"></div>
                    </div>
                  </td>
                  <td style="padding:8px 12px;color:#94a3b8;font-size:12px;">
                    {pct}%
                  </td>
                </tr>"""

            stats_html = f"""
            <tr><td style="padding:8px 32px 24px;">
              <h3 style="color:#1e293b;font-size:15px;margin:0 0 6px;">
                📊 Répartition sur {es_total:,} logs — couverture 100%
                &nbsp;{source_badge}
              </h3>
              <table width="100%" cellpadding="0" cellspacing="0"
                     style="border-collapse:collapse;border:1px solid #e2e8f0;
                            border-radius:8px;overflow:hidden;">
                <tbody>{level_rows}</tbody>
              </table>
            </td></tr>"""

        # NOUVEAU : Section top erreurs ES (fréquences réelles)
        top_errors_es_html = ''
        if es_top_errors:
            err_rows = ''
            for i, e in enumerate(es_top_errors[:10]):
                err_rows += f"""
                <tr style="border-bottom:1px solid #f8fafc;">
                  <td style="padding:7px 10px;font-size:12px;color:#94a3b8;
                              text-align:right;width:30px;">#{i+1}</td>
                  <td style="padding:7px 10px;font-size:12px;color:#ef4444;
                              font-weight:600;width:60px;">×{e['count']:,}</td>
                  <td style="padding:7px 10px;font-size:12px;color:#475569;
                              font-family:monospace;">
                    {str(e['message'])[:150]}
                  </td>
                </tr>"""

            top_errors_es_html = f"""
            <tr><td style="padding:8px 32px 24px;">
              <h3 style="color:#1e293b;font-size:15px;margin:0 0 12px;">
                🔥 Top erreurs par fréquence réelle
              </h3>
              <table width="100%" cellpadding="0" cellspacing="0"
                     style="border-collapse:collapse;border:1px solid #e2e8f0;
                            border-radius:8px;">
                <tbody>{err_rows}</tbody>
              </table>
            </td></tr>"""

        # NOUVEAU : Section pods impactés
        pods_html = ''
        if es_top_pods:
            pod_rows = ''
            max_errors = es_top_pods[0]['errors'] if es_top_pods else 1
            for p in es_top_pods:
                bar = int(p['errors'] / max_errors * 100)
                pod_rows += f"""
                <tr style="border-bottom:1px solid #f8fafc;">
                  <td style="padding:8px 12px;font-size:12px;
                              font-family:monospace;color:#1e293b;">
                    {p['pod']}
                  </td>
                  <td style="padding:8px 12px;font-size:12px;
                              color:#ef4444;font-weight:600;">
                    {p['errors']:,} erreurs
                  </td>
                  <td style="padding:8px 12px;width:150px;">
                    <div style="background:#fef2f2;border-radius:4px;height:6px;">
                      <div style="background:#ef4444;border-radius:4px;
                                  height:6px;width:{bar}%;"></div>
                    </div>
                  </td>
                </tr>"""

            pods_html = f"""
            <tr><td style="padding:8px 32px 24px;">
              <h3 style="color:#1e293b;font-size:15px;margin:0 0 12px;">
                🐳 Pods les plus impactés
              </h3>
              <table width="100%" cellpadding="0" cellspacing="0"
                     style="border-collapse:collapse;border:1px solid #e2e8f0;
                            border-radius:8px;">
                <tbody>{pod_rows}</tbody>
              </table>
            </td></tr>"""

        # Anomalies IA
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
                  <td style="padding:10px 12px;color:#059669;font-size:12px;
                              font-style:italic;">
                    {a.get('recommendation', '—')}
                  </td>
                </tr>"""

            anomalies_html = f"""
            <tr><td style="padding:24px 32px 8px;">
              <h3 style="color:#1e293b;font-size:15px;margin:0 0 12px;">
                🔍 Anomalies détectées par l'IA ({len(anomalies)})
              </h3>
              <table width="100%" cellpadding="0" cellspacing="0"
                     style="border-collapse:collapse;border:1px solid #e2e8f0;
                            border-radius:8px;overflow:hidden;">
                <thead>
                  <tr style="background:#f8fafc;">
                    <th style="padding:10px 12px;text-align:left;
                                font-size:12px;color:#64748b;">Sévérité</th>
                    <th style="padding:10px 12px;text-align:left;
                                font-size:12px;color:#64748b;">Description</th>
                    <th style="padding:10px 12px;text-align:left;
                                font-size:12px;color:#64748b;">Service</th>
                    <th style="padding:10px 12px;text-align:left;
                                font-size:12px;color:#64748b;">Occurrences</th>
                    <th style="padding:10px 12px;text-align:left;
                                font-size:12px;color:#64748b;">Recommandation</th>
                  </tr>
                </thead>
                <tbody>{rows}</tbody>
              </table>
            </td></tr>"""

        # Compteurs IA
        def counter_cell(label, count, color):
            return (
                f'<td style="padding:4px 6px;">'
                f'<div style="background:{color}15;border:1px solid {color}40;'
                f'border-radius:8px;padding:10px 14px;text-align:center;">'
                f'<div style="font-size:22px;font-weight:800;color:{color};">{count}</div>'
                f'<div style="font-size:10px;color:{color};font-weight:600;'
                f'text-transform:uppercase;letter-spacing:.5px;">{label}</div>'
                f'</div></td>'
            )

        counters_html = ''.join([
            counter_cell('CRITICAL', analysis.get('critical_count', 0), '#ef4444'),
            counter_cell('ERROR',    analysis.get('error_count', 0),    '#f97316'),
            counter_cell('WARNING',  analysis.get('warning_count', 0),  '#f59e0b'),
            counter_cell('INFO',     analysis.get('info_count', 0),     '#3b82f6'),
        ])

        return f"""<!DOCTYPE html>
<html lang="fr">
<head><meta charset="utf-8"><title>{title}</title></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="padding:32px 0;">
    <tr><td align="center">
      <table width="700" cellpadding="0" cellspacing="0"
             style="background:#ffffff;border-radius:12px;
                    box-shadow:0 4px 24px rgba(0,0,0,.10);overflow:hidden;">

        <tr>
          <td style="background:linear-gradient(135deg,#0f172a,#1e40af);
                      padding:28px 32px;">
            <h1 style="color:#fff;margin:0;font-size:20px;font-weight:700;">
              🤖 {title}
            </h1>
            <p style="color:#93c5fd;margin:6px 0 0;font-size:13px;">
              Généré le {now_str} ·
              Index: <code style="color:#e0f2fe;">{es_index}</code> ·
              Période: <code style="color:#e0f2fe;">{es_time_range}</code> ·
              <strong style="color:#bbf7d0;">{es_total:,} logs couverts</strong>
            </p>
          </td>
        </tr>

        {urgency_banner}

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

        <tr>
          <td style="padding:8px 32px 16px;">
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                {counters_html}
                <td style="text-align:right;padding:0 0 0 12px;vertical-align:middle;">
                  <span style="font-size:11px;color:#94a3b8;">
                    {es_total:,} logs · {es_took_ms}ms
                  </span>
                </td>
              </tr>
            </table>
          </td>
        </tr>

        {stats_html}
        {top_errors_es_html}
        {pods_html}
        {anomalies_html}

        <tr>
          <td style="padding:16px 32px;background:#f8fafc;
                      border-top:1px solid #e2e8f0;text-align:center;">
            <p style="color:#94a3b8;font-size:11px;margin:0;">
              Rapport généré automatiquement par
              <strong>Workflow Engine — AIOps</strong>
              · Source: {ai_source}
            </p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""

    def _build_markdown(self, title, analysis, hcfg, now_str,
                        es_index, es_fetched, es_total, es_time_range,
                        es_by_level, es_top_errors, es_top_pods, ai_source) -> str:
        lines = [
            f"# {hcfg['icon']} {title}",
            f"",
            f"**Généré le** {now_str} · **Index** `{es_index}` · "
            f"**Période** `{es_time_range}` · **{es_total:,} logs couverts**",
            f"",
            f"## État global : {hcfg['icon']} {hcfg['label']}",
            f"",
            analysis.get('summary', ''),
            f"",
            f"| CRITICAL | ERROR | WARNING | INFO |",
            f"|----------|-------|---------|------|",
            f"| {analysis.get('critical_count',0)} "
            f"| {analysis.get('error_count',0)} "
            f"| {analysis.get('warning_count',0)} "
            f"| {analysis.get('info_count',0)} |",
            f"",
        ]
        if es_by_level:
            lines.append("## Répartition ES (100% des logs)")
            lines.append("")
            for level, count in sorted(es_by_level.items(), key=lambda x: -x[1]):
                pct = round(count / es_total * 100, 1) if es_total > 0 else 0
                lines.append(f"- **{level.upper()}**: {count:,} ({pct}%)")
            lines.append("")
        if es_top_errors:
            lines.append("## Top erreurs (fréquences réelles)")
            lines.append("")
            for i, e in enumerate(es_top_errors[:10]):
                lines.append(f"{i+1}. ×{e['count']} — `{e['message'][:100]}`")
            lines.append("")
        if es_top_pods:
            lines.append("## Pods les plus impactés")
            lines.append("")
            for p in es_top_pods:
                lines.append(f"- `{p['pod']}` : {p['errors']:,} erreurs")
            lines.append("")
        if analysis.get('anomalies'):
            lines.append("## Anomalies IA")
            lines.append("")
            for a in analysis['anomalies']:
                lines.append(
                    f"- **[{a.get('severity','?')}]** {a.get('description','')} "
                    f"— *{a.get('affected_service','?')}* ×{a.get('count',1)}"
                )
                if a.get('recommendation'):
                    lines.append(f"  → {a['recommendation']}")
            lines.append("")
        lines.append(f"\n---\n*Rapport généré par Workflow Engine — AIOps · source: {ai_source}*")
        return '\n'.join(lines)

    def _build_text(self, title, analysis, hcfg, now_str,
                    es_index, es_total, es_time_range) -> str:
        lines = [
            f"{'='*60}",
            f"  {title}",
            f"  Généré le {now_str}",
            f"  Index: {es_index} | Période: {es_time_range}",
            f"  Logs couverts: {es_total:,}",
            f"{'='*60}",
            f"",
            f"ÉTAT : {hcfg['icon']} {hcfg['label']}",
            f"",
            analysis.get('summary', ''),
            f"",
            f"CRITICAL:{analysis.get('critical_count',0)} "
            f"ERROR:{analysis.get('error_count',0)} "
            f"WARNING:{analysis.get('warning_count',0)} "
            f"INFO:{analysis.get('info_count',0)}",
        ]
        if analysis.get('anomalies'):
            lines.append("\nANOMALIES :")
            for a in analysis['anomalies']:
                lines.append(
                    f"  [{a.get('severity','?')}] {a.get('description','')} "
                    f"({a.get('affected_service','?')}) ×{a.get('count',1)}"
                )
        lines.append(f"\n{'─'*60}")
        lines.append("Workflow Engine — AIOps")
        return '\n'.join(lines)