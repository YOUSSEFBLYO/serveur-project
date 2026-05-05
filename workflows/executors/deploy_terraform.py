"""
Terraform Plan — Executor.

Exécute `terraform init` puis `terraform plan` (et optionnellement `apply`)
dans un répertoire de configuration Terraform.
"""
import json
import logging
import os
import shutil
import subprocess
import tempfile

from .base import BaseExecutor

logger = logging.getLogger(__name__)

_TF_TIMEOUT = 600  # 10 minutes


def _run_tf(cmd: list, cwd: str, env: dict,
            timeout: int = _TF_TIMEOUT) -> tuple[int, str, str]:
    """Exécute une commande terraform et retourne (returncode, stdout, stderr)."""
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


class TerraformPlanExecutor(BaseExecutor):
    """
    deploy.Terraform — Plan (et apply optionnel) Terraform.

    Config du nœud :
        configDir      : str  — Chemin vers le répertoire de config TF
                                (héritée de repo_path si non fourni)
        workspace      : str  — Workspace Terraform (défaut: default)
        varFile        : str  — Chemin vers un fichier .tfvars (optionnel)
        extraVars      : text — Variables JSON supplémentaires (ex: {"env":"prod"})
        applyOnSuccess : bool — Appliquer le plan si aucune erreur
        destroyMode    : bool — Exécuter terraform destroy au lieu de apply
        backend        : enum — 'local' | 's3' | 'azurerm' | 'gcs'
    """

    def run(self) -> dict:
        config_dir  = (
            self.cfg('configDir', '').strip()
            or self.ctx('repo_path', '').strip()
        )
        workspace   = self.cfg('workspace', 'default').strip() or 'default'
        var_file    = self.cfg('varFile', '').strip()
        extra_vars  = self.cfg('extraVars', '{}').strip()
        apply_ok    = self.cfg('applyOnSuccess', False)
        destroy     = self.cfg('destroyMode', False)

        if not config_dir:
            raise RuntimeError(
                "[Terraform] Aucun répertoire de configuration trouvé.\n"
                "Configurez 'configDir' ou connectez ce nœud à un nœud 'Git Pull'."
            )
        if not shutil.which('terraform'):
            raise RuntimeError(
                "[Terraform] La commande 'terraform' est introuvable.\n"
                "Installez Terraform sur le serveur d'exécution."
            )

        # Parsing des variables supplémentaires
        tf_vars: dict = {}
        if extra_vars and extra_vars != '{}':
            try:
                tf_vars = json.loads(extra_vars)
                if not isinstance(tf_vars, dict):
                    tf_vars = {}
            except (json.JSONDecodeError, ValueError):
                logger.warning('[Terraform] extraVars JSON invalides — ignorés')

        # Écriture d'un fichier tfvars temporaire pour les variables injectées
        tf_var_file: str | None = None
        if tf_vars:
            tmp_var = tempfile.NamedTemporaryFile(
                mode='w', suffix='.tfvars.json', delete=False
            )
            json.dump(tf_vars, tmp_var)
            tmp_var.close()
            tf_var_file = tmp_var.name

        env = {**os.environ, 'TF_IN_AUTOMATION': '1', 'TF_CLI_ARGS': '-no-color'}

        logger.info(
            f'[Terraform] Répertoire={config_dir}  workspace={workspace}  '
            f'apply={apply_ok}  destroy={destroy}'
        )

        # ── terraform init ─────────────────────────────────────────────────────
        rc, out, err = _run_tf(['terraform', 'init', '-input=false'], config_dir, env)
        if rc != 0:
            raise RuntimeError(
                f"[Terraform] terraform init a échoué.\n{err[:1500]}"
            )
        logger.info('[Terraform] init OK')

        # ── terraform workspace ────────────────────────────────────────────────
        if workspace != 'default':
            _run_tf(['terraform', 'workspace', 'select', '-or-create', workspace],
                    config_dir, env)

        # ── terraform plan ─────────────────────────────────────────────────────
        plan_cmd = ['terraform', 'plan', '-input=false', '-detailed-exitcode',
                    '-out=tfplan.binary']
        if var_file:
            plan_cmd += [f'-var-file={var_file}']
        if tf_var_file:
            plan_cmd += [f'-var-file={tf_var_file}']
        if destroy:
            plan_cmd.append('-destroy')

        rc, plan_out, plan_err = _run_tf(plan_cmd, config_dir, env)

        # Exit codes terraform : 0=no changes, 1=error, 2=changes pending
        if rc == 1:
            raise RuntimeError(
                f"[Terraform] terraform plan a échoué.\n"
                f"{plan_err[:1500] or plan_out[:1500]}"
            )

        has_changes = rc == 2
        logger.info(
            f'[Terraform] Plan OK — changements_détectés={has_changes}'
        )

        result = {
            'terraform_plan_ok':     True,
            'terraform_has_changes': has_changes,
            'terraform_workspace':   workspace,
            'terraform_config_dir':  config_dir,
            'terraform_destroy':     destroy,
        }

        # ── terraform apply (si demandé et s'il y a des changements) ──────────
        if apply_ok and has_changes:
            apply_cmd = ['terraform', 'apply', '-input=false', '-auto-approve', 'tfplan.binary']
            rc_apply, apply_out, apply_err = _run_tf(apply_cmd, config_dir, env)
            if rc_apply != 0:
                raise RuntimeError(
                    f"[Terraform] terraform apply a échoué.\n"
                    f"{apply_err[:1500] or apply_out[:1500]}"
                )
            logger.info('[Terraform] Apply terminé avec succès')
            result['terraform_applied'] = True
        else:
            result['terraform_applied'] = False

        # Nettoyage du fichier tfvars temporaire
        if tf_var_file and os.path.exists(tf_var_file):
            os.unlink(tf_var_file)

        return result
