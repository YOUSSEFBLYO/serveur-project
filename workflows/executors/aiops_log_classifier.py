"""
AIOps — Log Classifier avec agrégations
nœud aiops.LogClassifier

CHANGEMENTS vs version originale :
  - Lit es_aggregated depuis le contexte (priorité sur es_logs bruts)
  - Prompt enrichi avec les statistiques agrégées (100% de couverture)
  - Fallback sur logs bruts si pas d'agrégation disponible
  - user_message construit depuis le résumé agrégé
"""
import json
import logging
import re

import openai

from .base import BaseExecutor

logger = logging.getLogger(__name__)


def _resolve_vars(text: str, context: dict) -> str:
    if not text:
        return text or ''
    for key, value in context.items():
        text = text.replace(f'{{{{{key}}}}}', str(value))
    return text


# ── Prompt système FR ─────────────────────────────────────────────────────────
_SYSTEM_PROMPT_FR = """Tu es un expert en analyse de logs applicatifs et en opérations (AIOps).

Ta tâche est d'analyser les statistiques de logs fournies et de répondre
UNIQUEMENT avec un objet JSON valide ayant EXACTEMENT cette structure
(sans markdown, sans backticks, sans explication) :

{
  "summary": "Résumé en 2-3 phrases de l'état général du système",
  "critical_count": 0,
  "error_count": 0,
  "warning_count": 0,
  "info_count": 0,
  "anomalies": [
    {
      "severity": "CRITICAL|ERROR|WARNING",
      "description": "Description de l'anomalie détectée",
      "affected_service": "Nom du service concerné",
      "first_occurrence": "timestamp ISO ou période",
      "count": 1,
      "recommendation": "Action recommandée"
    }
  ],
  "top_errors": [
    {
      "message": "Message d'erreur représentatif",
      "count": 1,
      "level": "ERROR|CRITICAL"
    }
  ],
  "overall_health": "HEALTHY|DEGRADED|CRITICAL",
  "needs_immediate_action": false,
  "immediate_actions": ["Action 1 si urgent", "Action 2"]
}

Règles de classification de la sévérité :
- CRITICAL : overall_health=CRITICAL si critical_count > 0 ou error_count > 50
- DEGRADED : overall_health=DEGRADED si error_count > 0 ou warning_count > 20
- HEALTHY  : overall_health=HEALTHY sinon
- needs_immediate_action=true seulement si CRITICAL
"""

_SYSTEM_PROMPT_EN = """You are an expert in application log analysis and AIOps.

Analyze the provided log statistics and respond ONLY with a valid JSON object
having EXACTLY this structure (no markdown, no backticks, no explanation):

{
  "summary": "2-3 sentence summary of the system's overall state",
  "critical_count": 0,
  "error_count": 0,
  "warning_count": 0,
  "info_count": 0,
  "anomalies": [
    {
      "severity": "CRITICAL|ERROR|WARNING",
      "description": "Description of the detected anomaly",
      "affected_service": "Service name",
      "first_occurrence": "ISO timestamp or period",
      "count": 1,
      "recommendation": "Recommended action"
    }
  ],
  "top_errors": [
    {
      "message": "Representative error message",
      "count": 1,
      "level": "ERROR|CRITICAL"
    }
  ],
  "overall_health": "HEALTHY|DEGRADED|CRITICAL",
  "needs_immediate_action": false,
  "immediate_actions": ["Action 1 if urgent"]
}
"""


class LogClassifierExecutor(BaseExecutor):
    """
    Agent IA de classification de logs.

    NOUVEAU : utilise en priorité es_aggregated (résumé statistique
    couvrant 100% des logs) au lieu des logs bruts limités à max_logs.
    Fallback sur les logs bruts si pas d'agrégation disponible.
    """

    def run(self) -> dict:
        # ── Paramètres ────────────────────────────────────────────────────────
        ai_model      = (self.cfg('ai_model',     '')        or '').strip()
        api_key       = _resolve_vars((self.cfg('api_key',      '') or '').strip(), self.context)
        api_base_url  = _resolve_vars((self.cfg('api_base_url', '') or '').strip(), self.context)
        input_key     = (self.cfg('input_key',  'es_logs')   or 'es_logs').strip()
        max_logs      = int(self.cfg('max_logs', 50)         or 50)
        language      = (self.cfg('language',   'fr')        or 'fr').strip().lower()
        output_key    = (self.cfg('output_key', 'ai_analysis') or 'ai_analysis').strip()
        custom_prompt = (self.cfg('custom_prompt', '')       or '').strip()

        # ── NOUVEAU : Priorité aux agrégations ────────────────────────────────
        aggregated = self.context.get('es_aggregated')
        logs       = self.context.get(input_key, [])

        if aggregated:
            # Cas 1 : agrégations disponibles → couverture 100% des logs
            user_message, logs_analyzed = self._build_message_from_aggs(aggregated)
            source = 'aggregated'
            logger.info(
                f'[LogClassifier] Mode agrégations — '
                f'{aggregated.get("total_logs", 0)} logs couverts via stats ES'
            )

        elif logs:
            # Cas 2 : fallback sur logs bruts (ancienne méthode)
            logs_to_analyze = logs[:max_logs]
            logs_json       = json.dumps(logs_to_analyze, ensure_ascii=False)
            user_message    = (
                f"Voici {len(logs_to_analyze)} logs bruts à analyser :\n"
                f"{logs_json}"
            )
            logs_analyzed = len(logs_to_analyze)
            source = 'raw_logs'
            logger.warning(
                '[LogClassifier] Fallback logs bruts — '
                'pas d\'agrégation disponible (couverture partielle)'
            )

        else:
            logger.warning('[LogClassifier] Aucune donnée à analyser')
            return self._empty_result(output_key)

        # ── Prompt système ────────────────────────────────────────────────────
        system_prompt = _SYSTEM_PROMPT_EN if language == 'en' else _SYSTEM_PROMPT_FR
        if custom_prompt:
            system_prompt += f"\n\nContexte métier spécifique :\n{custom_prompt}"

        logger.info(
            f'[LogClassifier] Appel LiteLLM — '
            f'model={ai_model} source={source}'
        )

        # ── Appel LiteLLM ─────────────────────────────────────────────────────
        raw_response = self._call_litellm(
            api_key       = api_key,
            api_base_url  = api_base_url,
            model         = ai_model or 'gpt-4o-mini',
            system_prompt = system_prompt,
            user_message  = user_message,
        )

        analysis = self._parse_ai_response(raw_response)

        logger.info(
            f'[LogClassifier] ✅ Analyse terminée — '
            f'health={analysis.get("overall_health")} '
            f'errors={analysis.get("error_count")} '
            f'source={source}'
        )

        return {
            output_key:          analysis,
            'ai_analysis':       analysis,
            'ai_logs_analyzed':  logs_analyzed,
            'ai_source':         source,            # ← NOUVEAU : trace la source
            'ai_model':          ai_model,
            'ai_overall_health': analysis.get('overall_health', 'UNKNOWN'),
            'ai_needs_action':   analysis.get('needs_immediate_action', False),
        }

    # ── NOUVEAU : Construction du message depuis les agrégations ──────────────

    def _build_message_from_aggs(self, agg: dict) -> tuple:
        """
        Construit le message utilisateur depuis le résumé agrégé ES.
        Couvre 100% des logs en n'envoyant que les statistiques.

        Returns:
            (user_message: str, logs_analyzed: int)
        """
        total      = agg.get('total_logs', 0)
        period     = agg.get('period', '?')
        index      = agg.get('index', '?')
        by_level   = agg.get('by_level', {})
        top_errors = agg.get('top_error_messages', [])
        top_pods   = agg.get('most_affected_pods', [])
        error_rate = agg.get('error_rate_per_min', [])
        struct_types = agg.get('structured_error_types', [])
        samples    = agg.get('sample_logs', [])

        # Formatage compact des niveaux
        levels_str = ' | '.join(
            f"{k}: {v}"
            for k, v in sorted(by_level.items(), key=lambda x: -x[1])
        )

        # Formatage des top erreurs
        errors_str = '\n'.join(
            f"  [{i+1}] ×{e['count']} — {e['message'][:120]}"
            for i, e in enumerate(top_errors[:10])
        ) or '  Aucune erreur détectée'

        # Formatage des pods impactés
        pods_str = '\n'.join(
            f"  - {p['pod']}: {p['errors']} erreurs"
            for p in top_pods[:5]
        ) or '  Aucun pod identifié'

        # Formatage des types d'erreurs structurées
        struct_str = '\n'.join(
            f"  - {t['type']}: ×{t['count']}"
            for t in struct_types[:5]
        ) or '  Aucun type structuré disponible'

        # Pics d'erreurs (minutes avec le plus d'erreurs)
        peaks = sorted(error_rate, key=lambda x: -x['errors'])[:3]
        peaks_str = ', '.join(
            f"{p['minute']} (×{p['errors']})"
            for p in peaks
        ) or 'Aucun pic détecté'

        # Exemples de logs bruts
        samples_str = '\n'.join(
            f"  [{s.get('level','?')}] {s.get('timestamp','')[:19]} "
            f"pod={s.get('pod','?')} — {s.get('message','')[:100]}"
            for s in samples[:3]
        ) or '  Aucun exemple disponible'

        # Message final
        message = f"""Analyse ce résumé statistique de logs Elasticsearch.

CONTEXTE :
  Index   : {index}
  Période : {period}
  Total   : {total:,} logs analysés (couverture 100%)

RÉPARTITION PAR NIVEAU :
  {levels_str}

TOP 10 MESSAGES D'ERREUR (fréquence) :
{errors_str}

PODS LES PLUS IMPACTÉS :
{pods_str}

TYPES D'ERREURS STRUCTURÉES (champ structured.error.type) :
{struct_str}

PICS D'ERREURS (minutes les plus chargées) :
  {peaks_str}

EXEMPLES DE LOGS BRUTS (3 derniers) :
{samples_str}
"""
        return message, total

    # ── Appel LiteLLM ─────────────────────────────────────────────────────────

    def _call_litellm(self, api_key, api_base_url, model,
                      system_prompt, user_message) -> str:

        if not api_key:
            raise RuntimeError("[LogClassifier] 'api_key' vide.")
        if not api_base_url:
            raise RuntimeError("[LogClassifier] 'api_base_url' vide.")

        base_url = api_base_url.rstrip('/')
        logger.info(f'[LogClassifier] LiteLLM → {base_url} model={model}')

        import os
        from urllib.parse import urlparse

        proxy_url = None
        try:
            from decouple import config as dconfig
            proxy_url = dconfig('PROXY_URL', default=None)
        except Exception:
            pass

        proxy_url = (
            proxy_url
            or os.environ.get('https_proxy')
            or os.environ.get('HTTPS_PROXY')
            or os.environ.get('http_proxy')
            or os.environ.get('HTTP_PROXY')
        )

        hostname = urlparse(base_url).hostname or ''
        bypass   = hostname in ('localhost', '127.0.0.1', '::1') \
                   or hostname.endswith('.attijariwafa.net')

        import httpx
        client_kwargs = {}
        if proxy_url and not bypass:
            if '://' not in proxy_url:
                proxy_url = f'http://{proxy_url}'
            client_kwargs['proxy'] = proxy_url

        http_client = httpx.Client(
            verify=False, trust_env=False, **client_kwargs
        )

        client = openai.OpenAI(
            api_key     = api_key,
            base_url    = base_url,
            http_client = http_client,
        )

        try:
            response = client.chat.completions.create(
                model           = model,
                messages        = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_message},
                ],
                temperature     = 0.1,
                max_tokens      = 2000,
                response_format = {"type": "json_object"},
            )
            content = response.choices[0].message.content
            logger.info(
                f'[LogClassifier] Réponse reçue — '
                f'tokens={response.usage.total_tokens if response.usage else "?"}'
            )
            return content

        except openai.AuthenticationError as exc:
            raise RuntimeError(f"[LogClassifier] Auth LiteLLM : {exc}")
        except openai.APIConnectionError as exc:
            raise RuntimeError(
                f"[LogClassifier] Connexion LiteLLM impossible : {exc}\n"
                f"URL: {base_url}"
            )
        except openai.APIStatusError as exc:
            raise RuntimeError(
                f"[LogClassifier] LiteLLM HTTP {exc.status_code}: {exc.message}"
            )
        except Exception as exc:
            raise RuntimeError(
                f"[LogClassifier] Erreur inattendue : {type(exc).__name__}: {exc}"
            )

    # ── Parsing de la réponse ─────────────────────────────────────────────────

    def _parse_ai_response(self, raw: str) -> dict:
        text = raw.strip()
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$',          '', text)

        try:
            result = json.loads(text)
            result.setdefault('summary',                'Analyse complétée.')
            result.setdefault('overall_health',         'UNKNOWN')
            result.setdefault('critical_count',         0)
            result.setdefault('error_count',            0)
            result.setdefault('warning_count',          0)
            result.setdefault('info_count',             0)
            result.setdefault('anomalies',              [])
            result.setdefault('top_errors',             [])
            result.setdefault('needs_immediate_action', False)
            result.setdefault('immediate_actions',      [])
            return result

        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(f'[LogClassifier] Parsing de secours : {exc}')
            return {
                'summary':                raw[:500],
                'overall_health':         'UNKNOWN',
                'critical_count':         0,
                'error_count':            0,
                'warning_count':          0,
                'info_count':             0,
                'anomalies':              [],
                'top_errors':             [],
                'needs_immediate_action': False,
                'immediate_actions':      [],
                '_parse_error':           str(exc),
            }

    def _empty_result(self, output_key: str) -> dict:
        empty = {
            'summary':                'Aucun log à analyser.',
            'overall_health':         'HEALTHY',
            'critical_count':         0,
            'error_count':            0,
            'warning_count':          0,
            'info_count':             0,
            'anomalies':              [],
            'top_errors':             [],
            'needs_immediate_action': False,
            'immediate_actions':      [],
        }
        return {
            output_key:         empty,
            'ai_analysis':      empty,
            'ai_logs_analyzed': 0,
            'ai_source':        'none',
        }