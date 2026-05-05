"""
Rollback Git Revert — Executor.

Effectue un revert Git du dernier commit (ou d'un commit spécifique)
sur le dépôt cible, puis pousse le commit de revert sur la branche.
Utile pour annuler rapidement un déploiement défectueux.
"""
import logging
import os
import subprocess
import tempfile
from urllib.parse import urlparse, urlunparse

from .base import BaseExecutor

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 120


def _run_git(cmd: list, cwd: str | None = None, timeout: int = _GIT_TIMEOUT) -> str:
    """Exécute une commande git et retourne stdout."""
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd
    )
    if result.returncode != 0:
        error = (result.stderr or result.stdout or 'Erreur git inconnue').strip()
        raise RuntimeError(error[:1000])
    return result.stdout.strip()


def _inject_credentials(url: str, token: str, username: str = 'oauth2') -> str:
    parsed = urlparse(url)
    host   = parsed.netloc
    if '@' in host:
        host = host.split('@', 1)[-1]
    netloc = f'{username}:{token}@{host}'
    return urlunparse(parsed._replace(netloc=netloc))


class RollbackGitRevertExecutor(BaseExecutor):
    """
    deploy.Rollback — Revert Git du dernier commit (ou d'un SHA précis).

    Config du nœud :
        repoUrl      : str  — URL du dépôt (héritée du contexte si non fourni)
        branch       : str  — Branche cible (héritée du contexte)
        commitSha    : str  — SHA du commit à reverter (vide = HEAD)
        token        : str  — Token d'authentification Git
        tokenUser    : str  — Username du token (défaut: oauth2)
        authorName   : str  — Nom de l'auteur du commit de revert
        authorEmail  : str  — Email de l'auteur
        noCommit     : bool — Prépare le revert sans créer de commit
    """

    def run(self) -> dict:
        repo_url    = (
            self.cfg('repoUrl', '').strip()
            or self.ctx('trigger_repo', '').strip()
        )
        branch      = (
            self.cfg('branch', '').strip()
            or self.ctx('trigger_branch', 'main').strip()
        )
        commit_sha  = (
            self.cfg('commitSha', '').strip()
            or self.ctx('commit_sha', 'HEAD').strip()
        )
        token       = self.cfg('token', '').strip()
        token_user  = self.cfg('tokenUser', 'oauth2').strip() or 'oauth2'
        author_name = self.cfg('authorName', 'Workflow Bot').strip()
        author_email= self.cfg('authorEmail', 'bot@workflow.local').strip()
        no_commit   = self.cfg('noCommit', False)

        if not repo_url:
            raise RuntimeError(
                "[Rollback] Aucune URL de dépôt trouvée.\n"
                "Configurez 'repoUrl' ou connectez ce nœud à un Déclencheur Git."
            )

        logger.info(
            f'[Rollback] Revert de {commit_sha} sur {repo_url} '
            f'— branche={branch}  auteur={author_name}'
        )

        work_dir = tempfile.mkdtemp(prefix='wf_rollback_')

        # Construire l'URL authentifiée
        clone_url = _inject_credentials(repo_url, token, token_user) if token else repo_url

        # Clone
        _run_git([
            'git', 'clone', '--branch', branch, '--depth', '50',
            clone_url, work_dir,
        ])

        # Config auteur
        env = {**os.environ,
               'GIT_AUTHOR_NAME':     author_name,
               'GIT_AUTHOR_EMAIL':    author_email,
               'GIT_COMMITTER_NAME':  author_name,
               'GIT_COMMITTER_EMAIL': author_email}

        # Revert
        revert_cmd = ['git', 'revert', '--no-edit', commit_sha]
        if no_commit:
            revert_cmd.append('--no-commit')

        result = subprocess.run(
            revert_cmd, capture_output=True, text=True,
            timeout=_GIT_TIMEOUT, cwd=work_dir, env=env
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"[Rollback] Échec du git revert.\n"
                f"Commit: {commit_sha}\nDétail: {result.stderr.strip()[:800]}"
            )

        # Récupère le SHA du nouveau commit de revert
        revert_sha = _run_git(['git', 'rev-parse', 'HEAD'], cwd=work_dir)
        revert_msg = _run_git(
            ['git', 'log', '-1', '--format=%s'], cwd=work_dir
        )

        # Push
        push_url = _inject_credentials(repo_url, token, token_user) if token else repo_url
        _run_git(
            ['git', 'push', push_url, f'HEAD:{branch}'],
            cwd=work_dir
        )

        logger.info(
            f'[Rollback] Revert poussé avec succès — '
            f'nouveau SHA={revert_sha[:8]}  message="{revert_msg}"'
        )

        return {
            'rollback_reverted_sha': commit_sha,
            'rollback_new_sha':      revert_sha,
            'rollback_message':      revert_msg,
            'rollback_branch':       branch,
            'rollback_repo':         repo_url,
        }
