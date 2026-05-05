"""
GitLab Pull — Executor.

Deux modes de clonage selon la configuration du nœud :

  [Projet PUBLIC]
    - Fournir uniquement `gitlabUrl`
    - Pas de token requis → clone direct sans authentification

  [Projet PRIVÉ]
    - Fournir `gitlabUrl` + `projectToken`
    - `tokenUsername` : optionnel (défaut = 'oauth2')
      • Personal Access Token  → tokenUsername = 'oauth2'
      • Deploy Token           → tokenUsername = votre identifiant de deploy token
    - Le token est injecté dans l'URL sous la forme :
        https://oauth2:<token>@gitlab.example.com/group/project.git

  [Aucune URL]
    - Lancement d'une erreur explicite (pas de simulation)

Outputs propagés dans le contexte partagé :
    commit_sha, repo_path, author, commit_msg, branch, is_private
"""
import logging
import os
import subprocess
import tempfile
from urllib.parse import urlparse, urlunparse

from .base import BaseExecutor

logger = logging.getLogger(__name__)

# Timeout pour les opérations git (secondes)
GIT_TIMEOUT = 120


# ─────────────────────────────────────────────────────────────────────────────
# Utilitaires
# ─────────────────────────────────────────────────────────────────────────────

def _inject_credentials(url: str, token: str, username: str = 'oauth2') -> str:
    """
    Injecte les credentials dans l'URL HTTP/HTTPS de GitLab.

    Exemple :
        https://gitlab.example.com/group/repo.git
        → https://oauth2:glpat-xxxx@gitlab.example.com/group/repo.git
    """
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        # SSH ou autre protocole : on ne modifie pas l'URL
        return url

    # Supprimer d'éventuels credentials déjà présents dans l'URL
    host = parsed.netloc
    if '@' in host:
        host = host.split('@', 1)[-1]

    user = (username.strip() or 'oauth2')
    netloc = f'{user}:{token}@{host}'
    return urlunparse(parsed._replace(netloc=netloc))


def _strip_credentials(url: str) -> str:
    """Retourne l'URL sans credentials (pour les logs)."""
    parsed = urlparse(url)
    host = parsed.netloc
    if '@' in host:
        host = host.split('@', 1)[-1]
    return urlunparse(parsed._replace(netloc=host))


def _run_git(cmd: list, timeout: int = GIT_TIMEOUT) -> str:
    """
    Exécute une commande git et retourne stdout.
    Lève RuntimeError si le code de retour est non-nul.
    """
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        error = (result.stderr or result.stdout or 'Erreur git inconnue').strip()
        raise RuntimeError(error[:1000])
    return result.stdout.strip()


def _git_metadata(repo_path: str) -> dict:
    """Extrait les métadonnées du dernier commit."""
    try:
        sha    = _run_git(['git', '-C', repo_path, 'rev-parse', 'HEAD'])
        author = _run_git(['git', '-C', repo_path, 'log', '-1', '--format=%an'])
        email  = _run_git(['git', '-C', repo_path, 'log', '-1', '--format=%ae'])
        msg    = _run_git(['git', '-C', repo_path, 'log', '-1', '--format=%s'])
        date   = _run_git(['git', '-C', repo_path, 'log', '-1', '--format=%ci'])
        return {
            'commit_sha': sha,
            'author':     author,
            'author_email': email,
            'commit_msg': msg,
            'commit_date': date,
        }
    except Exception as exc:
        logger.warning(f'[GitLabPull] Impossible de lire les métadonnées git : {exc}')
        return {
            'commit_sha': 'unknown',
            'author':     'unknown',
            'author_email': '',
            'commit_msg': '',
            'commit_date': '',
        }


# ─────────────────────────────────────────────────────────────────────────────
# Executor principal
# ─────────────────────────────────────────────────────────────────────────────

class GitLabPullExecutor(BaseExecutor):
    """
    gitlab.Pull — Clone un dépôt GitLab (public ou privé).

    Config du nœud (canvas) :
        gitlabUrl      : str  — URL HTTPS du dépôt (ex: https://gitlab.com/group/repo.git)
        branch         : str  — Branche à cloner (défaut: 'main')
        projectToken   : str  — Token d'accès (laisser vide pour repo public)
        tokenUsername  : str  — Identifiant du token (défaut: 'oauth2')
    """

    def run(self) -> dict:
        url      = self.cfg('gitlabUrl', '').strip()
        branch   = self.cfg('branch', 'main').strip() or 'main'
        token    = self.cfg('projectToken', '').strip()
        username = self.cfg('tokenUsername', 'oauth2').strip() or 'oauth2'

        if not url:
            raise RuntimeError(
                "[GitLabPull] Aucune URL GitLab fournie.\n"
                "Configurez le champ 'gitlabUrl' dans les propriétés du nœud.\n"
                "Exemple : https://gitlab.com/groupe/projet.git"
            )

        is_private = bool(token)

        if is_private:
            return self._clone_private(url, branch, token, username)
        else:
            return self._clone_public(url, branch)

    # ── Projet PUBLIC ─────────────────────────────────────────────────────────

    def _clone_public(self, url: str, branch: str) -> dict:
        """Clone un dépôt public sans authentification."""
        repo_path = tempfile.mkdtemp(prefix='wf_pub_')
        logger.info(f'[GitLabPull] Clone PUBLIC  url={url}  branch={branch}  dest={repo_path}')

        try:
            _run_git([
                'git', 'clone',
                '--branch', branch,
                '--depth', '1',
                '--single-branch',
                url, repo_path,
            ])
        except RuntimeError as exc:
            raise RuntimeError(
                f"Échec du clone public GitLab.\n"
                f"URL     : {url}\n"
                f"Branche : {branch}\n"
                f"Détail  : {exc}\n\n"
                f"Si le dépôt est privé, fournissez un `projectToken`."
            )

        meta = _git_metadata(repo_path)
        logger.info(f'[GitLabPull] Clone PUBLIC OK — commit={meta["commit_sha"][:8]}  author={meta["author"]}')

        return {
            **meta,
            'repo_path':  repo_path,
            'branch':     branch,
            'is_private': False,
        }

    # ── Projet PRIVÉ ──────────────────────────────────────────────────────────

    def _clone_private(self, url: str, branch: str, token: str, username: str) -> dict:
        """Clone un dépôt privé en injectant les credentials dans l'URL."""
        repo_path = tempfile.mkdtemp(prefix='wf_priv_')
        auth_url  = _inject_credentials(url, token, username)
        safe_url  = _strip_credentials(auth_url)   # Pour les logs (sans le token)

        logger.info(f'[GitLabPull] Clone PRIVÉ  url={safe_url}  branch={branch}  dest={repo_path}')

        try:
            _run_git([
                'git', 'clone',
                '--branch', branch,
                '--depth', '1',
                '--single-branch',
                auth_url, repo_path,
            ])

        except RuntimeError as exc:
            # Retry automatique : si le username n'est pas 'oauth2', on réessaie
            if username.lower() != 'oauth2':
                logger.warning(
                    f'[GitLabPull] Échec avec username="{username}" — retry avec oauth2'
                )
                auth_url_retry = _inject_credentials(url, token, 'oauth2')
                try:
                    _run_git([
                        'git', 'clone',
                        '--branch', branch,
                        '--depth', '1',
                        '--single-branch',
                        auth_url_retry, repo_path,
                    ])
                except RuntimeError as exc2:
                    raise RuntimeError(
                        f"Échec du clone privé GitLab (essayé avec '{username}' et 'oauth2').\n"
                        f"URL     : {safe_url}\n"
                        f"Branche : {branch}\n"
                        f"Détail  : {exc2}\n\n"
                        f"Vérifiez que le token a le scope 'read_repository' et que l'URL est correcte."
                    )
            else:
                raise RuntimeError(
                    f"Échec du clone privé GitLab.\n"
                    f"URL     : {safe_url}\n"
                    f"Branche : {branch}\n"
                    f"Détail  : {exc}\n\n"
                    f"Vérifiez que le projectToken est valide et a le scope 'read_repository'."
                )

        meta = _git_metadata(repo_path)
        logger.info(f'[GitLabPull] Clone PRIVÉ OK — commit={meta["commit_sha"][:8]}  author={meta["author"]}')

        return {
            **meta,
            'repo_path':  repo_path,
            'branch':     branch,
            'is_private': True,
        }
