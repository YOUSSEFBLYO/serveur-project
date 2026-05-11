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
import ssl
import time
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

from .base import BaseExecutor

logger = logging.getLogger(__name__)


def _resolve_vars(text: str, context: dict) -> str:
    """Remplace {{key}} par la valeur du contexte."""
    if not text:
        return text
    for key, value in context.items():
        text = text.replace(f'{{{{{key}}}}}', str(value))
    return text


class HttpRequestExecutor(BaseExecutor):

    def run(self) -> dict:
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
        verify_ssl   = self.cfg('verify_ssl', True)

        expected_status = int(
            self.cfg('expected_status') or self.cfg('expectedStatus', 200) or 200
        )
        output_key = (
            self.cfg('save_response_as') or self.cfg('outputKey', 'http_response') or 'http_response'
        ).strip()

        retry_on_fail = self.cfg('retry_on_fail', False)
        retry_count   = max(1, int(self.cfg('retry_count') or 1)) if retry_on_fail else 1
        retry_delay   = int(self.cfg('retry_delay', 5) or 5)

        # ── Construction des headers ──────────────────────────────────────────
        # Ordre : Content-Type → Authentification → Headers additionnels
        headers: dict = {
            'Content-Type': content_type,
            'Accept':       'application/json',
        }

        # 1) Authentification intégrée
        auth_type = (self.cfg('auth_type', 'Aucune') or 'Aucune').strip()
        headers = self._apply_auth(headers, auth_type)

        # 2) Headers additionnels (peuvent écraser Content-Type ou l'auth si besoin)
        if headers_raw and headers_raw not in ('{}', '', 'null'):
            try:
                custom = json.loads(headers_raw)
                if isinstance(custom, dict):
                    headers.update({k: _resolve_vars(str(v), self.context) for k, v in custom.items()})
            except (json.JSONDecodeError, ValueError):
                logger.warning('[HttpRequest] headers additionnels JSON invalides — ignorés')

        # ── Encodage du corps ─────────────────────────────────────────────────
        body_bytes: bytes | None = None
        if body_raw and method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            if content_type == 'application/x-www-form-urlencoded':
                try:
                    body_dict  = json.loads(body_raw)
                    body_bytes = urlencode(body_dict).encode('utf-8')
                except (json.JSONDecodeError, ValueError):
                    # Déjà au format clé=valeur
                    body_bytes = body_raw.encode('utf-8')
            else:
                body_bytes = body_raw.encode('utf-8')

        logger.info(
            f'[HttpRequest] {method} {url}  auth={auth_type}  '
            f'content_type={content_type}  timeout={timeout}s  retries={retry_count}'
        )

        # ── Contexte SSL ──────────────────────────────────────────────────────
        ssl_ctx = None
        if not verify_ssl:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode    = ssl.CERT_NONE

        # ── Boucle de retry ───────────────────────────────────────────────────
        last_error: Exception | None = None
        response_data: dict | str    = {}
        status_code = 0

        for attempt in range(retry_count):
            if attempt > 0:
                logger.info(f'[HttpRequest] Tentative {attempt + 1}/{retry_count} (attente {retry_delay}s)')
                time.sleep(retry_delay)
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

            except URLError as exc:
                last_error = RuntimeError(f"[HttpRequest] Erreur réseau : {exc}\nURL: {url}")
                logger.warning(f'[HttpRequest] Erreur réseau (tentative {attempt + 1}) : {exc}')

        if last_error:
            raise last_error

        # ── Validation du code de statut ──────────────────────────────────────
        if expected_status > 0 and status_code != expected_status:
            raise RuntimeError(
                f"[HttpRequest] Code HTTP inattendu : {status_code} (attendu : {expected_status})\n"
                f"URL: {url}"
            )

        logger.info(f'[HttpRequest] ✓ {status_code}  clé_sortie={output_key}')

        return {
            output_key:         response_data,
            'http_status_code': status_code,
            'http_method':      method,
            'http_url':         url,
            'http_success':     True,
            'http_auth_type':   auth_type,
        }

    # ── Méthodes d'authentification ───────────────────────────────────────────

    def _apply_auth(self, headers: dict, auth_type: str) -> dict:
        """Injecte l'header d'authentification selon le type choisi."""

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
