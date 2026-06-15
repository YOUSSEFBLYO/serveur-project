"""
HTTP Request — Exécuteur unifié (nœud 2/7).

Nœud générique pour tout appel REST. Authentification intégrée :
  - Aucune        → pas d'Authorization header
  - Bearer Token  → Authorization: Bearer <token>
  - Basic Auth    → Authorization: Basic <base64(user:pass)>
  - API Key       → <header_name>: <api_key>

Supporte {{variable}} dans url, body, headers et valeurs d'auth.
"""
import base64
import json
import logging
import random
import ssl
import time
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

from .base import BaseExecutor

logger = logging.getLogger(__name__)


def _resolve_vars(text: str, context: dict) -> str:
    """Remplace chaque occurrence de {{key}} dans text par sa valeur dans context."""
    if not text:
        return text
    for key, value in context.items():
        text = text.replace(f'{{{{{key}}}}}', str(value))
    return text


def _parse_bool(value, default: bool = True) -> bool:
    """Convertit une valeur cfg (bool, str "true"/"false", int) en bool Python.
    Nécessaire car les champs checkbox du frontend arrivent parfois en string.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ('false', '0', 'no', '')
    if isinstance(value, int):
        return bool(value)
    return default


class HttpRequestExecutor(BaseExecutor):

    def run(self) -> dict:
        """Point d'entrée du nœud.

        Lit la configuration, construit la requête HTTP (headers, auth, body),
        l'exécute avec retry exponentiel, valide le code de statut et retourne
        la réponse dans le contexte sous output_key.
        """
        # ── Champs de base ────────────────────────────────────────────────────
        url    = _resolve_vars(self.cfg('url', '').strip(), self.context)
        method = (self.cfg('method', 'GET') or 'GET').strip().upper()

        if not url:
            raise RuntimeError(
                "[HttpRequest] Le champ 'url' est requis.\n"
                "Ex : https://gitlab.example.com/api/v4/projects/{{project_id}}/pipeline"
            )

        content_type = self.cfg('content_type', 'application/json').strip()
        body_raw     = _resolve_vars((self.cfg('body', '') or '').strip(), self.context)
        headers_raw  = _resolve_vars((self.cfg('headers', '') or '').strip(), self.context)
        timeout      = int(self.cfg('timeout', 30) or 30)

        # cfg peut retourner "True"/"False" (string) depuis le frontend → cast explicite
        verify_ssl = _parse_bool(self.cfg('verify_ssl', True))

        raw_status = self.cfg('expected_status') or self.cfg('expectedStatus') or 200
        try:
            expected_status = int(str(raw_status).strip().split()[0])
        except (ValueError, TypeError, IndexError):
            expected_status = 200

        output_key = (
            self.cfg('save_response_as') or self.cfg('outputKey', 'http_response') or 'http_response'
        ).strip()

        # ── Paramètres de retry ───────────────────────────────────────────────
        retry_on_fail = _parse_bool(self.cfg('retry_on_fail', False), default=False)
        try:
            retry_count = max(1, int(self.cfg('retry_count') or 1)) if retry_on_fail else 1
        except (ValueError, TypeError):
            retry_count = 1
        retry_delay = int(self.cfg('retry_delay', 5) or 5)

        # Codes HTTP qui déclenchent un retry (surchargeables depuis la config)
        retry_on_status = [429, 500, 502, 503, 504]
        custom_retry_status = self.cfg('retry_on_status')
        if custom_retry_status:
            try:
                if isinstance(custom_retry_status, str):
                    retry_on_status = [int(s.strip()) for s in custom_retry_status.split(',') if s.strip().isdigit()]
                elif isinstance(custom_retry_status, list):
                    retry_on_status = [int(s) for s in custom_retry_status]
                elif isinstance(custom_retry_status, int):
                    retry_on_status = [custom_retry_status]
            except Exception:
                pass

        # ── Construction des headers ──────────────────────────────────────────
        # Ordre appliqué : Content-Type → Auth → Headers additionnels (peuvent écraser les précédents)
        headers: dict = {
            'Content-Type': content_type,
            'Accept':       'application/json',
        }

        auth_type = (self.cfg('auth_type', 'Aucune') or 'Aucune').strip()
        headers = self._apply_auth(headers, auth_type)

        if headers_raw and headers_raw not in ('{}', '', 'null'):
            try:
                custom = json.loads(headers_raw)
                if isinstance(custom, dict):
                    headers.update({k: _resolve_vars(str(v), self.context) for k, v in custom.items()})
            except (json.JSONDecodeError, ValueError):
                logger.warning('[HttpRequest] headers additionnels JSON invalides — ignorés')

        # ── Encodage du corps ─────────────────────────────────────────────────
        # GET/HEAD n'ont pas de body ; form-urlencoded nécessite un encodage différent de JSON
        body_bytes: bytes | None = None
        if body_raw and method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            if content_type == 'application/x-www-form-urlencoded':
                try:
                    body_dict  = json.loads(body_raw)
                    body_bytes = urlencode(body_dict).encode('utf-8')
                except (json.JSONDecodeError, ValueError):
                    # body_raw est déjà au format clé=valeur&clé2=valeur2
                    body_bytes = body_raw.encode('utf-8')
            else:
                body_bytes = body_raw.encode('utf-8')

        logger.info(
            f'[HttpRequest] {method} {url}  auth={auth_type}  '
            f'content_type={content_type}  timeout={timeout}s  retries={retry_count}'
        )

        # ── Contexte SSL ──────────────────────────────────────────────────────
        # verify_ssl=False uniquement pour les environnements internes avec certificats auto-signés
        ssl_ctx = None
        if not verify_ssl:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode    = ssl.CERT_NONE

        # ── Boucle de retry avec backoff exponentiel + jitter ─────────────────
        last_error: Exception | None = None
        response_data: dict | str    = {}
        status_code = 0

        for attempt in range(retry_count):
            if attempt > 0:
                # Backoff exponentiel plafonné à 60s + jitter aléatoire pour éviter les tempêtes
                wait = min(60, retry_delay * (2 ** (attempt - 1))) + random.uniform(0, 1)
                logger.info(f'[HttpRequest] Tentative {attempt + 1}/{retry_count} (attente {wait:.2f}s)')
                time.sleep(wait)
            try:
                req = Request(url, data=body_bytes, method=method, headers=headers)
                with urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
                    status_code = resp.status
                    raw = resp.read().decode('utf-8', errors='replace')
                    try:
                        response_data = json.loads(raw)
                    except (json.JSONDecodeError, ValueError):
                        response_data = {'raw': raw[:4000]}
                    last_error = None
                    break

            except HTTPError as exc:
                status_code = exc.code
                err_body    = exc.read().decode('utf-8', errors='replace')[:500]
                last_error  = RuntimeError(
                    f"[HttpRequest] HTTP {status_code} — {exc.reason}\n"
                    f"URL: {url}\nRéponse: {err_body}"
                )
                logger.warning(f'[HttpRequest] HTTP {status_code} (tentative {attempt + 1})')
                if status_code not in retry_on_status:
                    raise last_error

            except URLError as exc:
                last_error = RuntimeError(f"[HttpRequest] Erreur réseau : {exc}\nURL: {url}")
                logger.warning(f'[HttpRequest] Erreur réseau (tentative {attempt + 1}) : {exc}')

        if last_error:
            raise last_error

        # ── Validation du code de statut attendu ──────────────────────────────
        if expected_status > 0 and status_code != expected_status:
            raise RuntimeError(
                f"[HttpRequest] Code HTTP inattendu : {status_code} (attendu : {expected_status})\n"
                f"URL: {url}"
            )

        logger.info(f'[HttpRequest] ✓ {status_code}  clé_sortie={output_key}')

        # ── Construction du résultat injecté dans le contexte ─────────────────
        result = {
            output_key:         response_data,
            'http_status_code': status_code,
            'http_method':      method,
            'http_url':         url,
            'http_success':     True,
            'http_auth_type':   auth_type,
        }

        # Mapping optionnel : extrait des champs de la réponse via chemin pointé (ex: "data.id")
        response_mapping_raw = self.cfg('response_mapping', '').strip()
        if response_mapping_raw:
            try:
                mapping = json.loads(response_mapping_raw)
                for ctx_key, json_path in mapping.items():
                    value = response_data
                    for part in str(json_path).split('.'):
                        value = value.get(part) if isinstance(value, dict) else None
                    result[ctx_key] = value
            except Exception:
                pass

        return result

    # ── Méthodes d'authentification ───────────────────────────────────────────

    def _apply_auth(self, headers: dict, auth_type: str) -> dict:
        """Injecte l'header Authorization (ou custom) selon le type d'auth configuré.

        Modifie headers en place et le retourne.
        Lève RuntimeError si les champs requis sont absents ou vides.
        """
        if auth_type == 'Bearer Token':
            token = _resolve_vars(
                (self.cfg('auth_token', '') or '').strip(), self.context
            )
            if not token:
                raise RuntimeError(
                    "[HttpRequest] auth_type=Bearer Token mais 'auth_token' est vide.\n"
                    "Renseignez le champ Token ou utilisez {{MA_VARIABLE}}."
                )
            headers['Authorization'] = f'Bearer {token}'
            logger.info('[HttpRequest] Auth : Bearer Token ✓')

        elif auth_type == 'Basic Auth':
            username = _resolve_vars(
                (self.cfg('auth_username', '') or '').strip(), self.context
            )
            password = _resolve_vars(
                (self.cfg('auth_password', '') or '').strip(), self.context
            )
            if not username:
                raise RuntimeError(
                    "[HttpRequest] auth_type=Basic Auth mais 'auth_username' est vide."
                )
            credentials = base64.b64encode(f'{username}:{password}'.encode()).decode()
            headers['Authorization'] = f'Basic {credentials}'
            logger.info(f'[HttpRequest] Auth : Basic Auth (user={username}) ✓')

        elif auth_type == 'API Key':
            header_name = (
                self.cfg('auth_header_name', '') or 'X-API-Key'
            ).strip()
            api_key = _resolve_vars(
                (self.cfg('auth_api_key', '') or '').strip(), self.context
            )
            if not api_key:
                raise RuntimeError(
                    f"[HttpRequest] auth_type=API Key mais 'auth_api_key' est vide.\n"
                    f"Renseignez le champ Valeur de la clé ou utilisez {{{{MA_VARIABLE}}}}."
                )
            headers[header_name] = api_key
            logger.info(f'[HttpRequest] Auth : API Key sur header "{header_name}" ✓')

        # auth_type == 'Aucune' → aucun header ajouté

        return headers
