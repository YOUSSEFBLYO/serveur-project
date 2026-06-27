"""
AIOps — Elasticsearch Log Fetcher avec Agrégations
nœud aiops.ElasticsearchFetch

CHANGEMENTS vs version originale :
  - Ajout du bloc "aggs" dans la requête DSL
  - Extraction des agrégations dans la réponse
  - Retour de es_aggregated, es_by_level, es_top_errors, es_top_pods
  - max_hits réduit à 5 (exemples seulement, plus d'analyse brute)
"""
import base64
import json
import logging
import ssl
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

from .base import BaseExecutor

logger = logging.getLogger(__name__)


def _resolve_vars(text: str, context: dict) -> str:
    if not text:
        return text or ''
    for key, value in context.items():
        text = text.replace(f'{{{{{key}}}}}', str(value))
    return text


class ElasticsearchFetchExecutor(BaseExecutor):
    """
    Récupère des statistiques agrégées depuis Elasticsearch.

    NOUVEAU : utilise les agrégations ES pour couvrir 100% des logs
    sans les transférer physiquement. Retourne :
      - es_aggregated    : dict  → résumé statistique complet (pour LogClassifier)
      - es_by_level      : dict  → comptage par niveau {ERROR: 47, INFO: 183191, ...}
      - es_top_errors    : list  → top messages d'erreur avec fréquence
      - es_top_pods      : list  → pods les plus impactés
      - es_logs          : list  → 5 exemples de logs bruts (pour contexte)
      - es_total         : int   → total de hits dans ES
      - es_fetched       : int   → nombre d'exemples retournés (toujours 5)
      - es_took_ms       : int   → temps de réponse ES
    """

    def run(self) -> dict:
        # ── Paramètres ────────────────────────────────────────────────────────
        es_url = _resolve_vars(
            (self.cfg('es_url', '') or '').strip().rstrip('/'),
            self.context,
        )
        if not es_url:
            raise RuntimeError(
                "[ElasticsearchFetch] Le champ 'es_url' est requis."
            )

        index_pattern   = _resolve_vars(
            (self.cfg('index_pattern', 'app-logs-*') or 'app-logs-*').strip(),
            self.context,
        )
        time_field      = (self.cfg('time_field',    '@timestamp') or '@timestamp').strip()
        time_range      = (self.cfg('time_range',    'now-15m')    or 'now-15m').strip()
        output_key      = (self.cfg('output_key',    'es_logs')    or 'es_logs').strip()
        log_level_field = (self.cfg('log_level_field', 'level')    or 'level').strip()
        message_field   = (self.cfg('message_field',   'message')  or 'message').strip()

        # CHANGEMENT : max_hits = 5 exemples seulement
        # Les vraies stats viennent des agrégations
        max_hits = 5

        # ── Construction de la requête avec AGRÉGATIONS ───────────────────────
        query = {
            "bool": {
                "must": [{
                    "range": {
                        time_field: {
                            "gte": time_range,
                            "lte": "now"
                        }
                    }
                }]
            }
        }

        # Filtre additionnel optionnel
        query_filter_raw = _resolve_vars(
            (self.cfg('query_filter', '') or '').strip(),
            self.context,
        )
        if query_filter_raw:
            try:
                extra_filter = json.loads(query_filter_raw)
                query["bool"].setdefault("filter", []).append(extra_filter)
            except (json.JSONDecodeError, ValueError):
                logger.warning('[ElasticsearchFetch] query_filter JSON invalide — ignoré')

        # ── NOUVEAU : bloc agrégations ────────────────────────────────────────
        aggs = {

            # 1. Comptage par niveau de log
            "by_level": {
                "terms": {
                    "field": f"{log_level_field}.keyword",
                    "size":  10
                }
            },

            # 2. Top messages d'erreur les plus fréquents
            "top_errors": {
                "filter": {
                    "terms": {
                        f"{log_level_field}.keyword": [
                            "ERROR", "CRITICAL", "FATAL", "error", "critical"
                        ]
                    }
                },
                "aggs": {
                    "messages": {
                        "terms": {
                            "field": f"{message_field}.keyword",
                            "size":  15,
                            "order": {"_count": "desc"}
                        }
                    },
                    # 3. Pods les plus impactés (dans les erreurs)
                    "pods": {
                        "terms": {
                            "field": "kubernetes.pod_name.keyword",
                            "size":  5,
                            "order": {"_count": "desc"}
                        }
                    }
                }
            },

            # 4. Évolution temporelle des erreurs (par minute)
            "error_rate_over_time": {
                "filter": {
                    "terms": {
                        f"{log_level_field}.keyword": [
                            "ERROR", "CRITICAL", "FATAL"
                        ]
                    }
                },
                "aggs": {
                    "per_minute": {
                        "date_histogram": {
                            "field":          time_field,
                            "fixed_interval": "1m",
                            "min_doc_count":  0
                        }
                    }
                }
            },

            # 5. Types d'erreurs structurées (champ structured.error.type)
            "structured_error_types": {
                "filter": {
                    "exists": {"field": "structured.error.type"}
                },
                "aggs": {
                    "types": {
                        "terms": {
                            "field": "structured.error.type.keyword",
                            "size":  10
                        }
                    }
                }
            }
        }

        body = json.dumps({
            "query":  query,
            "size":   max_hits,
            "sort":   [{time_field: {"order": "desc"}}],
            "_source": [
                time_field,
                log_level_field,
                message_field,
                "kubernetes.pod_name",
                "kubernetes.namespace_name",
                "structured.error.type",
                "structured.error.reason",
                "hostname",
                "topic",
            ],
            "aggs": aggs   # ← NOUVEAU
        }).encode('utf-8')

        # ── URL et headers ────────────────────────────────────────────────────
        search_url = f"{es_url}/{index_pattern}/_search"
        logger.info(
            f'[ElasticsearchFetch] → {search_url} '
            f'time_range={time_range} (agrégations activées)'
        )

        headers = {
            'Content-Type': 'application/json',
            'Accept':       'application/json',
        }
        auth_type = (self.cfg('auth_type', 'Aucune') or 'Aucune').strip()
        headers   = self._apply_auth(headers, auth_type)

        # ── Appel HTTP ────────────────────────────────────────────────────────
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode    = ssl.CERT_NONE

        try:
            req = Request(search_url, data=body, method='POST', headers=headers)
            with urlopen(req, timeout=30, context=ssl_ctx) as resp:
                es_response = json.loads(resp.read().decode('utf-8', errors='replace'))

        except HTTPError as exc:
            err_body = exc.read().decode('utf-8', errors='replace')[:500]
            raise RuntimeError(
                f"[ElasticsearchFetch] HTTP {exc.code} — {exc.reason}\n"
                f"URL: {search_url}\nRéponse ES: {err_body}"
            )
        except URLError as exc:
            raise RuntimeError(
                f"[ElasticsearchFetch] Impossible de joindre ES : {exc}\n"
                f"URL: {search_url}"
            )

        # ── Extraction des hits (exemples) ────────────────────────────────────
        hits_raw    = es_response.get('hits', {})
        total_val   = hits_raw.get('total', {})
        total_count = (
            total_val.get('value', 0)
            if isinstance(total_val, dict)
            else int(total_val or 0)
        )
        took_ms = es_response.get('took', 0)
        docs    = [h.get('_source', h) for h in hits_raw.get('hits', [])]

        simplified_logs = []
        for doc in docs:
            simplified_logs.append({
                'timestamp': doc.get(time_field, ''),
                'level':     doc.get(log_level_field, 'unknown'),
                'message':   doc.get(message_field, ''),
                'pod':       doc.get('kubernetes', {}).get('pod_name', ''),
                'namespace': doc.get('kubernetes', {}).get('namespace_name', ''),
                'error_type':   doc.get('structured', {}).get('error', {}).get('type', ''),
                'error_reason': doc.get('structured', {}).get('error', {}).get('reason', ''),
                'hostname':  doc.get('hostname', ''),
                'topic':     doc.get('topic', ''),
            })

        # ── NOUVEAU : Extraction des agrégations ──────────────────────────────
        raw_aggs = es_response.get('aggregations', {})

        # 1. Comptage par niveau
        by_level = {
            b["key"]: b["doc_count"]
            for b in raw_aggs.get("by_level", {}).get("buckets", [])
        }

        # 2. Top messages d'erreur
        top_errors = [
            {
                "message": b["key"],
                "count":   b["doc_count"]
            }
            for b in raw_aggs.get("top_errors", {})
                              .get("messages", {})
                              .get("buckets", [])
        ]

        # 3. Pods les plus impactés
        top_pods = [
            {
                "pod":    b["key"],
                "errors": b["doc_count"]
            }
            for b in raw_aggs.get("top_errors", {})
                              .get("pods", {})
                              .get("buckets", [])
        ]

        # 4. Évolution temporelle
        error_rate = [
            {
                "minute": b["key_as_string"],
                "errors": b["doc_count"]
            }
            for b in raw_aggs.get("error_rate_over_time", {})
                              .get("per_minute", {})
                              .get("buckets", [])
            if b["doc_count"] > 0
        ]

        # 5. Types d'erreurs structurées
        structured_types = [
            {
                "type":  b["key"],
                "count": b["doc_count"]
            }
            for b in raw_aggs.get("structured_error_types", {})
                              .get("types", {})
                              .get("buckets", [])
        ]

        # 6. Résumé agrégé complet (ce qui sera envoyé à LogClassifier)
        error_total   = sum(
            c for k, c in by_level.items()
            if k.upper() in ('ERROR', 'CRITICAL', 'FATAL')
        )
        warning_total = sum(
            c for k, c in by_level.items()
            if k.upper() in ('WARN', 'WARNING')
        )

        aggregated_summary = {
            "total_logs":          total_count,
            "period":              time_range,
            "index":               index_pattern,
            "query_took_ms":       took_ms,
            "by_level":            by_level,
            "error_total":         error_total,
            "warning_total":       warning_total,
            "top_error_messages":  top_errors,
            "most_affected_pods":  top_pods,
            "error_rate_per_min":  error_rate,
            "structured_error_types": structured_types,
            "sample_logs":         simplified_logs,
        }

        logger.info(
            f'[ElasticsearchFetch] ✅ Agrégations OK — '
            f'total={total_count} | errors={error_total} | '
            f'warnings={warning_total} | took={took_ms}ms'
        )

        return {
            output_key:             simplified_logs,
            'es_logs':              simplified_logs,
            'es_aggregated':        aggregated_summary,   # ← NOUVEAU
            'es_by_level':          by_level,             # ← NOUVEAU
            'es_top_errors':        top_errors,           # ← NOUVEAU
            'es_top_pods':          top_pods,             # ← NOUVEAU
            'es_error_rate':        error_rate,           # ← NOUVEAU
            'es_structured_types':  structured_types,     # ← NOUVEAU
            'es_total':             total_count,
            'es_fetched':           len(docs),
            'es_error_total':       error_total,
            'es_warning_total':     warning_total,
            'es_index':             index_pattern,
            'es_took_ms':           took_ms,
            'es_time_range':        time_range,
            'es_url':               es_url,
        }

    def _apply_auth(self, headers: dict, auth_type: str) -> dict:
        if auth_type == 'Basic Auth':
            username = _resolve_vars(
                (self.cfg('auth_username', '') or '').strip(), self.context
            )
            password = _resolve_vars(
                (self.cfg('auth_password', '') or '').strip(), self.context
            )
            if not username:
                raise RuntimeError(
                    "[ElasticsearchFetch] auth_username vide."
                )
            creds = base64.b64encode(f'{username}:{password}'.encode()).decode()
            headers['Authorization'] = f'Basic {creds}'

        elif auth_type == 'API Key':
            api_key = _resolve_vars(
                (self.cfg('auth_api_key', '') or '').strip(), self.context
            )
            if not api_key:
                raise RuntimeError(
                    "[ElasticsearchFetch] auth_api_key vide."
                )
            headers['Authorization'] = f'ApiKey {api_key}'

        return headers