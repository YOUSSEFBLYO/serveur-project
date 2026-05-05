"""
Generate Report — Executor isolé.
Assemble un rapport JSON depuis toutes les sorties précédentes du pipeline.

Compatible avec le pipeline Django CI :
  GitLabPull → ScriptTask (manage.py test) → GenerateReport → SendEmail

Outputs: report_json, overall
"""
import json
import logging
import time
from datetime import datetime, timezone

from .base import BaseExecutor

logger = logging.getLogger(__name__)


class GenerateReportExecutor(BaseExecutor):
    """
    report.Generate — Construit un rapport structuré à partir du contexte d'exécution.
    Compatible avec ScriptTaskExecutor (sorties Django: tests_passed, tests_failed, etc.)
    """

    def run(self) -> dict:
        time.sleep(0.5)

        # ── Métriques de tests ────────────────────────────────────────────────
        tests_passed  = int(self.ctx('tests_passed',  0))
        tests_failed  = int(self.ctx('tests_failed',  0))
        coverage_pct  = float(self.ctx('coverage_pct',  0.0))
        test_duration = float(self.ctx('test_duration', 0.0))

        # ── Métriques d'exécution ─────────────────────────────────────────────
        build_status  = self.ctx('build_status', None)
        returncode    = self.ctx('returncode',   None)
        stdout        = self.ctx('stdout',       '')
        stderr        = self.ctx('stderr',       '')

        # Déduire build_status depuis le returncode si non disponible
        if build_status is None:
            if returncode is not None:
                build_status = 'SUCCESS' if int(returncode) == 0 else 'FAILED'
            else:
                build_status = 'SUCCESS'

        # ── Statut global ─────────────────────────────────────────────────────
        # FAILED si : des tests ont échoué OU le script a échoué
        overall = 'FAILED' if (tests_failed > 0 or build_status == 'FAILED') else 'SUCCESS'

        # ── Source (depuis GitLabPull) ─────────────────────────────────────────
        commit_sha = str(self.ctx('commit_sha', 'unknown'))
        report = {
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'overall':      overall,
            'source': {
                'commit':  commit_sha[:8] if commit_sha != 'unknown' else 'unknown',
                'author':  self.ctx('author',     'System'),
                'message': self.ctx('commit_msg', '—'),
                'branch':  self.ctx('branch',     'main'),
            },
            'tests': {
                'passed':   tests_passed,
                'failed':   tests_failed,
                'duration': round(test_duration, 2),
                'coverage': round(coverage_pct, 1),
                'output':   str(stdout)[:800] if stdout else str(stderr)[:800],
            },
            'build': {
                'status':      build_status,
                'returncode':  returncode,
                'total_tests': tests_passed + tests_failed,
            },
        }

        logger.info(
            f'[GenerateReport] overall={overall}  '
            f'tests={tests_passed}✓/{tests_failed}✗  '
            f'duration={test_duration:.1f}s'
        )

        return {
            'report_json': json.dumps(report),
            'overall':     overall,
        }
