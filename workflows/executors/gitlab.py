"""
GitLab Pipeline executor.
If GITLAB_URL + GITLAB_TOKEN are set → real GitLab API call.
Otherwise → simulation.
"""
import logging
import time
import requests
from django.conf import settings
from .base import BaseExecutor

logger = logging.getLogger(__name__)


class GitLabPipelineExecutor(BaseExecutor):
    """gitlab.RunPipeline — trigger a GitLab CI pipeline via API."""

    def run(self) -> dict:
        project  = self.cfg('projectName', 'my-project')
        ref      = self.cfg('ref', 'main')
        variables = self.cfg('variables', {})

        gitlab_url   = settings.GITLAB_URL
        gitlab_token = settings.GITLAB_TOKEN

        if gitlab_url and gitlab_token:
            return self._real_pipeline(project, ref, variables, gitlab_url, gitlab_token)

        return self._simulate(project, ref)

    def _real_pipeline(self, project: str, ref: str, variables: dict,
                       url: str, token: str) -> dict:
        # URL-encode the project path
        project_enc = project.replace('/', '%2F')
        endpoint    = f'{url.rstrip("/")}/api/v4/projects/{project_enc}/pipeline'
        headers     = {'PRIVATE-TOKEN': token, 'Content-Type': 'application/json'}

        payload = {'ref': ref}
        if variables:
            payload['variables'] = [
                {'key': k, 'value': v} for k, v in variables.items()
            ]

        logger.info(f'[GitLab] Triggering pipeline for {project}@{ref}')
        resp = requests.post(endpoint, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        return {
            'pipeline_id': data.get('id'),
            'status':      data.get('status'),
            'web_url':     data.get('web_url'),
            'ref':         ref,
            'project':     project,
        }

    def _simulate(self, project: str, ref: str) -> dict:
        logger.info(f'[GitLab SIMULATE] Pipeline {project}@{ref}')
        time.sleep(2)
        return {
            'pipeline_id': 98765,
            'status':      'running',
            'web_url':     f'https://gitlab.example.com/{project}/-/pipelines/98765',
            'ref':         ref,
            'project':     project,
            'mode':        'simulation',
        }
