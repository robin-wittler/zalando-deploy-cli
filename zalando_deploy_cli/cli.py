import json
import subprocess
import time

import click
import pystache
import requests
import stups_cli.config
import yaml
import zign.api
from clickclick import AliasedGroup, info

CONTEXT_SETTINGS = dict(help_option_names=['-h', '--help'])


def approve_and_execute(api_url, change_request_id):
    token = zign.api.get_token('uid', ['uid'])
    headers = {'Authorization': 'Bearer {}'.format(token), 'Content-Type': 'application/json'}
    url = '{}/change-requests/{}/approvals'.format(api_url, change_request_id)
    response = requests.post(url, headers=headers, data=json.dumps({}), timeout=5)
    response.raise_for_status()

    url = '{}/change-requests/{}/execute'.format(api_url, change_request_id)
    response = requests.post(url, headers=headers, timeout=5)
    response.raise_for_status()


def parse_parameters(parameter):
    context = {}
    for param in parameter:
        key, val = param.split('=', 1)
        context[key] = val
    return context


def _render_template(template, context):
    contents = template.read()
    rendered_contents = pystache.render(contents, context)
    data = yaml.safe_load(rendered_contents)
    return data


def get_scaling_operation(replicas, deployment_name):
    return {'resources_update': [{'kind': 'deployments', 'name': deployment_name,
            'operations': [{'op': 'replace', 'path': '/spec/replicas', 'value': replicas}]}]}


@click.group(cls=AliasedGroup, context_settings=CONTEXT_SETTINGS)
@click.pass_context
def cli(ctx):
    ctx.obj = stups_cli.config.load_config('zalando-deploy-cli')


@cli.command()
@click.option('--deploy-api')
@click.option('--cluster-registry')
@click.option('--aws-account')
@click.option('--aws-region')
@click.option('--kubernetes-api-server')
@click.option('--kubernetes-cluster')
@click.option('--kubernetes-namespace')
@click.pass_obj
def configure(config, **kwargs):
    for key, val in kwargs.items():
        if val is not None:
            config[key] = val
    stups_cli.config.store_config(config, 'zalando-deploy-cli')


@cli.command()
@click.argument('template', type=click.File('r'))
@click.argument('parameter', nargs=-1)
@click.pass_obj
@click.option('--execute', is_flag=True)
def apply(config, template, parameter, execute):
    '''Apply CloudFormation or Kubernetes resource'''
    data = _render_template(template, parse_parameters(parameter))

    token = zign.api.get_token('uid', ['uid'])
    headers = {'Authorization': 'Bearer {}'.format(token), 'Content-Type': 'application/json'}
    api_url = config.get('deploy_api')
    if 'kind' in data:
        cluster_id = config.get('kubernetes_cluster')
        namespace = config.get('kubernetes_namespace')
        url = '{}/kubernetes-clusters/{}/namespaces/{}/resources'.format(api_url, cluster_id, namespace)
        response = requests.post(url, headers=headers, data=json.dumps(data), timeout=5)
        response.raise_for_status()
        change_request_id = response.json()['id']
    else:
        pass

    if execute:
        approve_and_execute(api_url, change_request_id)
    else:
        print(change_request_id)


@cli.command('create-deployment')
@click.argument('template', type=click.File('r'))
@click.argument('application')
@click.argument('version')
@click.argument('release')
@click.argument('parameter', nargs=-1)
@click.pass_obj
@click.option('--execute', is_flag=True)
def create_deployment(config, template, application, version, release, parameter, execute):
    '''Create a new Kubernetes deployment'''
    context = parse_parameters(parameter)
    context['application'] = application
    context['version'] = version
    context['release'] = release
    data = _render_template(template, context)

    token = zign.api.get_token('uid', ['uid'])
    headers = {'Authorization': 'Bearer {}'.format(token), 'Content-Type': 'application/json'}
    api_url = config.get('deploy_api')
    cluster_id = config.get('kubernetes_cluster')
    namespace = config.get('kubernetes_namespace')
    url = '{}/kubernetes-clusters/{}/namespaces/{}/resources'.format(api_url, cluster_id, namespace)
    response = requests.post(url, headers=headers, data=json.dumps(data), timeout=5)
    response.raise_for_status()
    change_request_id = response.json()['id']

    if execute:
        approve_and_execute(api_url, change_request_id)
    else:
        print(change_request_id)


@cli.command('wait-for-deployment')
@click.argument('application')
@click.argument('version')
@click.argument('release')
@click.option('-t', '--timeout',
              type=click.IntRange(0, 7200, clamp=True),
              metavar='SECS',
              default=300,
              help='Maximum wait time (default: 300s)')
@click.option('-i', '--interval', default=10,
              type=click.IntRange(1, 600, clamp=True),
              help='Time between checks (default: 10s)')
@click.pass_obj
def wait_for_deployment(config, application, version, release, timeout, interval):
    '''Wait for all pods'''
    namespace = config.get('kubernetes_namespace')
    # TODO: api server needs to come from Cluster Registry
    subprocess.check_output(['zkubectl', 'login', config.get('kubernetes_api_server')])
    deployment_name = '{}-{}-{}'.format(application, version, release)
    cutoff = time.time() + timeout
    while time.time() < cutoff:
        cmd = ['zkubectl', 'get', 'pods', '--namespace={}'.format(namespace),
               '-l', 'application={},version={},release={}'.format(application, version, release), '-o', 'json']
        out = subprocess.check_output(cmd)
        data = json.loads(out.decode('utf-8'))
        pods = data['items']
        pods_ready = 0
        for pod in pods:
            if pod['status'].get('phase') == 'Running':
                all_containers_ready = True
                for cont in pod['status'].get('containerStatuses', []):
                    if not cont.get('ready'):
                        all_containers_ready = False
                if all_containers_ready:
                    pods_ready += 1
        if pods and pods_ready >= len(pods):
            return
        info('Waiting up to {:.0f} more secs for deployment '
             '{} ({}/{} pods ready)..'.format(cutoff - time.time(), deployment_name, pods_ready, len(pods)))
        time.sleep(interval)
    raise click.Abort()


@cli.command('switch-deployment')
@click.argument('application')
@click.argument('version')
@click.argument('release')
@click.argument('ratio')
@click.pass_obj
@click.option('--execute', is_flag=True)
def switch_deployment(config, application, version, release, ratio, execute):
    '''Switch to new release'''
    namespace = config.get('kubernetes_namespace')
    # TODO: api server needs to come from Cluster Registry
    subprocess.check_output(['zkubectl', 'login', config.get('kubernetes_api_server')])

    target_replicas, total = ratio.split('/')
    target_replicas = int(target_replicas)
    total = int(total)

    cmd = ['zkubectl', 'get', 'deployments', '--namespace={}'.format(namespace),
           '-l', 'application={}'.format(application), '-o', 'json']
    out = subprocess.check_output(cmd)
    data = json.loads(out.decode('utf-8'))
    deployments = data['items']
    target_deployment_name = '{}-{}-{}'.format(application, version, release)

    remaining_replicas = total
    for deployment in sorted(deployments, key=lambda d: d['metadata']['name'], reverse=True):
        deployment_name = deployment['metadata']['name']
        if deployment_name == target_deployment_name:
            replicas = target_replicas
        else:
            # maybe spread across all other deployments?
            replicas = remaining_replicas

        remaining_replicas -= replicas

        info('Scaling deployment {} to {} replicas..'.format(deployment_name, replicas))
        token = zign.api.get_token('uid', ['uid'])
        headers = {'Authorization': 'Bearer {}'.format(token), 'Content-Type': 'application/json'}
        api_url = config.get('deploy_api')
        cluster_id = config.get('kubernetes_cluster')
        namespace = config.get('kubernetes_namespace')
        url = '{}/kubernetes-clusters/{}/namespaces/{}/resources'.format(api_url, cluster_id, namespace)
        response = requests.patch(url, headers=headers, data=json.dumps(
            get_scaling_operation(replicas, deployment_name)), timeout=5)
        response.raise_for_status()
        change_request_id = response.json()['id']

        if execute:
            approve_and_execute(api_url, change_request_id)
        else:
            print(change_request_id)


@cli.command('delete-old-deployments')
@click.argument('application')
@click.argument('version')
@click.argument('release')
@click.pass_obj
@click.option('--execute', is_flag=True)
def delete_old_deployments(config, application, version, release, execute):
    '''Delete old releases'''
    namespace = config.get('kubernetes_namespace')
    # TODO: api server needs to come from Cluster Registry
    subprocess.check_output(['zkubectl', 'login', config.get('kubernetes_api_server')])

    cmd = ['zkubectl', 'get', 'deployments', '--namespace={}'.format(namespace),
           '-l', 'application={}'.format(application), '-o', 'json']
    out = subprocess.check_output(cmd)
    data = json.loads(out.decode('utf-8'))
    deployments = data['items']
    target_deployment_name = '{}-{}-{}'.format(application, version, release)

    for deployment in sorted(deployments, key=lambda d: d['metadata']['name'], reverse=True):
        deployment_name = deployment['metadata']['name']
        if deployment_name != target_deployment_name:
            info('Deleting deployment {}..'.format(deployment_name))
            token = zign.api.get_token('uid', ['uid'])
            headers = {'Authorization': 'Bearer {}'.format(token)}
            api_url = config.get('deploy_api')
            cluster_id = config.get('kubernetes_cluster')
            namespace = config.get('kubernetes_namespace')
            url = '{}/kubernetes-clusters/{}/namespaces/{}/deployments/{}'.format(
                api_url, cluster_id, namespace, deployment_name)
            response = requests.delete(url, headers=headers, timeout=5)
            response.raise_for_status()
            change_request_id = response.json()['id']

            if execute:
                approve_and_execute(api_url, change_request_id)
            else:
                print(change_request_id)


@cli.command('render-template')
@click.argument('template', type=click.File('r'))
@click.argument('parameter', nargs=-1)
@click.pass_obj
def render_template(config, template, parameter):
    '''Interpolate YAML Mustache template'''
    data = _render_template(template, parse_parameters(parameter))
    print(yaml.safe_dump(data))


def main():
    cli()
