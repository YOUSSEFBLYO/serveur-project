"""
iTop Ticket (ITSM) — Executor.

Crée ou met à jour un ticket iTop (Change, Incident, Service Request)
directement depuis le workflow via l'API REST iTop.

Compatible avec le module self_service_change de Kraken qui utilise
déjà itop_wrapper.py sur la même instance iTop.
"""
import json
import logging
import os
from urllib.request import urlopen, Request
from urllib.parse import urlencode
from urllib.error import URLError, HTTPError

from .base import BaseExecutor

logger = logging.getLogger(__name__)

_DEFAULT_ITOP_URL  = os.environ.get('ITOP_URL',  'https://itop.company.com/webservices/rest.php')
_DEFAULT_ITOP_USER = os.environ.get('ITOP_USER', '')
_DEFAULT_ITOP_PASS = os.environ.get('ITOP_PASS', '')


def _itop_request(url: str, user: str, password: str, operation: str, payload: dict) -> dict:
    """Effectue un appel à l'API REST iTop."""
    json_data = json.dumps({'operation': operation, **payload})
    form_data = urlencode({
        'version':  '1.3',
        'auth_user': user,
        'auth_pwd':  password,
        'json_data': json_data,
    }).encode('utf-8')

    req = Request(url, data=form_data, method='POST')
    req.add_header('Content-Type', 'application/x-www-form-urlencoded')

    with urlopen(req, timeout=30) as resp:
        raw = resp.read().decode('utf-8')

    result = json.loads(raw)
    if result.get('code', 0) != 0:
        raise RuntimeError(
            f"[iTop] Erreur API : {result.get('message', 'Erreur inconnue')}\n"
            f"Code: {result.get('code')}"
        )
    return result


class ITopTicketExecutor(BaseExecutor):
    """
    itop.Ticket — Création/Mise à jour de ticket iTop (ITSM).

    Config du nœud :
        itopUrl      : str  — URL de l'API REST iTop
        itopUser     : str  — Login iTop
        itopPass     : str  — Mot de passe iTop
        action       : enum — 'create' | 'update' | 'get_status'
        ticketClass  : enum — 'Change' | 'Incident' | 'UserRequest'
        title        : str  — Titre du ticket (obligatoire pour create)
        description  : text — Description / notes
        impact       : enum — '1' | '2' | '3' (Haut / Moyen / Bas)
        urgency      : enum — '1' | '2' | '3'
        ticketId     : str  — ID du ticket existant (obligatoire pour update/get_status)
        extraFields  : text — Champs supplémentaires JSON (ex: {"team_id": 2})
        outputKey    : str  — Clé de sortie dans le contexte (défaut: itop_ticket)
    """

    def run(self) -> dict:
        itop_url    = self.cfg('itopUrl',     _DEFAULT_ITOP_URL).strip()
        itop_user   = self.cfg('itopUser',    _DEFAULT_ITOP_USER).strip()
        itop_pass   = self.cfg('itopPass',    _DEFAULT_ITOP_PASS).strip()
        action       = self.cfg('action',      'create').strip().lower()
        ticket_class = self.cfg('ticketClass', 'Change').strip()
        title        = self.cfg('title',       '').strip()
        description  = self.cfg('description', '').strip()
        impact       = str(self.cfg('impact',  '2')).strip()
        urgency      = str(self.cfg('urgency', '2')).strip()
        ticket_id    = str(self.cfg('ticketId', '')).strip()
        output_key   = self.cfg('outputKey', 'itop_ticket').strip() or 'itop_ticket'

        extra_fields: dict = {}
        try:
            raw_extra = self.cfg('extraFields', '{}')
            if isinstance(raw_extra, str) and raw_extra.strip():
                extra_fields = json.loads(raw_extra)
        except json.JSONDecodeError:
            logger.warning('[iTop] extraFields invalide — ignoré')

        if not itop_url:
            raise RuntimeError("[iTop] 'itopUrl' non configuré.")
        if not itop_user or not itop_pass:
            raise RuntimeError(
                "[iTop] Identifiants iTop manquants.\n"
                "Configurez ITOP_USER / ITOP_PASS dans .env ou dans les propriétés du nœud."
            )

        # ── CREATE ──────────────────────────────────────────────────────────
        if action == 'create':
            if not title:
                raise RuntimeError("[iTop] Le champ 'title' est obligatoire pour créer un ticket.")

            fields = {
                'title':       title,
                'description': description,
                'impact':      impact,
                'urgency':     urgency,
                **extra_fields,
            }
            logger.info(
                f'[iTop] Création d\'un ticket {ticket_class} — titre="{title}"  '
                f'impact={impact}  urgency={urgency}'
            )

            payload = {
                'class':          ticket_class,
                'fields':         fields,
                'output_fields':  'id,ref,friendlyname,status',
            }
            try:
                result = _itop_request(itop_url, itop_user, itop_pass, 'core/create', payload)
            except (URLError, HTTPError) as exc:
                raise RuntimeError(f"[iTop] Impossible de créer le ticket.\nDétail: {exc}")

            objects  = result.get('objects', {})
            ticket   = next(iter(objects.values()), {}).get('fields', {}) if objects else {}
            ref      = ticket.get('ref', f'{ticket_class}-???')
            obj_id   = ticket.get('id', '?')
            status   = ticket.get('status', '?')

            logger.info(f'[iTop] Ticket créé : {ref} (id={obj_id}, statut={status}) ✓')

            return {
                output_key: {
                    'action':       'create',
                    'class':        ticket_class,
                    'id':           obj_id,
                    'ref':          ref,
                    'status':       status,
                    'title':        title,
                    'itop_url':     itop_url,
                },
                'itop_ticket_ref': ref,
                'itop_ticket_id':  obj_id,
            }

        # ── UPDATE ───────────────────────────────────────────────────────────
        elif action == 'update':
            if not ticket_id:
                ticket_id = str(self.ctx('itop_ticket_id', '')).strip()
            if not ticket_id:
                raise RuntimeError(
                    "[iTop] 'ticketId' obligatoire pour mettre à jour un ticket.\n"
                    "Chaînez ce nœud après un nœud iTop.Ticket (create) ou renseignez l'ID."
                )

            fields = {**extra_fields}
            if description:
                fields['description'] = description

            logger.info(f'[iTop] Mise à jour du ticket {ticket_class}::id={ticket_id}')

            payload = {
                'class':  ticket_class,
                'key':    {'id': int(ticket_id)},
                'fields': fields,
                'output_fields': 'id,ref,status',
            }
            try:
                result = _itop_request(itop_url, itop_user, itop_pass, 'core/update', payload)
            except (URLError, HTTPError) as exc:
                raise RuntimeError(f"[iTop] Impossible de mettre à jour le ticket.\nDétail: {exc}")

            objects = result.get('objects', {})
            ticket  = next(iter(objects.values()), {}).get('fields', {}) if objects else {}
            ref     = ticket.get('ref', f'{ticket_class}-{ticket_id}')
            status  = ticket.get('status', '?')

            logger.info(f'[iTop] Ticket {ref} mis à jour — statut={status} ✓')

            return {
                output_key: {
                    'action': 'update',
                    'class':  ticket_class,
                    'id':     ticket_id,
                    'ref':    ref,
                    'status': status,
                },
                'itop_ticket_ref': ref,
            }

        # ── GET_STATUS ───────────────────────────────────────────────────────
        elif action == 'get_status':
            if not ticket_id:
                ticket_id = str(self.ctx('itop_ticket_id', '')).strip()
            if not ticket_id:
                raise RuntimeError("[iTop] 'ticketId' obligatoire pour consulter le statut.")

            oql     = f"SELECT {ticket_class} WHERE id = {ticket_id}"
            payload = {
                'class':         ticket_class,
                'key':           oql,
                'output_fields': 'id,ref,status,title,impact,urgency',
            }
            try:
                result = _itop_request(itop_url, itop_user, itop_pass, 'core/get', payload)
            except (URLError, HTTPError) as exc:
                raise RuntimeError(f"[iTop] Impossible de lire le ticket.\nDétail: {exc}")

            objects = result.get('objects', {})
            ticket  = next(iter(objects.values()), {}).get('fields', {}) if objects else {}
            status  = ticket.get('status', 'unknown')
            ref     = ticket.get('ref', f'{ticket_class}-{ticket_id}')

            logger.info(f'[iTop] Ticket {ref} — statut={status}')

            return {
                output_key: {
                    'action': 'get_status',
                    'class':  ticket_class,
                    'id':     ticket_id,
                    'ref':    ref,
                    'status': status,
                },
                'itop_ticket_status': status,
                'itop_ticket_ref':    ref,
            }

        else:
            raise RuntimeError(
                f"[iTop] Action inconnue : '{action}'.\n"
                "Valeurs autorisées : create | update | get_status"
            )
