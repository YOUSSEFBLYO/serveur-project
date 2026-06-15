"""
AIOps — Log Classifier (nœud aiops.LogClassifier)

Agent IA qui reçoit les logs bruts depuis le contexte (produit par ElasticsearchFetch)
et les classifie par niveau de sévérité, détecte les anomalies, et produit un résumé
structuré pour le nœud suivant (ReportGenerator ou Notification).

Providers IA supportés :
    openai   → API OpenAI (GPT-4o, GPT-4o-mini, GPT-3.5-turbo …)
    gemini   → Google Gemini API (gemini-1.5-flash, gemini-1.5-pro …)
    ollama   → Serveur Ollama local (llama3, mistral …) — aucune clé API requise

Config du nœud (canvas React Flow) :
    ai_provider    : str  — openai | gemini | ollama  (défaut: openai)
    ai_model       : str  — Nom du modèle             (défaut: gpt-4o-mini)
    api_key        : str  — Clé API (supporte {{MA_VAR}})
    api_base_url   : str  — URL de base (seulement pour ollama/custom)
                            ex: http://localhost:11434/api
    input_key      : str  — Clé contexte contenant les logs  (défaut: es_logs)
    max_logs       : int  — Nombre max de logs envoyés à l'IA (défaut: 50)
    custom_prompt  : str  — Instruction additionnelle à ajouter au prompt système
    language       : str  — Langue du rapport IA  fr | en  (défaut: fr)
    output_key     : str  — Clé de sortie  (défaut: ai_analysis)
"""
import json
import logging
import re
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


# ─────────────────────────────────────────────────────────────────────────────
# Prompt système — instruit l'IA sur le format de réponse attendu
# ─────────────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT_FR = """Tu es un expert en analyse de logs applicatifs et en opérations (AIOps).
On te fournit une liste de logs bruts au format JSON.

Ta tâche est d'analyser ces logs et de répondre UNIQUEMENT avec un objet JSON valide
ayant EXACTEMENT la structure suivante (sans markdown, sans backticks, sans explication) :

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
      "affected_service": "Nom du service",
      "first_occurrence": "timestamp ISO",
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
  "immediate_actions": ["Action 1 si urgence", "Action 2"]
}

Règles :
- Classe chaque log dans l'un des niveaux : CRITICAL, ERROR, WARNING, INFO
- Détecte les patterns répétitifs qui signalent une anomalie
- Si aucune anomalie : anomalies = [], top_errors = []
- Sois précis et concis dans les descriptions
"""

_SYSTEM_PROMPT_EN = """You are an expert in application log analysis and AIOps.
You receive a list of raw logs in JSON format.

Your task is to analyze these logs and respond ONLY with a valid JSON object
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
      "first_occurrence": "ISO timestamp",
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
  "immediate_actions": ["Action 1 if urgent", "Action 2"]
}
"""


class LogClassifierExecutor(BaseExecutor):
    """
    Agent IA de classification de logs.

    Reçoit les logs depuis le contexte (produit par ElasticsearchFetchExecutor),
    construit un prompt structuré, appelle l'API IA choisie et retourne
    une analyse complète (comptages, anomalies, santé globale) dans le contexte.
    """

    def run(self) -> dict:
        # ── Paramètres ────────────────────────────────────────────────────────
        ai_provider = (self.cfg('ai_provider', 'openai') or 'openai').strip().lower()
        ai_model    = (self.cfg('ai_model', '')          or '').strip()
        api_key     = _resolve_vars(
            (self.cfg('api_key', '') or '').strip(), self.context
        )
        api_base_url = _resolve_vars(
            (self.cfg('api_base_url', '') or '').strip(), self.context
        )
        input_key    = (self.cfg('input_key', 'es_logs') or 'es_logs').strip()
        max_logs     = int(self.cfg('max_logs', 50) or 50)
        language     = (self.cfg('language', 'fr') or 'fr').strip().lower()
        output_key   = (self.cfg('output_key', 'ai_analysis') or 'ai_analysis').strip()
        custom_prompt = (self.cfg('custom_prompt', '') or '').strip()

        # ── Récupération des logs depuis le contexte ──────────────────────────
        logs = self.context.get(input_key, [])
        if not logs:
            logger.warning(
                f'[LogClassifier] Aucun log trouvé dans le contexte '
                f'(clé: "{input_key}") — analyse ignorée.'
            )
            return {
                output_key: {
                    'summary': 'Aucun log à analyser.',
                    'overall_health': 'HEALTHY',
                    'critical_count': 0, 'error_count': 0,
                    'warning_count': 0,  'info_count': 0,
                    'anomalies': [], 'top_errors': [],
                    'needs_immediate_action': False,
                    'immediate_actions': [],
                },
                'ai_logs_analyzed': 0,
                'ai_provider': ai_provider,
            }

        # Limite au nombre max de logs
        logs_to_analyze = logs[:max_logs]
        logs_json = json.dumps(logs_to_analyze, ensure_ascii=False, indent=None)

        logger.info(
            f'[LogClassifier] Analyse de {len(logs_to_analyze)}/{len(logs)} logs '
            f'via provider="{ai_provider}" model="{ai_model}"'
        )

        # ── Choix du prompt système ───────────────────────────────────────────
        system_prompt = _SYSTEM_PROMPT_EN if language == 'en' else _SYSTEM_PROMPT_FR
        if custom_prompt:
            system_prompt += f"\n\nInstruction additionnelle : {custom_prompt}"

        user_message = f"Voici {len(logs_to_analyze)} logs à analyser :\n{logs_json}"

        # ── Appel IA selon le provider ────────────────────────────────────────
        if ai_provider == 'gemini':
            raw_response = self._call_gemini(
                api_key, ai_model or 'gemini-1.5-flash',
                system_prompt, user_message
            )
        elif ai_provider == 'ollama':
            raw_response = self._call_ollama(
                api_base_url or 'http://localhost:11434',
                ai_model or 'llama3',
                system_prompt, user_message
            )
        else:
            # OpenAI (défaut)
            raw_response = self._call_openai(
                api_key, ai_model or 'gpt-4o-mini',
                system_prompt, user_message
            )

        # ── Parsing de la réponse JSON ────────────────────────────────────────
        analysis = self._parse_ai_response(raw_response, ai_provider)

        logger.info(
            f'[LogClassifier] ✅ Analyse terminée — '
            f'health={analysis.get("overall_health")}  '
            f'critical={analysis.get("critical_count")}  '
            f'errors={analysis.get("error_count")}  '
            f'warnings={analysis.get("warning_count")}'
        )

        return {
            output_key:        analysis,
            'ai_analysis':     analysis,
            'ai_logs_analyzed': len(logs_to_analyze),
            'ai_provider':     ai_provider,
            'ai_model':        ai_model,
            'ai_overall_health':    analysis.get('overall_health', 'UNKNOWN'),
            'ai_needs_action': analysis.get('needs_immediate_action', False),
        }

    # ── Providers ─────────────────────────────────────────────────────────────

    def _call_openai(self, api_key: str, model: str,
                     system_prompt: str, user_message: str) -> str:
        if not api_key:
            raise RuntimeError(
                "[LogClassifier] provider=openai mais 'api_key' est vide.\n"
                "Renseignez la clé API OpenAI dans la config du nœud."
            )
        payload = json.dumps({
            "model": model,
            "messages": [
                {"role": "system",  "content": system_prompt},
                {"role": "user",    "content": user_message},
            ],
            "temperature":  0.1,
            "max_tokens":   2000,
            "response_format": {"type": "json_object"},
        }).encode('utf-8')

        req = Request(
            'https://api.openai.com/v1/chat/completions',
            data=payload,
            method='POST',
            headers={
                'Content-Type':  'application/json',
                'Authorization': f'Bearer {api_key}',
            }
        )
        return self._http_call(req, provider='OpenAI')

    def _call_gemini(self, api_key: str, model: str,
                     system_prompt: str, user_message: str) -> str:
        if not api_key:
            raise RuntimeError(
                "[LogClassifier] provider=gemini mais 'api_key' est vide.\n"
                "Renseignez la clé API Google AI Studio dans la config du nœud."
            )
        url = (
            f'https://generativelanguage.googleapis.com/v1beta/models/'
            f'{model}:generateContent?key={api_key}'
        )
        payload = json.dumps({
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"parts": [{"text": user_message}]}],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 2000,
                "responseMimeType": "application/json",
            },
        }).encode('utf-8')

        req = Request(
            url, data=payload, method='POST',
            headers={'Content-Type': 'application/json'}
        )
        return self._http_call(req, provider='Gemini')

    def _call_ollama(self, base_url: str, model: str,
                     system_prompt: str, user_message: str) -> str:
        """Appelle un serveur Ollama local (aucune clé API requise)."""
        url = f'{base_url.rstrip("/")}/api/chat'
        payload = json.dumps({
            "model":  model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_message},
            ],
            "options": {"temperature": 0.1},
        }).encode('utf-8')

        req = Request(
            url, data=payload, method='POST',
            headers={'Content-Type': 'application/json'}
        )
        return self._http_call(req, provider='Ollama')

    # ── HTTP helper commun ────────────────────────────────────────────────────

    def _http_call(self, req: Request, provider: str) -> str:
        try:
            with urlopen(req, timeout=60) as resp:
                raw = resp.read().decode('utf-8', errors='replace')
                data = json.loads(raw)

            # Extraction du texte selon chaque provider
            if provider == 'OpenAI':
                return data['choices'][0]['message']['content']
            elif provider == 'Gemini':
                return data['candidates'][0]['content']['parts'][0]['text']
            elif provider == 'Ollama':
                return data['message']['content']
            return raw

        except HTTPError as exc:
            err_body = exc.read().decode('utf-8', errors='replace')[:500]
            raise RuntimeError(
                f"[LogClassifier] {provider} HTTP {exc.code} — {exc.reason}\n"
                f"Réponse: {err_body}"
            )
        except URLError as exc:
            raise RuntimeError(
                f"[LogClassifier] Impossible de joindre {provider} : {exc}"
            )
        except (KeyError, IndexError) as exc:
            raise RuntimeError(
                f"[LogClassifier] Format de réponse {provider} inattendu : {exc}\n"
                f"Réponse brute reçue — vérifiez le modèle configuré."
            )

    # ── Parsing de la réponse ─────────────────────────────────────────────────

    def _parse_ai_response(self, raw: str, provider: str) -> dict:
        """Extrait le JSON de la réponse IA (robuste aux markdown fences)."""
        text = raw.strip()

        # Retire les backticks markdown si présents (ex: ```json ... ```)
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)

        try:
            result = json.loads(text)
            # Assure la présence des champs minimaux
            result.setdefault('summary', 'Analyse complétée.')
            result.setdefault('overall_health', 'UNKNOWN')
            result.setdefault('critical_count', 0)
            result.setdefault('error_count', 0)
            result.setdefault('warning_count', 0)
            result.setdefault('info_count', 0)
            result.setdefault('anomalies', [])
            result.setdefault('top_errors', [])
            result.setdefault('needs_immediate_action', False)
            result.setdefault('immediate_actions', [])
            return result

        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                f'[LogClassifier] Réponse IA non-JSON ({provider}) — '
                f'parsing de secours activé. Erreur: {exc}'
            )
            # Fallback : retourne la réponse brute dans le résumé
            return {
                'summary': raw[:1000],
                'overall_health': 'UNKNOWN',
                'critical_count': 0, 'error_count': 0,
                'warning_count': 0,  'info_count': 0,
                'anomalies': [], 'top_errors': [],
                'needs_immediate_action': False,
                'immediate_actions': [],
                '_parse_error': str(exc),
                '_raw_response': raw[:500],
            }
