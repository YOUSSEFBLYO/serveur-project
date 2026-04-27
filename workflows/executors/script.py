"""
Script Task executor — runs an inline script in a sandboxed subprocess.
Supports Python scripts and shell scripts.

For Node.js / npm pipelines the executor:
  1. Automatically resolves cwd from context['repo_path']  (set by cicd.GitPull)
  2. Runs the script step-by-step (install → build → test)
  3. Parses test results (Jest JSON, vitest, mocha, pytest regex)
  4. Exposes the same context variables as the dedicated cicd.* executors:
       build_status, build_duration, tests_passed, tests_failed,
       test_duration, coverage_pct
     so that cicd.GenerateReport and cicd.SendEmail receive full data.
"""
import logging
import subprocess
import sys
import tempfile
import os
import json
import re
import time
from .base import BaseExecutor

logger = logging.getLogger(__name__)

# Max execution time in seconds
TIMEOUT = 300


def _npm() -> str:
    """Return the correct npm executable name for the current platform."""
    return 'npm.cmd' if sys.platform == 'win32' else 'npm'


class ScriptTaskExecutor(BaseExecutor):
    """
    xlrelease.ScriptTask — execute an inline Python/shell script.

    When the script contains npm commands the executor detects them and runs
    each step individually so it can measure durations and parse test output
    properly.  All collected metrics are returned alongside the raw stdout so
    downstream GenerateReport / SendEmail nodes work without modification.
    """

    def run(self) -> dict:
        script    = self.cfg('script', '')
        is_python = self.cfg('pythonScript', False)
        timeout   = int(self.cfg('timeout', TIMEOUT))
        cwd       = self.ctx('repo_path', None)          # provided by cicd.GitPull

        if not script.strip():
            return {'result': 'no script provided', 'stdout': '', 'returncode': 0}

        if cwd and not os.path.isdir(cwd):
            logger.warning(f'[ScriptTask] repo_path {cwd} not found — running without cwd')
            cwd = None

        if is_python:
            return self._run_python(script, cwd, timeout)

        # ── Smart npm detection ───────────────────────────────────────────────
        lower = script.lower()
        has_npm = 'npm ' in lower
        has_build = any(kw in lower for kw in ['npm run build', 'npm build'])
        has_test  = any(kw in lower for kw in ['npm test', 'npm run test'])

        if has_npm and cwd and (has_build or has_test):
            return self._run_npm_pipeline(script, cwd, timeout, has_build, has_test)

        return self._run_shell(script, cwd, timeout)

    # ── NPM Pipeline (smart step-by-step) ────────────────────────────────────
    def _run_npm_pipeline(self, script: str, cwd: str, timeout: int,
                          has_build: bool, has_test: bool) -> dict:
        """
        Run npm install / build / test separately so we can:
        - capture per-step timing
        - parse test output reliably
        - expose build_status, tests_passed, etc. into the shared context
        """
        npm = _npm()
        all_stdout = []
        result: dict = {'language': 'shell', 'returncode': 0}

        # ── 1. npm install (if script mentions it) ────────────────────────────
        lower = script.lower()
        if 'npm install' in lower or 'npm i ' in lower or lower.strip().startswith('npm i'):
            logger.info(f'[ScriptTask] npm install in {cwd}')
            try:
                proc = subprocess.run(
                    [npm, 'install', '--prefer-offline'],
                    cwd=cwd, capture_output=True, text=True, timeout=180,
                )
                all_stdout.append(proc.stdout)
                if proc.returncode != 0:
                    result.update({
                        'returncode':    proc.returncode,
                        'stdout':        '\n'.join(all_stdout)[:2000],
                        'stderr':        proc.stderr[:500],
                        'build_status':  'FAILED',
                        'install_error': proc.stderr[:200],
                    })
                    return result
            except subprocess.TimeoutExpired:
                raise RuntimeError('npm install timed out')

        # ── 2. npm run build ─────────────────────────────────────────────────
        if has_build:
            logger.info(f'[ScriptTask] npm run build in {cwd}')
            build_start = time.time()
            try:
                proc = subprocess.run(
                    [npm, 'run', 'build'],
                    cwd=cwd, capture_output=True, text=True, timeout=timeout,
                )
                build_dur = round(time.time() - build_start, 2)
                all_stdout.append(proc.stdout)
                build_ok  = proc.returncode == 0
                result['build_status']   = 'SUCCESS' if build_ok else 'FAILED'
                result['build_duration'] = build_dur

                # Try to report artifact size
                dist_dir = os.path.join(cwd, 'dist')
                if os.path.isdir(dist_dir):
                    total = sum(
                        os.path.getsize(os.path.join(dp, f))
                        for dp, _, files in os.walk(dist_dir) for f in files
                    )
                    result['build_size'] = f'{round(total / 1024 / 1024, 2)} MB'

                if not build_ok:
                    result.update({
                        'returncode': proc.returncode,
                        'stdout':     '\n'.join(all_stdout)[:2000],
                        'stderr':     proc.stderr[:500],
                    })
                    return result
            except subprocess.TimeoutExpired:
                raise RuntimeError(f'npm run build timed out after {timeout}s')
        else:
            result['build_status'] = 'SKIPPED'

        # ── 3. npm test ───────────────────────────────────────────────────────
        if has_test:
            logger.info(f'[ScriptTask] npm test in {cwd}')
            test_start = time.time()
            try:
                proc = subprocess.run(
                    [npm, 'test'],
                    cwd=cwd, capture_output=True, text=True, timeout=timeout,
                )
                test_dur = round(time.time() - test_start, 2)
                all_stdout.append(proc.stdout)
                result['test_duration'] = test_dur
                result['returncode']    = proc.returncode
                result['stderr']        = proc.stderr[:500]

                metrics = self._parse_test_output(proc.stdout, proc.stderr)
                result.update(metrics)

            except subprocess.TimeoutExpired:
                raise RuntimeError(f'npm test timed out after {timeout}s')

        result['stdout'] = '\n'.join(all_stdout)[:3000]
        logger.info(f'[ScriptTask] Pipeline done: {result}')
        return result

    # ── Python ────────────────────────────────────────────────────────────────
    def _run_python(self, script: str, cwd, timeout: int) -> dict:
        logger.info(f'[ScriptTask] Running Python script ({len(script)} chars) '
                    f'cwd={cwd or "<default>"} timeout={timeout}s')

        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.py', delete=False, encoding='utf-8'
        ) as f:
            f.write(script)
            tmp_path = f.name

        try:
            proc = subprocess.run(
                [sys.executable, tmp_path],
                cwd=cwd, capture_output=True, text=True, timeout=timeout,
            )
            return {
                'returncode': proc.returncode,
                'stdout':     proc.stdout[:2000],
                'stderr':     proc.stderr[:500],
                'language':   'python',
            }
        except subprocess.TimeoutExpired:
            raise RuntimeError(f'Script timed out after {timeout}s')
        finally:
            os.unlink(tmp_path)

    # ── Plain Shell ───────────────────────────────────────────────────────────
    def _run_shell(self, script: str, cwd, timeout: int) -> dict:
        logger.info(f'[ScriptTask] Running shell script ({len(script)} chars) '
                    f'cwd={cwd or "<default>"} timeout={timeout}s')

        if sys.platform == 'win32':
            shell_cmd = ['cmd', '/c', script]
        else:
            shell_cmd = ['/bin/bash', '-c', script]

        try:
            proc = subprocess.run(
                shell_cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
            )
            result = {
                'returncode': proc.returncode,
                'stdout':     proc.stdout[:2000],
                'stderr':     proc.stderr[:500],
                'language':   'shell',
                'build_status': 'SUCCESS' if proc.returncode == 0 else 'FAILED',
            }
            # Best-effort test parsing for shell scripts containing npm commands
            if 'npm test' in script or 'npm run test' in script:
                result.update(self._parse_test_output(proc.stdout, proc.stderr))
            return result
        except subprocess.TimeoutExpired:
            raise RuntimeError(f'Script timed out after {timeout}s')

    # ── Test output parser ────────────────────────────────────────────────────
    def _parse_test_output(self, stdout: str, stderr: str) -> dict:
        """
        Try to extract test metrics from:
          1. Jest  --json  structured output
          2. Vitest / Mocha summary lines
          3. pytest summary line
          4. Generic regex fallback
        Returns a dict with keys: tests_passed, tests_failed, coverage_pct
        """
        combined = stdout + '\n' + stderr

        # ── 1. Jest JSON ──────────────────────────────────────────────────────
        # Jest --json writes a single top-level JSON object to stdout
        try:
            data = json.loads(stdout.strip())
            if 'numPassedTests' in data or 'numFailedTests' in data:
                passed  = data.get('numPassedTests', 0)
                failed  = data.get('numFailedTests', 0)
                pct     = self._jest_coverage(data)
                logger.info(f'[ScriptTask] Jest JSON → passed={passed} failed={failed} coverage={pct}%')
                return {'tests_passed': passed, 'tests_failed': failed, 'coverage_pct': pct}
        except (json.JSONDecodeError, ValueError):
            pass

        # Sometimes Jest JSON is embedded in larger output
        json_match = re.search(r'(\{[^\n]*"numPassedTests"[^\n]*\})', combined)
        if json_match:
            try:
                data = json.loads(json_match.group(1))
                passed = data.get('numPassedTests', 0)
                failed = data.get('numFailedTests', 0)
                pct    = self._jest_coverage(data)
                return {'tests_passed': passed, 'tests_failed': failed, 'coverage_pct': pct}
            except Exception:
                pass

        # ── 2. Vitest / Mocha summary  (e.g. "14 passed | 0 failed") ──────────
        vitest_match = re.search(
            r'(\d+)\s+passed(?:\s*\|\s*(\d+)\s+failed)?', combined, re.IGNORECASE)
        if vitest_match:
            passed = int(vitest_match.group(1))
            failed = int(vitest_match.group(2)) if vitest_match.group(2) else 0
            cov    = re.search(r'(?:coverage|statements)\s*[:\|]\s*([\d.]+)%',
                               combined, re.IGNORECASE)
            return {
                'tests_passed': passed,
                'tests_failed': failed,
                'coverage_pct': float(cov.group(1)) if cov else 0.0,
            }

        # ── 3. pytest summary (e.g. "5 passed, 1 failed in 2.31s") ────────────
        pytest_match = re.search(
            r'(\d+) passed(?:,\s*(\d+) failed)?', combined, re.IGNORECASE)
        if pytest_match:
            passed = int(pytest_match.group(1))
            failed = int(pytest_match.group(2)) if pytest_match.group(2) else 0
            return {'tests_passed': passed, 'tests_failed': failed, 'coverage_pct': 0.0}

        # ── 4. Generic regex fallback ─────────────────────────────────────────
        passed_m  = re.search(r'(\d+)\s+(?:test[s]? )?passed', combined, re.IGNORECASE)
        failed_m  = re.search(r'(\d+)\s+(?:test[s]? )?failed', combined, re.IGNORECASE)
        cov_m     = re.search(r'(?:Statements|Coverage|All files)\s*[:|]\s*([\d.]+)%',
                               combined, re.IGNORECASE)
        return {
            'tests_passed': int(passed_m.group(1)) if passed_m else 0,
            'tests_failed': int(failed_m.group(1)) if failed_m else 0,
            'coverage_pct': float(cov_m.group(1)) if cov_m else 0.0,
        }

    @staticmethod
    def _jest_coverage(data: dict) -> float:
        """Compute statement coverage % from a Jest JSON report."""
        try:
            cov_map = data.get('coverageMap', {})
            totals  = [v.get('s', {}) for v in cov_map.values()]
            covered = sum(sum(1 for x in d.values() if x > 0) for d in totals)
            total   = sum(len(d) for d in totals)
            return round(covered / total * 100, 1) if total else 0.0
        except Exception:
            return 0.0

