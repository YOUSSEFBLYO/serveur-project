"""
Docker Build / Push — Executor.

Construit une image Docker à partir d'un Dockerfile et la pousse
vers un registre (Docker Hub, GitLab Registry, ECR, etc.).
Utilise le CLI `docker` disponible sur le serveur d'exécution.
"""
import logging
import shutil
import subprocess
import time

from .base import BaseExecutor

logger = logging.getLogger(__name__)

_BUILD_TIMEOUT = 600  # 10 minutes max pour le build


def _run_docker(cmd: list, timeout: int = _BUILD_TIMEOUT,
                capture: bool = True) -> str:
    """Exécute une commande docker et retourne stdout."""
    result = subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        error = (result.stderr or result.stdout or 'Erreur docker inconnue').strip()
        raise RuntimeError(error[:2000])
    return (result.stdout or '').strip()


class DockerBuildPushExecutor(BaseExecutor):
    """
    build.DockerBuild — Construction et push d'image Docker.

    Config du nœud :
        imageName       : str  — Nom de l'image (ex: myapp ou registry.com/org/app)
        imageTag        : str  — Tag de l'image (défaut: latest)
        dockerfilePath  : str  — Chemin vers le Dockerfile (défaut: Dockerfile)
        buildContext    : str  — Répertoire de contexte de build (défaut: repo_path du contexte)
        registryUrl     : str  — URL du registre (laisser vide pour Docker Hub)
        registryUser    : str  — Identifiant du registre (optionnel)
        registryPass    : str  — Mot de passe / token du registre (optionnel)
        buildArgs       : text — Arguments de build JSON (ex: {"VERSION": "1.0"})
        pushImage       : bool — Pousser l'image après le build (défaut: true)
        platforms       : str  — Plateformes cibles (ex: linux/amd64,linux/arm64)
    """

    def run(self) -> dict:
        image_name      = self.cfg('imageName', '').strip()
        image_tag       = self.cfg('imageTag', 'latest').strip() or 'latest'
        dockerfile_path = self.cfg('dockerfilePath', 'Dockerfile').strip() or 'Dockerfile'
        registry_url    = self.cfg('registryUrl', '').strip()
        registry_user   = self.cfg('registryUser', '').strip()
        registry_pass   = self.cfg('registryPass', '').strip()
        build_args_raw  = self.cfg('buildArgs', '{}').strip()
        push_image      = self.cfg('pushImage', True)
        platforms       = self.cfg('platforms', '').strip()

        # Résoudre le contexte de build depuis le contexte partagé (nœud Git Pull)
        build_context = (
            self.cfg('buildContext', '').strip()
            or self.ctx('repo_path', '').strip()
        )

        if not image_name:
            raise RuntimeError(
                "[DockerBuild] 'imageName' non configuré.\n"
                "Renseignez le nom complet de l'image (ex: myapp ou registry.com/org/app)."
            )
        if not build_context:
            raise RuntimeError(
                "[DockerBuild] Aucun répertoire de build trouvé.\n"
                "Configurez 'buildContext' ou connectez ce nœud à un nœud 'Git Pull'."
            )

        if not shutil.which('docker'):
            raise RuntimeError(
                "[DockerBuild] La commande 'docker' est introuvable sur ce serveur.\n"
                "Installez Docker ou exécutez ce workflow sur un serveur Docker-compatible."
            )

        # Parsing des build args
        import json
        build_args: dict = {}
        if build_args_raw and build_args_raw != '{}':
            try:
                build_args = json.loads(build_args_raw)
                if not isinstance(build_args, dict):
                    build_args = {}
            except (json.JSONDecodeError, ValueError):
                logger.warning('[DockerBuild] buildArgs JSON invalides — ignorés')

        full_tag = f'{image_name}:{image_tag}'
        if registry_url:
            full_tag = f'{registry_url}/{full_tag}'

        # ── Login registre ─────────────────────────────────────────────────────
        if registry_user and registry_pass:
            login_cmd = ['docker', 'login']
            if registry_url:
                login_cmd.append(registry_url)
            login_cmd += ['-u', registry_user, '--password-stdin']
            logger.info(f'[DockerBuild] Login registre : {registry_url or "Docker Hub"}')
            proc = subprocess.run(
                login_cmd,
                input=registry_pass,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"[DockerBuild] Échec du login registre : {proc.stderr.strip()[:500]}"
                )

        # ── Docker Build ───────────────────────────────────────────────────────
        build_cmd = ['docker', 'build', '-t', full_tag, '-f',
                     f'{build_context}/{dockerfile_path}']

        for k, v in build_args.items():
            build_cmd += ['--build-arg', f'{k}={v}']

        if platforms:
            build_cmd += ['--platform', platforms]

        build_cmd.append(build_context)

        logger.info(f'[DockerBuild] Build de l\'image : {full_tag}')
        t0 = time.time()
        _run_docker(build_cmd, timeout=_BUILD_TIMEOUT)
        build_time = round(time.time() - t0, 1)
        logger.info(f'[DockerBuild] Build terminé en {build_time}s → {full_tag}')

        # ── Docker Push ────────────────────────────────────────────────────────
        if push_image:
            logger.info(f'[DockerBuild] Push de l\'image : {full_tag}')
            _run_docker(['docker', 'push', full_tag], timeout=300)
            logger.info(f'[DockerBuild] Push réussi → {full_tag}')

        return {
            'docker_image':     full_tag,
            'docker_tag':       image_tag,
            'docker_pushed':    push_image,
            'docker_build_time': build_time,
            'docker_registry':  registry_url or 'docker.io',
        }
