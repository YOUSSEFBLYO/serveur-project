"""
HTTP Request (Universel) — Executor.

Effectue une requête HTTP vers n'importe quel endpoint REST.
Supporte GET, POST, PUT, PATCH, DELETE avec headers et body JSON personnalisés.
Idéal pour intégrer n'importe quelle API tierce dans un workflow.
"""
import json
import logging
import time
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

from .base import BaseExecutor

logger = logging.getLogger(__name__)


class HttpRequestExecutor(BaseExecutor):
    """
    integration.Http — Requête HTTP universelle.

    Config du nœud :
        url            : str  — URL de destination (requis)
        method         : enum — 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
        headers        : text — Headers JSON (ex: {"Authorization": "Bearer token"})
        body           : text — Corps de la requête JSON (pour POST/PUT/PATCH)
        timeout        : str  — Timeout en secondes (défaut: 30)
        expectedStatus : str  — Code HTTP attendu (défaut: 200, 0=ignorer)
        outputKey      : str  — Clé sous laquelle stocker la réponse dans le contexte
        retryCount     : str  — Nombre de tentatives en cas d'échec (défaut: 1)
        retryDelay     : str  — Délai entre tentatives en secondes (défaut: 5)
    """

    def run(self) -> dict:
        url             = self.cfg('url', '').strip()
        method          = self.cfg('method', 'GET').strip().upper() or 'GET'
        headers_raw     = self.cfg('headers', '{}').strip()
        body_raw        = self.cfg('body', '').strip()
        timeout         = int(self.cfg('timeout', '30') or '30')
        expected_status = int(self.cfg('expectedStatus', '200') or '200')
        output_key      = self.cfg('outputKey', 'http_response').strip() or 'http_response'
        retry_count     = int(self.cfg('retryCount', '1') or '1')
        retry_delay     = int(self.cfg('retryDelay', '5') or '5')

        if not url:
            raise RuntimeError(
                "[HttpRequest] 'url' non configurée.\n"
                "Renseignez l'URL de l'endpoint cible."
            )

        # Parsing headers
        headers: dict = {'Content-Type': 'application/json', 'Accept': 'application/json'}
        if headers_raw and headers_raw != '{}':
            try:
                custom_headers = json.loads(headers_raw)
                if isinstance(custom_headers, dict):
                    headers.update(custom_headers)
            except (json.JSONDecodeError, ValueError):
                logger.warning('[HttpRequest] headers JSON invalides — ignorés')

        # Parsing body
        body_bytes: bytes | None = None
        if body_raw and method in ('POST', 'PUT', 'PATCH'):
            # Substitution des variables du contexte dans le body
            body_with_ctx = body_raw
            for k, v in self.context.items():
                body_with_ctx = body_with_ctx.replace(f'{{{{{k}}}}}', str(v))
            try:
                # Validate JSON
                json.loads(body_with_ctx)
                body_bytes = body_with_ctx.encode('utf-8')
            except (json.JSONDecodeError, ValueError):
                body_bytes = body_raw.encode('utf-8')

        logger.info(
            f'[HttpRequest] {method} {url}  '
            f'timeout={timeout}s  retries={retry_count}'
        )

        last_error: Exception | None = None
        response_data: dict | str = {}
        status_code = 0

        for attempt in range(retry_count):
            if attempt > 0:
                logger.info(f'[HttpRequest] Tentative {attempt + 1}/{retry_count} après {retry_delay}s')
                time.sleep(retry_delay)

            try:
                req = Request(
                    url,
                    data=body_bytes,
                    method=method,
                    headers=headers,
                )
                with urlopen(req, timeout=timeout) as resp:
                    status_code  = resp.status
                    raw_response = resp.read().decode('utf-8', errors='replace')
                    try:
                        response_data = json.loads(raw_response)
                    except (json.JSONDecodeError, ValueError):
                        response_data = {'raw': raw_response[:2000]}
                    last_error = None
                    break  # Succès

            except HTTPError as exc:
                status_code = exc.code
                error_body  = exc.read().decode('utf-8', errors='replace')[:500]
                last_error  = RuntimeError(
                    f"[HttpRequest] HTTP {status_code} — {exc.reason}\n"
                    f"URL: {url}\nRéponse: {error_body}"
                )
                logger.warning(f'[HttpRequest] HTTP {status_code} (tentative {attempt + 1})')

            except URLError as exc:
                last_error = RuntimeError(
                    f"[HttpRequest] Erreur réseau : {exc}\nURL: {url}"
                )
                logger.warning(f'[HttpRequest] Erreur réseau (tentative {attempt + 1}) : {exc}')

        if last_error:
            raise last_error

        logger.info(
            f'[HttpRequest] Réponse reçue — status={status_code}  '
            f'type={type(response_data).__name__}'
        )

        # Vérification du code HTTP attendu
        if expected_status > 0 and status_code != expected_status:
            raise RuntimeError(
                f"[HttpRequest] Code HTTP inattendu : {status_code} (attendu: {expected_status})\n"
                f"URL: {url}"
            )

        return {
            output_key:           response_data,
            'http_status_code':   status_code,
            'http_method':        method,
            'http_url':           url,
            'http_success':       True,
        }
