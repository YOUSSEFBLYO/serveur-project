"""
Script Task — Executor.

Exécute un script fourni par l'utilisateur dans le nœud du canvas.

DEUX MODES selon la configuration du nœud :

  [Mode Python]   scriptType = 'Python'
    - Le script est écrit dans un fichier .py temporaire
    - Exécuté via `sys.executable` (même Python que le serveur Django)
    - Variable d'environnement REPO_PATH injectée si disponible

  [Mode Shell]    scriptType = 'Shell' (défaut)
    - Windows : `cmd.exe /c <script>`
    - Linux/Mac : `/bin/bash -c <script>`
    - Support multi-lignes (le script est passé en entier au shell)

PIPELINE DJANGO CI :
    Si le script contient `manage.py test`, les métriques Django sont extraites :
      - tests_passed, tests_failed, test_duration, build_status

Outputs propagés dans le contexte partagé :
    returncode, stdout, stderr, language, build_status,
    tests_passed, tests_failed, test_duration

Config du nœud (canvas) :
    script      : str  — Le script à exécuter (multi-lignes autorisé)
    scriptType  : str  — 'Python' ou 'Shell' (défaut: 'Shell')
    timeout     : int  — Timeout en secondes (défaut: 300)
"""
import logging
import os
import re
import subprocess
import sys
import tempfile
import time

from .base import BaseExecutor

logger  = logging.getLogger(__name__)
TIMEOUT = 300   # secondes par défaut


# ─────────────────────────────────────────────────────────────────────────────
# Utilitaires
# ─────────────────────────────────────────────────────────────────────────────

def _shell_cmd(script: str) -> list:
    """Retourne la commande shell adaptée à l'OS pour exécuter un script."""
    if sys.platform == 'win32':
        return ['cmd', '/c', script]
    else:
        return ['/bin/bash', '-c', script]


def _truncate(text: str, max_chars: int = 4000) -> str:
    """Tronque la sortie pour éviter des outputs trop volumineux en DB."""
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + '\n...[OUTPUT TRONQUÉ]...\n' + text[-half:]


# ─────────────────────────────────────────────────────────────────────────────
# Executor principal
# ─────────────────────────────────────────────────────────────────────────────

class ScriptTaskExecutor(BaseExecutor):
    """
    script.Task — Exécute un script Shell ou Python en sous-processus isolé.
    Spécialement adapté pour les pipelines Django CI/CD.
    """

    def run(self) -> dict:
        script      = self.cfg('script', '').strip()
        script_type = str(self.cfg('scriptType', 'Shell')).strip()
        # Rétro-compat avec l'ancien champ bool 'pythonScript'
        is_python   = (script_type.lower() == 'python') or bool(self.cfg('pythonScript', False))
        timeout     = int(self.cfg('timeout', TIMEOUT))
        repo_path   = self.ctx('repo_path', None)

        # ── Validation ────────────────────────────────────────────────────────
        if not script:
            logger.warning('[ScriptTask] Aucun script fourni — nœud ignoré')
            return {
                'result':       'no_script',
                'returncode':   0,
                'stdout':       '',
                'stderr':       '',
                'build_status': 'SKIPPED',
                'language':     'python' if is_python else 'shell',
                'tests_passed': 0,
                'tests_failed': 0,
                'test_duration': 0,
            }

        # Résoudre le répertoire de travail
        cwd = None
        if repo_path:
            if os.path.isdir(repo_path):
                cwd = repo_path
                logger.info(f'[ScriptTask] repo_path résolu : {cwd}')
            else:
                logger.warning(f'[ScriptTask] repo_path introuvable : {repo_path} — exécution sans cwd')

        # ── Routage selon le mode ─────────────────────────────────────────────
        if is_python:
            return self._run_python(script, cwd, timeout)
        else:
            return self._run_shell(script, cwd, timeout)

    # ─────────────────────────────────────────────────────────────────────────
    # Mode Python
    # ─────────────────────────────────────────────────────────────────────────

    def _run_python(self, script: str, cwd: str | None, timeout: int) -> dict:
        """
        Exécute le script Python dans un fichier temporaire.
        Le même interpréteur que le serveur Django est utilisé.
        REPO_PATH est injecté comme variable d'environnement.
        """
        tmp_file = None
        try:
            with tempfile.NamedTemporaryFile(
                mode='w',
                suffix='.py',
                delete=False,
                encoding='utf-8',
            ) as f:
                f.write(script)
                tmp_file = f.name

            logger.info(f'[ScriptTask/Python] Exécution  cwd={cwd or "(aucun)"}  timeout={timeout}s')

            env = os.environ.copy()
            env.pop('DJANGO_SETTINGS_MODULE', None)
            if cwd:
                env['REPO_PATH'] = cwd

            t0   = time.time()
            proc = subprocess.run(
                [sys.executable, tmp_file],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
            duration = round(time.time() - t0, 2)

            ok = proc.returncode == 0
            logger.info(f'[ScriptTask/Python] returncode={proc.returncode}  ok={ok}')

            result = {
                'returncode':    proc.returncode,
                'stdout':        _truncate(proc.stdout),
                'stderr':        _truncate(proc.stderr, 1000),
                'language':      'python',
                'build_status':  'SUCCESS' if ok else 'FAILED',
                'test_duration': duration,
            }
            result.update(self._parse_django_test_output(proc.stdout, proc.stderr))
            return result

        except subprocess.TimeoutExpired:
            logger.error(f'[ScriptTask/Python] Timeout atteint ({timeout}s)')
            raise RuntimeError(f'Script Python interrompu après {timeout}s (timeout).')

        finally:
            if tmp_file and os.path.exists(tmp_file):
                try:
                    os.unlink(tmp_file)
                except OSError:
                    pass

    # ─────────────────────────────────────────────────────────────────────────
    # Mode Shell
    # ─────────────────────────────────────────────────────────────────────────

    def _run_shell(self, script: str, cwd: str | None, timeout: int) -> dict:
        """
        Exécute le script Shell.
        Détecte automatiquement les commandes Django (manage.py test)
        pour extraire les métriques de tests.
        """
        logger.info(f'[ScriptTask/Shell] Exécution  cwd={cwd or "(aucun)"}  timeout={timeout}s')
        logger.debug(f'[ScriptTask/Shell] Script (extrait):\n{script[:300]}')

        # ── Injecter le chemin Python dans l'environnement ────────────────────
        # Sur Windows, cmd.exe n'a pas forcément Python dans son PATH.
        # On injecte sys.executable pour que 'python' soit résolu correctement.
        env = os.environ.copy()
        
        # IMPORTANT: Empêcher le script enfant d'hériter des settings de Workflow Engine
        env.pop('DJANGO_SETTINGS_MODULE', None)
        
        python_dir = os.path.dirname(sys.executable)
        current_path = env.get('PATH', '')
        if python_dir not in current_path:
            env['PATH'] = python_dir + os.pathsep + current_path
        if cwd:
            env['REPO_PATH'] = cwd

        cmd = _shell_cmd(script)
        t0  = time.time()

        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding='utf-8',
                errors='replace',
                env=env,
            )
            duration = round(time.time() - t0, 2)

            ok = proc.returncode == 0
            logger.info(f'[ScriptTask/Shell] returncode={proc.returncode}  ok={ok}  durée={duration}s')

            result = {
                'returncode':    proc.returncode,
                'stdout':        _truncate(proc.stdout),
                'stderr':        _truncate(proc.stderr, 1000),
                'language':      'shell',
                'build_status':  'SUCCESS' if ok else 'FAILED',
                'test_duration': duration,
            }

            # Parser les résultats de tests (Django, pytest, etc.)
            test_metrics = self._parse_django_test_output(proc.stdout, proc.stderr)
            result.update(test_metrics)

            return result

        except subprocess.TimeoutExpired:
            logger.error(f'[ScriptTask/Shell] Timeout atteint ({timeout}s)')
            raise RuntimeError(f'Script Shell interrompu après {timeout}s (timeout).')

    # ─────────────────────────────────────────────────────────────────────────
    # Parser des résultats — Priorité à Django test runner
    # ─────────────────────────────────────────────────────────────────────────

    def _parse_django_test_output(self, stdout: str, stderr: str) -> dict:
        """
        Extrait les métriques de tests depuis la sortie de commandes de test.

        Supporte dans l'ordre :
          1. Django test runner  (python manage.py test)
          2. pytest
          3. Format générique "X passed / Y failed"

        La sortie Django va sur stderr par défaut, donc on cherche dans les deux.
        """
        combined = stdout + '\n' + stderr

        # ── 1. Django test runner ─────────────────────────────────────────────
        # Format OK : "Ran 12 tests in 3.456s\n\nOK"
        # Format KO : "Ran 12 tests in 3.456s\n\nFAILED (failures=2, errors=1)"
        ran_match = re.search(
            r'Ran\s+(\d+)\s+tests?\s+in\s+([\d.]+)s',
            combined, re.IGNORECASE
        )
        if ran_match:
            total_ran = int(ran_match.group(1))
            duration  = float(ran_match.group(2))

            # Cherche "FAILED (failures=X, errors=Y)"
            fail_match = re.search(
                r'FAILED\s*\((?:failures?=(\d+))?(?:,?\s*errors?=(\d+))?\)',
                combined, re.IGNORECASE
            )
            if fail_match:
                failures = int(fail_match.group(1) or 0)
                errors   = int(fail_match.group(2) or 0)
                total_failed = failures + errors
                total_passed = max(0, total_ran - total_failed)
                build_status = 'FAILED'
            else:
                # OK ou SKIPPED
                skipped_m = re.search(r'OK\s*\(skipped=(\d+)\)', combined, re.IGNORECASE)
                skipped   = int(skipped_m.group(1)) if skipped_m else 0
                total_failed = 0
                total_passed = total_ran - skipped
                build_status = 'SUCCESS'

            logger.info(
                f'[ScriptTask] Django tests: {total_passed} passed, '
                f'{total_failed} failed, durée={duration}s'
            )
            return {
                'tests_passed':  total_passed,
                'tests_failed':  total_failed,
                'test_duration': duration,
                'coverage_pct':  0.0,
                'build_status':  build_status,
            }

        # ── 2. pytest ─────────────────────────────────────────────────────────
        # "X passed, Y failed in Zs"  ou  "X passed in Zs"
        pytest_m = re.search(
            r'(\d+)\s+passed(?:,\s*(\d+)\s+failed)?(?:\s+in\s+([\d.]+)s)?',
            combined, re.IGNORECASE
        )
        if pytest_m:
            passed   = int(pytest_m.group(1))
            failed   = int(pytest_m.group(2) or 0)
            duration = float(pytest_m.group(3) or 0)
            cov = re.search(r'(?:TOTAL|coverage)\s+\d+\s+\d+\s+\d+\s+([\d.]+)%', combined)
            return {
                'tests_passed':  passed,
                'tests_failed':  failed,
                'test_duration': duration,
                'coverage_pct':  float(cov.group(1)) if cov else 0.0,
                'build_status':  'FAILED' if failed > 0 else 'SUCCESS',
            }

        # ── 3. Format générique ───────────────────────────────────────────────
        pm  = re.search(r'(\d+)\s+(?:tests?\s+)?passed', combined, re.IGNORECASE)
        fm  = re.search(r'(\d+)\s+(?:tests?\s+)?failed', combined, re.IGNORECASE)

        if pm or fm:
            passed = int(pm.group(1)) if pm else 0
            failed = int(fm.group(1)) if fm else 0
            return {
                'tests_passed':  passed,
                'tests_failed':  failed,
                'test_duration': 0,
                'coverage_pct':  0.0,
                'build_status':  'FAILED' if failed > 0 else 'SUCCESS',
            }

        # Aucun résultat de test trouvé (script sans tests)
        return {
            'tests_passed':  0,
            'tests_failed':  0,
            'test_duration': 0,
            'coverage_pct':  0.0,
        }
