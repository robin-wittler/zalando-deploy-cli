import json
import pytest
import requests
import yaml
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


def test_create_deployment_invalid_argument():
    runner = CliRunner()
    with runner.isolated_filesystem():
        with open('template.yaml', 'w') as fd:
            yaml.dump({}, fd)

        result = runner.invoke(cli, ['create-deployment', 'template.yaml', 'my-app2', 'v2-X', 'r42'])
    assert 'Error: Invalid value for "version": does not match regular expression pattern "^[a-z0-9][a-z0-9.-]*$' in result.output


def test_create_deployment_success(monkeypatch):
    request = MagicMock()
    request.return_value.json.return_value = {'id': 'my-cr-id'}
    monkeypatch.setattr('zalando_deploy_cli.cli.request', request)

    runner = CliRunner()
    with runner.isolated_filesystem():
        with open('template.yaml', 'w') as fd:
            yaml.dump({}, fd)

        result = runner.invoke(cli, ['create-deployment', 'template.yaml', 'my-app', 'v1', 'r1', 'replicas=3'])
    assert 'my-cr-id' == result.output.strip()


def test_switch_deployment(monkeypatch, mock_config):
    def check_output(cmd):
        assert cmd == ['zkubectl', 'get', '--namespace=mynamespace', '-o', 'json', 'deployments',
                       '-l', 'application=myapp']
        output = {
            'items': [
                {'metadata': {'name': 'myapp-v3-r40'}},
                {'metadata': {'name': 'myapp-v2-r41'}},
                {'metadata': {'name': 'myapp-v2-r42'}},
            ]
        }
        return json.dumps(output).encode('utf-8')

    request = MagicMock()
    request.return_value.json.return_value = {'id': 'my-change-request-id'}

    monkeypatch.setattr('zalando_deploy_cli.cli.kubectl_login', MagicMock())
    monkeypatch.setattr('zalando_deploy_cli.cli.request', request)
    monkeypatch.setattr('subprocess.check_output', check_output)

    runner = CliRunner()
    result = runner.invoke(cli, ['switch-deployment', 'myapp', 'v2', 'r42', '1/2'])
    assert ('Scaling deployment myapp-v3-r40 to 1 replicas..\n'
            'Scaling deployment myapp-v2-r42 to 1 replicas..\n'
            'Scaling deployment myapp-v2-r41 to 0 replicas..\n'
            'my-change-request-id' == result.output.strip())


def test_switch_deployment_call_once(monkeypatch, mock_config):
    def check_output(cmd):
        assert cmd == ['zkubectl', 'get', '--namespace=mynamespace', '-o', 'json', 'deployments',
                       '-l', 'application=myapp']
        output = {
            'items': [
                {'metadata': {'name': 'myapp-v3-r40'}},
                {'metadata': {'name': 'myapp-v2-r41'}},
                {'metadata': {'name': 'myapp-v2-r42'}},
            ]
        }
        return json.dumps(output).encode('utf-8')

    request = MagicMock()
    request.return_value.json.return_value = {'id': 'my-change-request-id'}

    monkeypatch.setattr('zalando_deploy_cli.cli.kubectl_login', MagicMock())
    monkeypatch.setattr('zalando_deploy_cli.cli.request', request)
    monkeypatch.setattr('subprocess.check_output', check_output)

    runner = CliRunner()
    result = runner.invoke(cli, ['switch-deployment', 'myapp', 'v2', 'r42', '1/2'])

    request.called_once_with(requests.patch,
                             ('https://example.org/kubernetes-clusters/'
                              'mycluster/namespaces/mynamespace/resources'),
                             json={'resources_update': ANY})
    assert result.exit_code == 0


def test_switch_deployment_target_does_not_exist(monkeypatch, mock_config):
    def check_output(cmd):
        assert cmd == ['zkubectl', 'get', '--namespace=mynamespace', '-o', 'json', 'deployments',
                       '-l', 'application=myapp']
        output = {
            'items': [
                {'metadata': {'name': 'myapp-v3-r40'}},
                {'metadata': {'name': 'myapp-v2-r41'}},
                {'metadata': {'name': 'myapp-v2-r43'}},
            ]
        }
        return json.dumps(output).encode('utf-8')

    request = MagicMock()
    request.return_value.json.return_value = {'id': 'my-change-request-id'}

    monkeypatch.setattr('zalando_deploy_cli.cli.kubectl_login', MagicMock())
    monkeypatch.setattr('zalando_deploy_cli.cli.request', request)
    monkeypatch.setattr('subprocess.check_output', check_output)

    runner = CliRunner()
    result = runner.invoke(cli, ['switch-deployment', 'myapp', 'v2', 'r42', '1/2'])
    assert 'Deployment myapp-v2-r42 does not exist!' in result.output
    assert result.exit_code == 1


def test_delete_old_deployments(monkeypatch, mock_config):
    def check_output(cmd):
        assert cmd == ['zkubectl', 'get', '--namespace=mynamespace', '-o', 'json', 'deployments', '-l',
                       'application=myapp']
        output = {
            'items': [
                {'metadata': {'name': 'myapp-v2-r40'}},
                {'metadata': {'name': 'myapp-v2-r41'}},
                {'metadata': {'name': 'myapp-v2-r42'}},
            ]
        }
        return json.dumps(output).encode('utf-8')

    request = MagicMock()
    request.return_value.json.return_value = {'id': 'my-change-request-id'}

    monkeypatch.setattr('zalando_deploy_cli.cli.kubectl_login', MagicMock())
    monkeypatch.setattr('zalando_deploy_cli.cli.request', request)
    monkeypatch.setattr('subprocess.check_output', check_output)

    runner = CliRunner()
    result = runner.invoke(cli, ['delete-old-deployments', 'myapp', 'v2', 'r42'])
    assert ('Deleting deployment myapp-v2-r41..\n'
            'my-change-request-id\n'
            'Deleting deployment myapp-v2-r40..\n'
            'my-change-request-id' == result.output.strip())


def test_promote_deployment(monkeypatch, mock_config):
    request = MagicMock()
    request.return_value.json.return_value = {'id': 'my-change-request-id'}

    monkeypatch.setattr('zalando_deploy_cli.cli.request', request)

    runner = CliRunner()
    result = runner.invoke(cli, ['promote-deployment', 'myapp', 'v2', 'r42', 'production'])
    assert 'Promoting deployment myapp-v2-r42 to production stage..\nmy-change-request-id' == result.output.strip()


def test_request_exit_on_error(monkeypatch, capsys):
    monkeypatch.setattr('zign.api.get_token', lambda a, b: 'mytok')

    mock_get = MagicMock()
    mock_get.return_value.status_code = 418
    mock_get.return_value.text = 'Some Error'

    with pytest.raises(SystemExit):
        zalando_deploy_cli.cli.request({}, mock_get, 'https://example.org')
    out, err = capsys.readouterr()
    assert 'Server returned HTTP error 418 for https://example.org:\nSome Error' == err.strip()


def test_request_headers(monkeypatch, capsys):
    monkeypatch.setattr('zign.api.get_token', lambda a, b: 'mytok')

    def mock_get(*args, **kwargs):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = kwargs.get('headers')
        return response

    response = zalando_deploy_cli.cli.request({'user': 'jdoe'}, mock_get, 'https://example.org')
    assert {'Authorization': 'Bearer mytok', 'X-On-Behalf-Of': 'jdoe'} == response.json()


def test_get_current_replicas(monkeypatch, mock_config):
    kubectl_get = MagicMock()
    kubectl_get.return_value = {'items': [{'status': {'replicas': 1}}, {'status': {'replicas': 2}}]}
    monkeypatch.setattr('zalando_deploy_cli.cli.kubectl_get', kubectl_get)

    runner = CliRunner()
    result = runner.invoke(cli, ['get-current-replicas', 'myapp'])
    assert '3' == result.output.strip()


def test_encrypt(monkeypatch, mock_config):
    encrypt_call = MagicMock()
    encrypt_call.return_value = encrypt_call
    encrypt_call.json = MagicMock(return_value={
        'data': 'barFooBAR='
    })
    monkeypatch.setattr('zalando_deploy_cli.cli.request', encrypt_call)

    runner = CliRunner()
    result = runner.invoke(cli, ['encrypt'], input='my_secret')
    assert 'deployment-secret:barFooBAR=' == result.output.strip()

    encrypt_call.assert_called_with(mock_config(), requests.post,
                                    mock_config().get('deploy_api') + '/secrets',
                                    json={'plaintext': 'my_secret'})
