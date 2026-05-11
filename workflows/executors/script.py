"""
Script / Code — Exécuteur unifié (nœud 3/7).

Remplace :
  Script Task | Docker Build | Générer Rapport | Envoyer Email (local)

Pour tout ce qui nécessite une exécution locale sur le serveur.
Equivalent n8n : Code Node — Equivalent Camunda : Script Task.

Config du nœud :
    language    : enum — 'shell' | 'python' | 'ansible'  (défaut shell)
    script      : text — code à exécuter (supporte {{ctx_key}})
    timeout     : int  — timeout en secondes (défaut 300)
    workingDir  : str  — répertoire de travail (hérité de repo_path si vide)
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
TIMEOUT = 300


def _resolve_vars(text: str, context: dict) -> str:
    """Remplace {{key}} par la valeur du contexte."""
    if not text:
        return text
    for key, value in context.items():
        text = text.replace(f'{{{{{key}}}}}', str(value))
    return text


def _truncate(text: str, max_chars: int = 4000) -> str:
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + '\n...[TRONQUÉ]...\n' + text[-half:]


class ScriptExecutor(BaseExecutor):
    """
    script — Exécute un script Shell, Python ou Ansible en sous-processus isolé.
    Les variables {{ctx_key}} dans le script sont résolues avant exécution.
    """

    def run(self) -> dict:
        language   = str(self.cfg('language', 'shell')).lower().strip()
        script_raw = self.cfg('script', '').strip()
        timeout    = int(self.cfg('timeout', TIMEOUT) or TIMEOUT)

        # Nouveau nom: working_dir — ancien: workingDir (rétrocompat)
        work_dir_cfg = (self.cfg('working_dir') or self.cfg('workingDir', '')).strip()
        repo_path    = self.ctx('repo_path', None)
        cwd = None
        if work_dir_cfg and os.path.isdir(work_dir_cfg):
            cwd = work_dir_cfg
        elif repo_path and os.path.isdir(str(repo_path)):
            cwd = repo_path

        # Nouveaux champs
        env_vars_raw   = self.cfg('env_vars', '').strip()        # "KEY=VALUE\nKEY2=VALUE2"
        fail_on_stderr = bool(self.cfg('fail_on_stderr', False))
        save_output_as = self.cfg('save_output_as', '').strip()  # clé de sortie stdout

        # Résolution des variables dans le script
        script = _resolve_vars(script_raw, self.context)

        if not script:
            logger.warning('[Script] Aucun script fourni — nœud passthrough')
            return {
                'result':       'no_script',
                'returncode':   0,
                'build_status': 'SKIPPED',
                'language':     language,
            }

        # Parser env_vars_raw → dict d'env supplémentaires
        extra_env: dict = {}
        for line in env_vars_raw.splitlines():
            line = _resolve_vars(line.strip(), self.context)
            if '=' in line and not line.startswith('#'):
                k, _, v = line.partition('=')
                extra_env[k.strip()] = v.strip()

        opts = dict(fail_on_stderr=fail_on_stderr, save_output_as=save_output_as, extra_env=extra_env)
        logger.info(f'[Script] Language={language}  cwd={cwd or "(auto)"}  timeout={timeout}s')

        if language == 'python':
            return self._run_python(script, cwd, timeout, **opts)
        elif language == 'ansible':
            return self._run_ansible(script, cwd, timeout)
        else:
            return self._run_shell(script, cwd, timeout, **opts)

    # ─────────────────────────────────────────────────────────────────────────
    # Shell
    # ─────────────────────────────────────────────────────────────────────────

    def _run_shell(self, script: str, cwd, timeout: int,
                   extra_env: dict = None, fail_on_stderr: bool = False,
                   save_output_as: str = '') -> dict:
        env = os.environ.copy()
        env.pop('DJANGO_SETTINGS_MODULE', None)
        python_dir = os.path.dirname(sys.executable)
        env['PATH'] = python_dir + os.pathsep + env.get('PATH', '')
        if cwd:
            env['REPO_PATH'] = str(cwd)
        if extra_env:
            env.update(extra_env)

        cmd = ['cmd', '/c', script] if sys.platform == 'win32' else ['/bin/bash', '-c', script]
        t0  = time.time()

        try:
            proc = subprocess.run(
                cmd, cwd=cwd, capture_output=True, text=True,
                timeout=timeout, encoding='utf-8', errors='replace', env=env,
            )
            duration = round(time.time() - t0, 2)
            ok = proc.returncode == 0

            if fail_on_stderr and proc.stderr.strip():
                raise RuntimeError(
                    f'[Script/Shell] stderr non vide (fail_on_stderr=true):\n{proc.stderr[:500]}'
                )

            logger.info(f'[Script/Shell] returncode={proc.returncode}  ok={ok}  {duration}s')

            result = {
                'returncode':    proc.returncode,
                'stdout':        _truncate(proc.stdout),
                'stderr':        _truncate(proc.stderr, 1000),
                'language':      'shell',
                'build_status':  'SUCCESS' if ok else 'FAILED',
                'test_duration': duration,
            }
            if save_output_as:
                result[save_output_as] = proc.stdout.strip()
            result.update(self._parse_test_output(proc.stdout, proc.stderr))
            return result

        except subprocess.TimeoutExpired:
            raise RuntimeError(f'Script Shell interrompu après {timeout}s (timeout).')

    # ─────────────────────────────────────────────────────────────────────────
    # Python
    # ─────────────────────────────────────────────────────────────────────────

    def _run_python(self, script: str, cwd, timeout: int,
                    extra_env: dict = None, fail_on_stderr: bool = False,
                    save_output_as: str = '') -> dict:
        tmp_file = None
        try:
            with tempfile.NamedTemporaryFile(
                mode='w', suffix='.py', delete=False, encoding='utf-8'
            ) as f:
                f.write(script)
                tmp_file = f.name

            env = os.environ.copy()
            env.pop('DJANGO_SETTINGS_MODULE', None)
            if cwd:
                env['REPO_PATH'] = str(cwd)
            if extra_env:
                env.update(extra_env)

            t0   = time.time()
            proc = subprocess.run(
                [sys.executable, tmp_file], cwd=cwd, capture_output=True,
                text=True, timeout=timeout, env=env,
            )
            duration = round(time.time() - t0, 2)
            ok = proc.returncode == 0

            if fail_on_stderr and proc.stderr.strip():
                raise RuntimeError(
                    f'[Script/Python] stderr non vide (fail_on_stderr=true):\n{proc.stderr[:500]}'
                )

            result = {
                'returncode':    proc.returncode,
                'stdout':        _truncate(proc.stdout),
                'stderr':        _truncate(proc.stderr, 1000),
                'language':      'python',
                'build_status':  'SUCCESS' if ok else 'FAILED',
                'test_duration': duration,
            }
            if save_output_as:
                result[save_output_as] = proc.stdout.strip()
            result.update(self._parse_test_output(proc.stdout, proc.stderr))
            return result

        except subprocess.TimeoutExpired:
            raise RuntimeError(f'Script Python interrompu après {timeout}s (timeout).')
        finally:
            if tmp_file and os.path.exists(tmp_file):
                try:
                    os.unlink(tmp_file)
                except OSError:
                    pass

    # ─────────────────────────────────────────────────────────────────────────
    # Ansible (via ansible-playbook CLI)
    # ─────────────────────────────────────────────────────────────────────────

    def _run_ansible(self, script: str, cwd, timeout: int) -> dict:
        """
        Écrit le contenu YAML dans un fichier temporaire et l'exécute
        via `ansible-playbook`. Ansible doit être installé sur le serveur.
        """
        tmp_file = None
        try:
            with tempfile.NamedTemporaryFile(
                mode='w', suffix='.yml', delete=False, encoding='utf-8'
            ) as f:
                f.write(script)
                tmp_file = f.name

            cmd = ['ansible-playbook', tmp_file]
            t0  = time.time()
            proc = subprocess.run(
                cmd, cwd=cwd, capture_output=True, text=True,
                timeout=timeout, encoding='utf-8', errors='replace',
            )
            duration = round(time.time() - t0, 2)
            ok = proc.returncode == 0

            return {
                'returncode':    proc.returncode,
                'stdout':        _truncate(proc.stdout),
                'stderr':        _truncate(proc.stderr, 1000),
                'language':      'ansible',
                'build_status':  'SUCCESS' if ok else 'FAILED',
                'test_duration': duration,
            }
        except subprocess.TimeoutExpired:
            raise RuntimeError(f'Ansible interrompu après {timeout}s (timeout).')
        except FileNotFoundError:
            raise RuntimeError(
                '[Script/Ansible] ansible-playbook introuvable. '
                'Installez Ansible sur le serveur ou choisissez shell/python.'
            )
        finally:
            if tmp_file and os.path.exists(tmp_file):
                try:
                    os.unlink(tmp_file)
                except OSError:
                    pass

    # ─────────────────────────────────────────────────────────────────────────
    # Parser des résultats de tests
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_test_output(stdout: str, stderr: str) -> dict:
        combined = stdout + '\n' + stderr

        # Django test runner
        ran = re.search(r'Ran\s+(\d+)\s+tests?\s+in\s+([\d.]+)s', combined, re.IGNORECASE)
        if ran:
            total    = int(ran.group(1))
            duration = float(ran.group(2))
            fail_m   = re.search(
                r'FAILED\s*\((?:failures?=(\d+))?(?:,?\s*errors?=(\d+))?\)',
                combined, re.IGNORECASE
            )
            if fail_m:
                failed = int(fail_m.group(1) or 0) + int(fail_m.group(2) or 0)
                return {'tests_passed': max(0, total - failed), 'tests_failed': failed,
                        'test_duration': duration, 'build_status': 'FAILED'}
            skipped = re.search(r'OK\s*\(skipped=(\d+)\)', combined, re.IGNORECASE)
            skipped_n = int(skipped.group(1)) if skipped else 0
            return {'tests_passed': total - skipped_n, 'tests_failed': 0,
                    'test_duration': duration, 'build_status': 'SUCCESS'}

        # pytest
        pm = re.search(r'(\d+)\s+passed(?:,\s*(\d+)\s+failed)?(?:\s+in\s+([\d.]+)s)?',
                       combined, re.IGNORECASE)
        if pm:
            passed = int(pm.group(1)); failed = int(pm.group(2) or 0)
            return {'tests_passed': passed, 'tests_failed': failed,
                    'test_duration': float(pm.group(3) or 0),
                    'build_status': 'FAILED' if failed > 0 else 'SUCCESS'}

        return {'tests_passed': 0, 'tests_failed': 0, 'test_duration': 0}
