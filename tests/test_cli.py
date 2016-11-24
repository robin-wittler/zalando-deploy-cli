import json
from unittest.mock import MagicMock

from click.testing import CliRunner
from zalando_deploy_cli.cli import cli


def test_switch_deployment(monkeypatch):
    config = {
        'kubernetes_api_server': 'https://example.org',
        'kubernetes_cluster': 'mycluster',
        'kubernetes_namespace': 'mynamespace'
    }

    def check_output(cmd):
        assert cmd == ['zkubectl', 'get', 'deployments', '--namespace=mynamespace', '-l', 'application=myapp', '-o', 'json']
        output = {
            'items': [
                {'metadata': {'name': 'myapp-v2-r41'}},
                {'metadata': {'name': 'myapp-v2-r42'}},
            ]
        }
        return json.dumps(output).encode('utf-8')

    monkeypatch.setattr('zalando_deploy_cli.cli.kubectl_login', MagicMock())
    monkeypatch.setattr('zalando_deploy_cli.cli.request', MagicMock())
    monkeypatch.setattr('subprocess.check_output', check_output)
    monkeypatch.setattr('stups_cli.config.load_config', MagicMock(return_value=config))

    runner = CliRunner()
    result = runner.invoke(cli, ['switch-deployment', 'myapp', 'v2', 'r42', '1/2'])
    print(result.output)
    assert 'Scaling deployment myapp-v2-r41 to 1 replicas..' in result.output
    assert 'Scaling deployment myapp-v2-r42 to 1 replicas..' in result.output
