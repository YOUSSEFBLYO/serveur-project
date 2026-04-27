"""
CI/CD Pipeline executors.
Implements the GitLab → Build → Test → Teams workflow.

Each executor operates in two modes:
  • Real mode   — when real credentials/config are present
  • Simulation  — when config is missing / demo environment

Variables flow through the shared `context` dict accumulated
by execute_workflow_task across all nodes of the same execution.
"""
import json
import logging
import os
import platform
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse

import requests

from decouple import config
from .base import BaseExecutor

logger = logging.getLogger(__name__)

# ─── helpers ──────────────────────────────────────────────────────────────────

def _is_windows() -> bool:
    return platform.system() == 'Windows'


def _run_cmd(cmd: list, cwd: str = None, timeout: int = 120) -> subprocess.CompletedProcess:
    """Run a command and return the CompletedProcess.  Raises on non-zero exit."""
    logger.info(f'[CMD] {" ".join(cmd)}  (cwd={cwd})')
    result = subprocess.run(
        cmd, cwd=cwd,
        capture_output=True, text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr[:1000] or result.stdout[:500])
    return result


def _dir_size_mb(path: str) -> str:
    """Return human-readable directory size."""
    try:
        total = sum(
            os.path.getsize(os.path.join(dp, f))
            for dp, _, files in os.walk(path)
            for f in files
        )
        return f'{round(total / 1024 / 1024, 2)} MB'
    except Exception:
        return '? MB'

def _build_auth_url(url: str, token: str, username: str = '') -> str:
    """Build a Git clone URL with token authentication."""
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        return url

    netloc = parsed.netloc
    if '@' in netloc:
        netloc = netloc.split('@')[-1]

    if token:
        user = username.strip() or 'oauth2'
        netloc = f'{user}:{token}@{netloc}'

    return urlunparse(parsed._replace(netloc=netloc))

# ══════════════════════════════════════════════════════════════════════════════
# 1. Git Pull
# ══════════════════════════════════════════════════════════════════════════════

class GitPullExecutor(BaseExecutor):
    """
    cicd.GitPull — clone a GitLab repo (or any git repo) using OAuth2.
    Outputs: commit_sha, repo_path, author, commit_msg
    """

    def run(self) -> dict:
        url            = self.ctx('gitlabUrl',    '') or self.ctx('gitlab_url', '')
        branch         = self.ctx('branch',       'main')
        token          = self.ctx('projectToken', '') or self.ctx('project_token', '')
        token_username = self.ctx('tokenUsername', '') or self.ctx('token_username', '')

        if url:
            return self._real_pull(url, branch, token, token_username)
        else:
            return self._simulate(branch)

    def _real_pull(self, url: str, branch: str, token: str, token_username: str) -> dict:
        auth_url = _build_auth_url(url, token, token_username)
        repo_path = tempfile.mkdtemp(prefix='kraken_cicd_')
        logger.info(f'[GitPull] Cloning {url} branch={branch} → {repo_path}')

        try:
            _run_cmd(['git', 'clone', '--branch', branch, '--depth', '1', auth_url, repo_path])
        except RuntimeError as exc:
            if token and token_username and token_username.lower() != 'oauth2':
                logger.warning('[GitPull] clone failed with username=%s, retrying oauth2 fallback', token_username)
                auth_url = _build_auth_url(url, token, 'oauth2')
                _run_cmd(['git', 'clone', '--branch', branch, '--depth', '1', auth_url, repo_path])
            else:
                raise RuntimeError(
                    'Git clone failed. Vérifiez l’URL, le token et le nom d’utilisateur du token. '
                    'Si vous utilisez un personal access token, utilisez tokenUsername = oauth2. '
                    f'Détail: {exc}'
                )

        sha = subprocess.check_output(
            ['git', '-C', repo_path, 'rev-parse', 'HEAD']
        ).decode().strip()
        author = subprocess.check_output(
            ['git', '-C', repo_path, 'log', '-1', '--format=%an']
        ).decode().strip()
        msg = subprocess.check_output(
            ['git', '-C', repo_path, 'log', '-1', '--format=%s']
        ).decode().strip()

        return {
            'commit_sha': sha,
            'repo_path':  repo_path,
            'author':     author,
            'commit_msg': msg,
        }

    def _simulate(self, branch: str) -> dict:
        """Create a minimal Node.js project in a temp dir for downstream tasks."""
        logger.info(f'[GitPull SIMULATE] branch={branch}')
        time.sleep(1.5)

        repo_path = tempfile.mkdtemp(prefix='kraken_sim_')
        # Write minimal package.json
        pkg = {
            'name': 'kraken-demo-project',
            'version': '1.0.0',
            'scripts': {
                'build': 'node -e "require(\'fs\').mkdirSync(\'dist\',{recursive:true});require(\'fs\').writeFileSync(\'dist/index.js\',\'console.log(42)\')"',
                'test':  'node -e "process.exit(0)"',
            },
        }
        with open(os.path.join(repo_path, 'package.json'), 'w') as f:
            json.dump(pkg, f, indent=2)

        return {
            'commit_sha': 'a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2',
            'repo_path':  repo_path,
            'author':     'Khalil Youssef',
            'commit_msg': 'feat: add pipeline support [simulation]',
            'mode':       'simulation',
        }


# ══════════════════════════════════════════════════════════════════════════════
# 2. Branch Gate
# ══════════════════════════════════════════════════════════════════════════════

class BranchGateExecutor(BaseExecutor):
    """
    cicd.BranchGate — GATE that checks the branch name.
    Raises if the branch is not main or release/*.
    """

    def run(self) -> dict:
        branch    = self.ctx('branch', 'main')
        allowed   = self.cfg('allowedBranches', 'main,release/')
        patterns  = [p.strip() for p in allowed.split(',')]

        ok = any(branch == p or branch.startswith(p) for p in patterns)
        logger.info(f'[BranchGate] branch={branch} allowed={patterns} → {"OK" if ok else "BLOCKED"}')

        if not ok:
            raise RuntimeError(
                f'Branch "{branch}" is not allowed. Permitted: {allowed}'
            )

        time.sleep(0.3)
        return {'gate_passed': True, 'checked_branch': branch}


# ══════════════════════════════════════════════════════════════════════════════
# 3. NPM Install
# ══════════════════════════════════════════════════════════════════════════════

class NpmInstallExecutor(BaseExecutor):
    """
    cicd.NpmInstall — run `npm install` in the cloned repository.
    Requires repo_path from context (set by GitPullExecutor).
    """

    def run(self) -> dict:
        repo = self.ctx('repo_path', '')
        if not repo or not os.path.isdir(repo):
            logger.info('[NpmInstall SIMULATE] no repo_path in context')
            time.sleep(2)
            return {'install_status': 'SIMULATED', 'mode': 'simulation'}

        npm = 'npm.cmd' if _is_windows() else 'npm'
        logger.info(f'[NpmInstall] npm install in {repo}')
        _run_cmd([npm, 'install', '--prefer-offline'], cwd=repo, timeout=180)
        return {'install_status': 'OK'}


# ══════════════════════════════════════════════════════════════════════════════
# 4. NPM Build
# ══════════════════════════════════════════════════════════════════════════════

class NpmBuildExecutor(BaseExecutor):
    """
    cicd.NpmBuild — run `npm run build`.
    Outputs: build_status, artifact_path, build_duration, build_size
    """

    def run(self) -> dict:
        repo = self.ctx('repo_path', '')
        if not repo or not os.path.isdir(repo):
            logger.info('[NpmBuild SIMULATE] no repo_path')
            time.sleep(2)
            return {
                'build_status':   'SUCCESS',
                'artifact_path':  '/tmp/kraken_sim/dist',
                'build_duration': 12.4,
                'build_size':     '2.31 MB',
                'mode':           'simulation',
            }

        npm   = 'npm.cmd' if _is_windows() else 'npm'
        start = time.time()
        logger.info(f'[NpmBuild] npm run build in {repo}')
        _run_cmd([npm, 'run', 'build'], cwd=repo, timeout=300)

        duration     = round(time.time() - start, 2)
        artifact_dir = os.path.join(repo, 'dist')
        size         = _dir_size_mb(artifact_dir) if os.path.isdir(artifact_dir) else '? MB'

        return {
            'build_status':   'SUCCESS',
            'artifact_path':  artifact_dir,
            'build_duration': duration,
            'build_size':     size,
        }


# ══════════════════════════════════════════════════════════════════════════════
# 5. NPM Test
# ══════════════════════════════════════════════════════════════════════════════

class NpmTestExecutor(BaseExecutor):
    """
    cicd.NpmTest — run `npm test`.
    Parses Jest JSON output when available.
    Outputs: tests_passed, tests_failed, test_duration
    """

    def run(self) -> dict:
        repo = self.ctx('repo_path', '')
        if not repo or not os.path.isdir(repo):
            logger.info('[NpmTest SIMULATE] no repo_path')
            time.sleep(2)
            return {
                'tests_passed':  42,
                'tests_failed':  0,
                'test_duration': 8.7,
                'mode':          'simulation',
            }

        npm   = 'npm.cmd' if _is_windows() else 'npm'
        start = time.time()
        logger.info(f'[NpmTest] npm test in {repo}')

        result = subprocess.run(
            [npm, 'test', '--', '--json', '--passWithNoTests'],
            cwd=repo, capture_output=True, text=True, timeout=180,
        )
        duration = round(time.time() - start, 2)

        try:
            report = json.loads(result.stdout)
            passed = report.get('numPassedTests', 0)
            failed = report.get('numFailedTests', 0)
        except (json.JSONDecodeError, ValueError):
            if result.returncode != 0:
                raise RuntimeError(f'Tests failed:\n{result.stderr[:800]}')
            passed, failed = 0, 0

        if failed > 0:
            raise RuntimeError(f'{failed} test(s) failed')

        return {
            'tests_passed':  passed,
            'tests_failed':  failed,
            'test_duration': duration,
        }


# ══════════════════════════════════════════════════════════════════════════════
# 6. NPM Coverage
# ══════════════════════════════════════════════════════════════════════════════

class NpmCoverageExecutor(BaseExecutor):
    """
    cicd.NpmCoverage — run Jest coverage report.
    Outputs: coverage_pct, report_url
    abortOnFailure is typically false for coverage.
    """

    def run(self) -> dict:
        repo = self.ctx('repo_path', '')
        if not repo or not os.path.isdir(repo):
            logger.info('[NpmCoverage SIMULATE]')
            time.sleep(1.5)
            return {
                'coverage_pct': 87.4,
                'report_url':   'http://localhost/coverage/index.html',
                'mode':         'simulation',
            }

        npm = 'npm.cmd' if _is_windows() else 'npm'
        logger.info(f'[NpmCoverage] jest --coverage in {repo}')

        result = subprocess.run(
            [npm, 'test', '--', '--coverage', '--json', '--passWithNoTests'],
            cwd=repo, capture_output=True, text=True, timeout=240,
        )

        pct = 0.0
        try:
            data    = json.loads(result.stdout)
            summary = data.get('coverageMap', {})
            # Aggregate statement coverage across files
            totals  = [v.get('s', {}) for v in summary.values()]
            covered = sum(sum(1 for x in d.values() if x > 0) for d in totals)
            total   = sum(len(d) for d in totals)
            pct     = round(covered / total * 100, 1) if total else 0.0
        except Exception:
            pct = 0.0

        report_dir = os.path.join(repo, 'coverage', 'lcov-report', 'index.html')
        return {
            'coverage_pct': pct,
            'report_url':   report_dir if os.path.exists(report_dir) else '',
        }


# ══════════════════════════════════════════════════════════════════════════════
# 7. Generate Report
# ══════════════════════════════════════════════════════════════════════════════

class GenerateReportExecutor(BaseExecutor):
    """
    cicd.GenerateReport — assembles a JSON report from all previous outputs.
    Outputs: report_json (serialized string for the Teams card)
    """

    def run(self) -> dict:
        import time as _time
        _time.sleep(0.5)

        # ── Collect metrics — works with ScriptTask OR cicd.* nodes ───────────
        tests_passed  = self.ctx('tests_passed',  0)
        tests_failed  = self.ctx('tests_failed',  0)
        coverage_pct  = self.ctx('coverage_pct',  0)
        test_duration = self.ctx('test_duration', 0)
        build_status  = self.ctx('build_status',  None)
        build_duration = self.ctx('build_duration', 0)
        build_size    = self.ctx('build_size',    '?')

        # If ScriptTask was used instead of cicd nodes, evaluate from returncode
        returncode    = self.ctx('returncode', None)
        stdout        = self.ctx('stdout', '')
        if build_status is None:
            if returncode is not None:
                build_status = 'SUCCESS' if int(returncode) == 0 else 'FAILED'
            else:
                build_status = 'SUCCESS'

        # Determine overall status
        if int(tests_failed) > 0 or build_status == 'FAILED':
            overall = 'FAILED'
        else:
            overall = 'SUCCESS'

        report = {
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'overall':      overall,
            'source': {
                'commit':  str(self.ctx('commit_sha', 'unknown'))[:8],
                'author':  self.ctx('author',     'System'),
                'message': self.ctx('commit_msg', '—'),
            },
            'build': {
                'status':   build_status,
                'duration': build_duration,
                'size':     build_size,
                'stdout':   str(stdout)[:500] if stdout else '',
            },
            'tests': {
                'passed':   tests_passed,
                'failed':   tests_failed,
                'duration': test_duration,
                'coverage': coverage_pct,
                'report':   self.ctx('report_url', ''),
            },
        }

        logger.info(f'[GenerateReport] overall={overall}')
        return {
            'report_json': json.dumps(report),
            'overall':     overall,
        }


# ══════════════════════════════════════════════════════════════════════════════
# 8. Send Teams Report
# ══════════════════════════════════════════════════════════════════════════════

class SendTeamsReportExecutor(BaseExecutor):
    """
    cicd.SendTeams — post a rich Adaptive Card to Teams with pipeline results.
    Uses teams_webhook from context or node config.
    Falls back to simulation if no webhook is set.
    """

    def run(self) -> dict:
        webhook     = self.ctx('teamsWebhook', '') or self.ctx('teams_webhook', '')
        report_json = self.ctx('report_json', '{}')

        try:
            report = json.loads(report_json)
        except json.JSONDecodeError:
            report = {}

        overall = report.get('overall', 'UNKNOWN')
        emoji   = '✅' if overall == 'SUCCESS' else '❌'

        card = self._build_adaptive_card(report, overall, emoji)

        if not webhook:
            logger.info(f'[SendTeams SIMULATE] {overall} — no webhook configured')
            time.sleep(1)
            return {
                'sent':    False,
                'mode':    'simulation',
                'overall': overall,
                'card_preview': str(card)[:200],
            }

        logger.info(f'[SendTeams] Posting {overall} card to Teams')
        resp = requests.post(webhook, json=card, timeout=15)
        resp.raise_for_status()
        return {
            'sent':        True,
            'http_status': resp.status_code,
            'overall':     overall,
        }

    @staticmethod
    def _build_adaptive_card(report: dict, overall: str, emoji: str) -> dict:
        src   = report.get('source', {})
        build = report.get('build', {})
        tests = report.get('tests', {})

        facts = [
            {'title': 'Commit',    'value': src.get('commit', '?')},
            {'title': 'Auteur',    'value': src.get('author', '?')},
            {'title': 'Message',   'value': src.get('message', '?')},
            {'title': 'Build',     'value': f"{build.get('status','?')} ({build.get('duration',0)}s — {build.get('size','?')})"},
            {'title': 'Tests',     'value': f"{tests.get('passed',0)} ✓  {tests.get('failed',0)} ✗  ({tests.get('duration',0)}s)"},
            {'title': 'Coverage',  'value': f"{tests.get('coverage',0)}%"},
            {'title': 'Généré à',  'value': report.get('generated_at', '?')},
        ]

        return {
            'type': 'message',
            'attachments': [{
                'contentType': 'application/vnd.microsoft.card.adaptive',
                'content': {
                    '$schema': 'http://adaptivecards.io/schemas/adaptive-card.json',
                    'type':    'AdaptiveCard',
                    'version': '1.4',
                    'body': [
                        {
                            'type':    'TextBlock',
                            'text':    f'{emoji} Kraken Pipeline — {overall}',
                            'size':    'Large',
                            'weight':  'Bolder',
                            'color':   'Good' if overall == 'SUCCESS' else 'Attention',
                        },
                        {
                            'type':  'FactSet',
                            'facts': facts,
                        },
                    ],
                },
            }],
        }

# ══════════════════════════════════════════════════════════════════════════════
# 9. Send Email Formatted Report (via Resend.com — HTTPS port 443)
# ══════════════════════════════════════════════════════════════════════════════

class SendEmailReportExecutor(BaseExecutor):
    """
    cicd.SendEmail — Envoie le rapport HTML via l'API Resend (HTTPS).
    
    Cette méthode contourne les blocages SMTP (ports 465/587) fréquents sur 
    certains réseaux en utilisant une requête HTTP sur le port 443.
    """

    def run(self) -> dict:
        from django.conf import settings
        import json

        # 1. Récupération du destinataire
        recipient = (
            self.ctx('recipientEmail', '')
            or self.ctx('recipient_email', '')
            or getattr(settings, 'RESEND_TO_EMAIL', '')
            or config('RESEND_TO_EMAIL', default='')
        )
        if not recipient:
            recipient = 'admin@kraken.local'

        subject     = self.ctx('subject', "Rapport d'exécution Kraken CI/CD")
        report_json = self.ctx('report_json', '{}')

        try:
            report = json.loads(report_json)
        except json.JSONDecodeError:
            report = {}

        overall  = report.get('overall', 'UNKNOWN')
        src      = report.get('source', {})
        build    = report.get('build', {})
        tests    = report.get('tests', {})
        gen_at   = report.get('generated_at', '?')

        emoji     = '✅' if overall == 'SUCCESS' else '❌'
        color     = '#16a34a' if overall == 'SUCCESS' else '#dc2626'
        bg_badge  = '#dcfce7' if overall == 'SUCCESS' else '#fee2e2'
        bld_color = '#16a34a' if build.get('status') == 'SUCCESS' else '#dc2626'

        # ── Plain-text fallback ───────────────────────────────────────────────
        plain = (
            f"Kraken Pipeline — {overall}\n"
            f"{'=' * 40}\n\n"
            f"Source:\n"
            f"  Commit  : {src.get('commit', '?')}\n"
            f"  Auteur  : {src.get('author', '?')}\n"
            f"  Message : {src.get('message', '?')}\n\n"
            f"Build:\n"
            f"  Status  : {build.get('status', '?')}\n"
            f"  Durée   : {build.get('duration', 0)}s\n"
            f"  Taille  : {build.get('size', '?')}\n\n"
            f"Tests:\n"
            f"  Passed  : {tests.get('passed', 0)}\n"
            f"  Failed  : {tests.get('failed', 0)}\n"
            f"  Durée   : {tests.get('duration', 0)}s\n"
            f"  Coverage: {tests.get('coverage', 0)}%\n\n"
            f"Généré le : {gen_at}\n"
        )

        # ── HTML email body ───────────────────────────────────────────────────
        html = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'></head>"
            "<body style='margin:0;padding:0;background:#f1f5f9;font-family:Arial,sans-serif;'>"
            "<table width='100%' cellpadding='0' cellspacing='0' style='background:#f1f5f9;padding:32px 0;'>"
            "<tr><td align='center'>"
            "<table width='600' cellpadding='0' cellspacing='0' style='background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.10);'>"
            # Header
            "<tr><td style='background:linear-gradient(135deg,#1e1b4b 0%,#312e81 100%);padding:28px 32px;text-align:center;'>"
            "<span style='font-size:28px;'>🐙</span>"
            "<h1 style='color:#fff;margin:8px 0 4px;font-size:22px;font-weight:700;'>Kraken Workflow Engine</h1>"
            "<p style='color:#a5b4fc;margin:0;font-size:13px;'>CI/CD Pipeline Report</p>"
            "</td></tr>"
            # Status badge
            "<tr><td style='padding:24px 32px 0;text-align:center;'>"
            f"<span style='display:inline-block;background:{bg_badge};color:{color};border:1.5px solid {color};border-radius:999px;padding:6px 24px;font-size:16px;font-weight:700;'>{emoji} {overall}</span>"
            "</td></tr>"
            # Source section
            "<tr><td style='padding:24px 32px 0;'>"
            "<h2 style='color:#1e293b;font-size:14px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;margin:0 0 12px;border-bottom:2px solid #e2e8f0;padding-bottom:8px;'>📦 Source</h2>"
            "<table width='100%' cellpadding='0' cellspacing='0'>"
            f"<tr><td style='color:#64748b;font-size:13px;padding:4px 0;width:100px;'>Commit</td><td style='color:#1e293b;font-size:13px;font-weight:600;font-family:monospace;'>{src.get('commit','?')}</td></tr>"
            f"<tr><td style='color:#64748b;font-size:13px;padding:4px 0;'>Auteur</td><td style='color:#1e293b;font-size:13px;'>{src.get('author','?')}</td></tr>"
            f"<tr><td style='color:#64748b;font-size:13px;padding:4px 0;'>Message</td><td style='color:#1e293b;font-size:13px;font-style:italic;'>{src.get('message','?')}</td></tr>"
            "</table></td></tr>"
            # Build section
            "<tr><td style='padding:20px 32px 0;'>"
            "<h2 style='color:#1e293b;font-size:14px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;margin:0 0 12px;border-bottom:2px solid #e2e8f0;padding-bottom:8px;'>🏗️ Build</h2>"
            "<table width='100%' cellpadding='6' cellspacing='0' style='background:#f8fafc;border-radius:8px;'>"
            f"<tr><td style='color:#64748b;font-size:13px;width:120px;'>Status</td><td style='font-size:13px;font-weight:700;color:{bld_color};'>{build.get('status','?')}</td></tr>"
            f"<tr><td style='color:#64748b;font-size:13px;'>Durée</td><td style='color:#1e293b;font-size:13px;'>{build.get('duration',0)}s</td></tr>"
            f"<tr><td style='color:#64748b;font-size:13px;'>Taille artefact</td><td style='color:#1e293b;font-size:13px;'>{build.get('size','?')}</td></tr>"
            "</table></td></tr>"
            # Tests section
            "<tr><td style='padding:20px 32px 0;'>"
            "<h2 style='color:#1e293b;font-size:14px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;margin:0 0 12px;border-bottom:2px solid #e2e8f0;padding-bottom:8px;'>🧪 Tests</h2>"
            "<table width='100%' cellpadding='0' cellspacing='0'><tr>"
            "<td style='width:33%;text-align:center;background:#f0fdf4;border-radius:8px;padding:12px;'>"
            f"<div style='font-size:24px;font-weight:700;color:#16a34a;'>{tests.get('passed',0)}</div><div style='font-size:11px;color:#64748b;margin-top:2px;'>Passed</div></td>"
            "<td style='width:4px;'></td>"
            "<td style='width:33%;text-align:center;background:#fef2f2;border-radius:8px;padding:12px;'>"
            f"<div style='font-size:24px;font-weight:700;color:#dc2626;'>{tests.get('failed',0)}</div><div style='font-size:11px;color:#64748b;margin-top:2px;'>Failed</div></td>"
            "<td style='width:4px;'></td>"
            "<td style='width:33%;text-align:center;background:#eff6ff;border-radius:8px;padding:12px;'>"
            f"<div style='font-size:24px;font-weight:700;color:#2563eb;'>{tests.get('coverage',0)}%</div><div style='font-size:11px;color:#64748b;margin-top:2px;'>Coverage</div></td>"
            "</tr></table>"
            f"<p style='color:#94a3b8;font-size:12px;margin:8px 0 0;text-align:right;'>Durée : {tests.get('duration',0)}s</p>"
            "</td></tr>"
            # Footer
            "<tr><td style='padding:24px 32px 28px;text-align:center;border-top:1px solid #e2e8f0;'>"
            f"<p style='color:#94a3b8;font-size:11px;margin:0;'>Généré le {gen_at} par <strong>Kraken Workflow Engine</strong></p>"
            "</td></tr>"
            "</table></td></tr></table></body></html>"
        )

        # ── Configuration Resend ─────────────────────────────────────────────
        api_key   = getattr(settings, 'RESEND_API_KEY', '') or config('RESEND_API_KEY', default='')
        from_addr = getattr(settings, 'RESEND_FROM_EMAIL', '') or config('RESEND_FROM_EMAIL', default='onboarding@resend.dev')

        logger.info(f'[SendEmail] Envoi rapport {overall} vers {recipient} via Resend API')

        if not api_key or 'REMPLACE' in api_key:
            logger.warning('[SendEmail] RESEND_API_KEY non configurée. Passage en mode simulation.')
            time.sleep(0.5)
            return {'sent': False, 'mode': 'simulation', 'overall': overall}

        try:
            resp = requests.post(
                'https://api.resend.com/emails',
                headers={
                    'Authorization': f'Bearer {api_key}',
                    'Content-Type':  'application/json',
                },
                json={
                    'from':    from_addr,
                    'to':      [recipient],
                    'subject': f'{emoji} {subject} — {overall}',
                    'html':    html,
                    'text':    plain,
                },
                timeout=20,
            )
            resp.raise_for_status()
            resend_id = resp.json().get('id', '')
            logger.info(f'[SendEmail] Envoyé avec succès ! ID Resend: {resend_id}')
            return {'sent': True, 'recipient': recipient, 'overall': overall, 'resend_id': resend_id}
        except Exception as exc:
            logger.warning(f'[SendEmail] Erreur API Resend: {exc}')
            return {'sent': False, 'mode': 'error', 'error': str(exc)[:300]}

