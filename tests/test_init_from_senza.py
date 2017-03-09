import json
import pytest
import requests
import yaml
from pathlib import Path
import zalando_deploy_cli.cli
from unittest.mock import MagicMock, ANY

from click.testing import CliRunner
from zalando_deploy_cli.cli import cli


@pytest.fixture
def mock_config(monkeypatch):
    config = {
        'kubernetes_api_server': 'https://example.org',
        'kubernetes_cluster': 'mycluster',
        'kubernetes_namespace': 'mynamespace',
        'deploy_api': 'https://deploy.example.org'
    }
    load_config = MagicMock(return_value=config)
    monkeypatch.setattr('stups_cli.config.load_config', load_config)
    return load_config


def test_init_from_senza():
    runner = CliRunner()

    senza_file = Path(__file__).parent / 'fixtures' / 'senza-helloworld.yaml'

    with runner.isolated_filesystem():
        result = runner.invoke(cli, ['init', '--from-senza={}'.format(senza_file), '--kubernetes-cluster=aws:123:my-region:my-kube'])

        for path in Path('.').iterdir():
            print(path)
    print(result.output)
