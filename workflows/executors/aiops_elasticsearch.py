"""
AIOps — Elasticsearch Log Fetcher (nœud aiops.ElasticsearchFetch)

Interroge un cluster Elasticsearch via son API REST (_search).
Retourne les logs bruts dans le contexte pour le nœud suivant (LogClassifier).

Config du nœud (canvas React Flow) :
    es_url         : str  — URL de base ES  ex: http://localhost:9200
    index_pattern  : str  — Nom de l'index  ex: app-logs-* ou logstash-2024.*
    time_field     : str  — Champ de date   ex: @timestamp  (défaut: @timestamp)
    time_range     : str  — Plage temporelle ex: now-1h, now-24h, now-7d  (défaut: now-1h)
    max_hits       : int  — Nombre max de documents à retourner  (défaut: 100)
    query_filter   : str  — Filtre DSL JSON additionnel (optionnel)
    log_level_field: str  — Champ du niveau de log  ex: level, severity (défaut: level)
    message_field  : str  — Champ du message         ex: message, msg   (défaut: message)
    auth_type      : str  — Aucune | Basic Auth | API Key
    auth_username  : str  — (Basic Auth)
    auth_password  : str  — (Basic Auth)
    auth_api_key   : str  — (API Key) → header: Authorization: ApiKey <key>
    output_key     : str  — Clé de sortie dans le contexte (défaut: es_logs)
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
    """Remplace {{key}} par la valeur du contexte d'exécution."""
    if not text:
        return text or ''
    for key, value in context.items():
        text = text.replace(f'{{{{{key}}}}}', str(value))
    return text


class ElasticsearchFetchExecutor(BaseExecutor):
    """
    Récupère des logs depuis Elasticsearch via l'API _search.

    Construit une requête DSL avec filtre temporel (range sur time_field),
    applique un filtre optionnel additionnel, et retourne :
      - es_logs         : list  → documents bruts (hits)
      - es_total        : int   → total de hits dans ES (peut dépasser max_hits)
      - es_fetched      : int   → nombre réel de docs retournés
      - es_index        : str   → index interrogé
      - es_took_ms      : int   → temps de réponse ES (ms)
      - es_time_range   : str   → plage temporelle utilisée
      - <output_key>    : list  → alias vers es_logs (pour chaîner avec LogClassifier)
    """

    def run(self) -> dict:
        # ── Paramètres de connexion ───────────────────────────────────────────
        es_url = _resolve_vars(
            (self.cfg('es_url', '') or '').strip().rstrip('/'),
            self.context,
        )
        if not es_url:
            raise RuntimeError(
                "[ElasticsearchFetch] Le champ 'es_url' est requis.\n"
                "Ex : http://localhost:9200"
            )

        index_pattern = _resolve_vars(
            (self.cfg('index_pattern', 'app-logs-*') or 'app-logs-*').strip(),
            self.context,
        )
        time_field   = (self.cfg('time_field',    '@timestamp') or '@timestamp').strip()
        time_range   = (self.cfg('time_range',    'now-1h')     or 'now-1h').strip()
        max_hits     = int(self.cfg('max_hits',   100) or 100)
        output_key   = (self.cfg('output_key',   'es_logs')    or 'es_logs').strip()
        log_level_field = (self.cfg('log_level_field', 'level') or 'level').strip()
        message_field   = (self.cfg('message_field',   'message') or 'message').strip()

        # ── Construction de la requête DSL ────────────────────────────────────
        query: dict = {
            "bool": {
                "must": [
                    {
                        "range": {
                            time_field: {
                                "gte": time_range,
                                "lte": "now"
                            }
                        }
                    }
                ]
            }
        }

        # Filtre additionnel optionnel (JSON brut)
        query_filter_raw = _resolve_vars(
            (self.cfg('query_filter', '') or '').strip(),
            self.context,
        )
        if query_filter_raw:
            try:
                extra_filter = json.loads(query_filter_raw)
                query["bool"].setdefault("filter", []).append(extra_filter)
                logger.info(f'[ElasticsearchFetch] Filtre additionnel appliqué')
            except (json.JSONDecodeError, ValueError):
                logger.warning('[ElasticsearchFetch] query_filter JSON invalide — ignoré')

        body = json.dumps({
            "query": query,
            "size": max_hits,
            "sort": [{time_field: {"order": "desc"}}],
            "_source": True,
        }).encode('utf-8')

        # ── Construction de l'URL _search ─────────────────────────────────────
        search_url = f"{es_url}/{index_pattern}/_search"
        logger.info(
            f'[ElasticsearchFetch] → {search_url}  '
            f'time_range={time_range}  max_hits={max_hits}'
        )

        # ── Headers + Auth ────────────────────────────────────────────────────
        headers = {
            'Content-Type': 'application/json',
            'Accept':       'application/json',
        }
        auth_type = (self.cfg('auth_type', 'Aucune') or 'Aucune').strip()
        headers = self._apply_auth(headers, auth_type)

        # ── Requête HTTP vers ES ──────────────────────────────────────────────
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode    = ssl.CERT_NONE

        try:
            req = Request(search_url, data=body, method='POST', headers=headers)
            with urlopen(req, timeout=30, context=ssl_ctx) as resp:
                raw = resp.read().decode('utf-8', errors='replace')
                es_response = json.loads(raw)

        except HTTPError as exc:
            err_body = exc.read().decode('utf-8', errors='replace')[:500]
            raise RuntimeError(
                f"[ElasticsearchFetch] HTTP {exc.code} — {exc.reason}\n"
                f"URL: {search_url}\nRéponse ES: {err_body}"
            )
        except URLError as exc:
            raise RuntimeError(
                f"[ElasticsearchFetch] Impossible de joindre Elasticsearch : {exc}\n"
                f"URL: {search_url}\n"
                f"Vérifiez que ES est démarré et accessible."
            )

        # ── Extraction des hits ───────────────────────────────────────────────
        hits_raw  = es_response.get('hits', {})
        total_val = hits_raw.get('total', {})
        total_count = (
            total_val.get('value', 0)
            if isinstance(total_val, dict)
            else int(total_val or 0)
        )
        took_ms = es_response.get('took', 0)
        docs    = [hit.get('_source', hit) for hit in hits_raw.get('hits', [])]

        # ── Extraction simplifiée des champs clés ─────────────────────────────
        # Produit une liste allégée pour l'agent IA (évite de surcharger le contexte)
        simplified_logs = []
        for doc in docs:
            simplified_logs.append({
                'timestamp': doc.get(time_field, ''),
                'level':     doc.get(log_level_field, 'unknown'),
                'message':   doc.get(message_field, ''),
                'service':   doc.get('service', doc.get('app', doc.get('host', 'unknown'))),
                '_raw':      doc,  # document complet disponible si besoin
            })

        logger.info(
            f'[ElasticsearchFetch] ✅ {len(docs)}/{total_count} docs récupérés '
            f'depuis "{index_pattern}" en {took_ms}ms'
        )

        return {
            output_key:      simplified_logs,
            'es_logs':       simplified_logs,
            'es_total':      total_count,
            'es_fetched':    len(docs),
            'es_index':      index_pattern,
            'es_took_ms':    took_ms,
            'es_time_range': time_range,
            'es_url':        es_url,
        }

    # ── Auth (même pattern que HttpRequestExecutor) ───────────────────────────

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
                    "[ElasticsearchFetch] auth_type=Basic Auth mais 'auth_username' est vide."
                )
            credentials = base64.b64encode(f'{username}:{password}'.encode()).decode()
            headers['Authorization'] = f'Basic {credentials}'
            logger.info(f'[ElasticsearchFetch] Auth : Basic Auth (user={username}) ✓')

        elif auth_type == 'API Key':
            api_key = _resolve_vars(
                (self.cfg('auth_api_key', '') or '').strip(), self.context
            )
            if not api_key:
                raise RuntimeError(
                    "[ElasticsearchFetch] auth_type=API Key mais 'auth_api_key' est vide."
                )
            headers['Authorization'] = f'ApiKey {api_key}'
            logger.info('[ElasticsearchFetch] Auth : API Key ✓')

        return headers
