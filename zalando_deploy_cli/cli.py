import json
import os
import re
import subprocess
import sys
import time

import click
import pystache
import requests
import stups_cli.config
import yaml
import zign.api
from clickclick import AliasedGroup, error, info, print_table

CONTEXT_SETTINGS = dict(help_option_names=['-h', '--help'])

# NOTE: application-version-release will be used as Kubernetes resource name
# Kubernetes resource names must conform to DNS_SUBDOMAIN
# see https://github.com/kubernetes/kubernetes/blob/1dfd64f4378ad9dd974bbfbef8e90127dce6aafe/pkg/api/v1/types.go#L53
APPLICATION_PATTERN = re.compile('^[a-z][a-z0-9-]*$')
VERSION_PATTERN = re.compile('^[a-z0-9][a-z0-9.-]*$')


def validate_pattern(pattern):
    def validate(ctx, param, value):
        if not pattern.match(value):
            raise click.BadParameter('does not match regular expression pattern "{}"'.format(pattern.pattern))
        return value
    return validate


application_argument = click.argument('application', callback=validate_pattern(APPLICATION_PATTERN))
version_argument = click.argument('version', callback=validate_pattern(VERSION_PATTERN))
release_argument = click.argument('release', callback=validate_pattern(VERSION_PATTERN))


def request(method, url, headers=None, exit_on_error=True, **kwargs):
    token = zign.api.get_token('uid', ['uid'])
    if not headers:
        headers = {}
    headers['Authorization'] = 'Bearer {}'.format(token)
    response = method(url, headers=headers, timeout=5, **kwargs)
    if exit_on_error:
        if not (200 <= response.status_code < 400):
            error('Server returned HTTP error {} for {}:\n{}'.format(response.status_code, url, response.text))
            exit(2)
    return response


def approve(config, change_request_id):
    api_url = config.get('deploy_api')
    url = '{}/change-requests/{}/approvals'.format(api_url, change_request_id)
    data = {}
    user = config.get('user')
    if user:
        data['user'] = user
    request(requests.post, url, json=data)


def execute(config, change_request_id):
    api_url = config.get('deploy_api')
    url = '{}/change-requests/{}/execute'.format(api_url, change_request_id)
    request(requests.post, url)


def approve_and_execute(config, change_request_id):
    approve(config, change_request_id)
    execute(config, change_request_id)


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


class ResourcesUpdate:
    def __init__(self, updates=None):
        self.resources_update = updates or []

    def set_number_of_replicas(self, name: str, replicas: int, kind: str='deployments'):
        self.resources_update.append({
            'name': name,
            'kind': kind,
            'operations': [{'op': 'replace', 'path': '/spec/replicas', 'value': replicas}]
        })

    def set_label(self, name: str, label_key: str, label_value: str, kind: str='deployments'):
        path = '/spec/template/metadata/labels/{}'.format(label_key)
        self.resources_update.append({
            'name': name,
            'kind': kind,
            'operations': [{'op': 'replace', 'path': path, 'value': label_value}]
        })

    def to_dict(self):
        return {'resources_update': self.resources_update}


def kubectl_login(config):
    arg = config.get('kubernetes_api_server')
    if not arg:
        # this requires zkubectl to be configured appropriately
        # with the Cluster Registry URL
        arg = config.get('kubernetes_cluster')
    subprocess.check_call(['zkubectl', 'login', arg])


def kubectl_get(namespace, *args):
    cmd = ['zkubectl', 'get', '--namespace={}'.format(namespace), '-o', 'json'] + list(args)
    out = subprocess.check_output(cmd)
    data = json.loads(out.decode('utf-8'))
    return data


@click.group(cls=AliasedGroup, context_settings=CONTEXT_SETTINGS)
@click.pass_context
def cli(ctx):
    ctx.obj = stups_cli.config.load_config('zalando-deploy-cli')


@cli.command()
@click.option('--deploy-api')
@click.option('--aws-account')
@click.option('--aws-region')
@click.option('--kubernetes-api-server')
@click.option('--kubernetes-cluster')
@click.option('--kubernetes-namespace')
@click.option('--user', help='Username to use for approvals (optional)')
@click.pass_obj
def configure(config, **kwargs):
    for key, val in kwargs.items():
        if val is not None:
            config[key] = val
    stups_cli.config.store_config(config, 'zalando-deploy-cli')


@cli.command()
@click.argument('template_or_directory')
@click.argument('parameter', nargs=-1)
@click.pass_obj
@click.option('--execute', is_flag=True)
def apply(config, template_or_directory, parameter, execute):
    '''Apply CloudFormation or Kubernetes resource'''

    template_paths = []
    if os.path.isdir(template_or_directory):
        for entry in os.listdir(template_or_directory):
            if entry.endswith('.yaml') and not entry.startswith('.'):
                template_paths.append(os.path.join(template_or_directory, entry))
    else:
        template_paths.append(template_or_directory)

    for path in template_paths:
        with open(path, 'r') as fd:
            data = _render_template(fd, parse_parameters(parameter))

        if not isinstance(data, dict):
            error('Invalid YAML contents in {}'.format(path))
            raise click.Abort()

        api_url = config.get('deploy_api')
        if 'kind' in data:
            info('Applying Kubernetes manifest {}..'.format(path))
            cluster_id = config.get('kubernetes_cluster')
            namespace = config.get('kubernetes_namespace')
            url = '{}/kubernetes-clusters/{}/namespaces/{}/resources'.format(api_url, cluster_id, namespace)
            response = request(requests.post, url, json=data)
            change_request_id = response.json()['id']
        elif 'Resources' in data:
            info('Applying Cloud Formation template {}..'.format(path))
            aws_account = config.get('aws_account')
            aws_region = config.get('aws_region')
            stack_name = data.get('Metadata', {}).get('StackName')
            if not stack_name:
                error('Cloud Formation template requires Metadata/StackName property')
                raise click.Abort()
            url = '{}/aws-accounts/{}/regions/{}/cloudformation-stacks/{}'.format(
                api_url, aws_account, aws_region, stack_name)
            response = request(requests.put, url, json=data)
            change_request_id = response.json()['id']
        else:
            error('Neither a Kubernetes manifest nor a Cloud Formation template: {}'.format(path))
            raise click.Abort()

        if execute:
            approve_and_execute(config, change_request_id)
        else:
            print(change_request_id)


@cli.command('create-deployment')
@click.argument('template', type=click.File('r'))
@application_argument
@version_argument
@release_argument
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

    api_url = config.get('deploy_api')
    cluster_id = config.get('kubernetes_cluster')
    namespace = config.get('kubernetes_namespace')
    url = '{}/kubernetes-clusters/{}/namespaces/{}/resources'.format(api_url, cluster_id, namespace)
    response = request(requests.post, url, json=data)
    change_request_id = response.json()['id']

    if execute:
        approve_and_execute(config, change_request_id)
    else:
        print(change_request_id)


@cli.command('wait-for-deployment')
@application_argument
@version_argument
@release_argument
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
    '''Wait for all pods to become ready'''
    namespace = config.get('kubernetes_namespace')
    kubectl_login(config)
    deployment_name = '{}-{}-{}'.format(application, version, release)
    cutoff = time.time() + timeout
    while time.time() < cutoff:
        data = kubectl_get(namespace, 'pods', '-l',
                           'application={},version={},release={}'.format(application, version, release))
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


@cli.command('promote-deployment')
@application_argument
@version_argument
@release_argument
@click.argument('stage')
@click.option('--execute', is_flag=True)
@click.pass_obj
def promote_deployment(config, application, version, release, stage, execute):
    '''Promote deployment to new stage'''
    namespace = config.get('kubernetes_namespace')
    deployment_name = '{}-{}-{}'.format(application, version, release)

    info('Promoting deployment {} to {} stage..'.format(deployment_name, stage))
    api_url = config.get('deploy_api')
    cluster_id = config.get('kubernetes_cluster')
    namespace = config.get('kubernetes_namespace')
    url = '{}/kubernetes-clusters/{}/namespaces/{}/resources'.format(api_url, cluster_id, namespace)

    resources_update = ResourcesUpdate()
    resources_update.set_label(deployment_name, 'stage', stage)
    response = request(requests.patch, url, json=resources_update.to_dict())
    change_request_id = response.json()['id']

    if execute:
        approve_and_execute(config, change_request_id)
    else:
        print(change_request_id)


@cli.command('switch-deployment')
@application_argument
@version_argument
@release_argument
@click.argument('ratio')
@click.pass_obj
@click.option('--execute', is_flag=True)
def switch_deployment(config, application, version, release, ratio, execute):
    '''Switch to new release'''
    namespace = config.get('kubernetes_namespace')
    kubectl_login(config)

    target_replicas, total = ratio.split('/')
    target_replicas = int(target_replicas)
    total = int(total)

    data = kubectl_get(namespace, 'deployments', '-l', 'application={}'.format(application))
    deployments = data['items']
    target_deployment_name = '{}-{}-{}'.format(application, version, release)

    target_deployment_exists = False
    for deployment in deployments:
        if deployment['metadata']['name'] == target_deployment_name:
            target_deployment_exists = True
    if not target_deployment_exists:
        error("Deployment {} does not exist!".format(target_deployment_name))
        exit(1)

    resources_update = ResourcesUpdate()
    remaining_replicas = total - target_replicas
    for deployment in sorted(deployments, key=lambda d: d['metadata']['name'], reverse=True):
        deployment_name = deployment['metadata']['name']
        if deployment_name == target_deployment_name:
            replicas = target_replicas
        else:
            # maybe spread across all other deployments?
            replicas = remaining_replicas
            remaining_replicas = 0

        info('Scaling deployment {} to {} replicas..'.format(deployment_name, replicas))
        resources_update.set_number_of_replicas(deployment_name, replicas)

    api_url = config.get('deploy_api')
    cluster_id = config.get('kubernetes_cluster')
    namespace = config.get('kubernetes_namespace')
    url = '{}/kubernetes-clusters/{}/namespaces/{}/resources'.format(api_url, cluster_id, namespace)
    response = request(requests.patch, url, json=resources_update.to_dict())
    change_request_id = response.json()['id']

    if execute:
        approve_and_execute(config, change_request_id)
    else:
        print(change_request_id)


@cli.command('get-current-replicas')
@application_argument
@click.pass_obj
def get_current_replicas(config, application):
    '''Get current total number of replicas for given application'''
    namespace = config.get('kubernetes_namespace')
    data = kubectl_get(namespace, 'deployments', '-l', 'application={}'.format(application))
    replicas = 0
    for deployment in data['items']:
        replicas += deployment['status']['replicas']
    print(replicas)


@cli.command('scale-deployment')
@application_argument
@version_argument
@release_argument
@click.argument('replicas', type=int)
@click.pass_obj
@click.option('--execute', is_flag=True)
def scale_deployment(config, application, version, release, replicas, execute):
    '''Scale a single deployment'''
    namespace = config.get('kubernetes_namespace')
    kubectl_login(config)

    deployment_name = '{}-{}-{}'.format(application, version, release)

    info('Scaling deployment {} to {} replicas..'.format(deployment_name, replicas))
    resources_update = ResourcesUpdate()
    resources_update.set_number_of_replicas(deployment_name, replicas)

    api_url = config.get('deploy_api')
    cluster_id = config.get('kubernetes_cluster')
    namespace = config.get('kubernetes_namespace')
    url = '{}/kubernetes-clusters/{}/namespaces/{}/resources'.format(api_url, cluster_id, namespace)
    response = request(requests.patch, url, json=resources_update.to_dict())
    change_request_id = response.json()['id']

    if execute:
        approve_and_execute(config, change_request_id)
    else:
        print(change_request_id)


@cli.command('apply-autoscaling')
@click.argument('template', type=click.File('r'))
@application_argument
@version_argument
@release_argument
@click.argument('parameter', nargs=-1)
@click.pass_obj
@click.option('--execute', is_flag=True)
def apply_autoscaling(config, template, application, version, release, parameter, execute):
    '''Apply Horizontal Pod Autoscaling to current deployment'''
    context = parse_parameters(parameter)
    context['application'] = application
    context['version'] = version
    context['release'] = release
    data = _render_template(template, context)

    api_url = config.get('deploy_api')
    cluster_id = config.get('kubernetes_cluster')
    namespace = config.get('kubernetes_namespace')
    url = '{}/kubernetes-clusters/{}/namespaces/{}/resources'.format(api_url, cluster_id, namespace)
    response = request(requests.post, url, json=data)
    change_request_id = response.json()['id']

    if execute:
        approve_and_execute(config, change_request_id)
    else:
        print(change_request_id)


@cli.command('delete-old-deployments')
@application_argument
@version_argument
@release_argument
@click.pass_obj
@click.option('--execute', is_flag=True)
def delete_old_deployments(config, application, version, release, execute):
    '''Delete old releases'''
    namespace = config.get('kubernetes_namespace')
    kubectl_login(config)

    data = kubectl_get(namespace, 'deployments', '-l', 'application={}'.format(application))
    deployments = data['items']
    target_deployment_name = '{}-{}-{}'.format(application, version, release)
    deployments_to_delete = []
    deployment_found = False

    for deployment in sorted(deployments, key=lambda d: d['metadata']['name'], reverse=True):
        deployment_name = deployment['metadata']['name']
        if deployment_name == target_deployment_name:
            deployment_found = True
        else:
            deployments_to_delete.append(deployment_name)

    if not deployment_found:
        error('Deployment {} was not found.'.format(target_deployment_name))
        raise click.Abort()

    for deployment_name in deployments_to_delete:
        info('Deleting deployment {}..'.format(deployment_name))
        api_url = config.get('deploy_api')
        cluster_id = config.get('kubernetes_cluster')
        namespace = config.get('kubernetes_namespace')
        url = '{}/kubernetes-clusters/{}/namespaces/{}/deployments/{}'.format(
            api_url, cluster_id, namespace, deployment_name)
        response = request(requests.delete, url)
        change_request_id = response.json()['id']

        if execute:
            approve_and_execute(config, change_request_id)
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


@cli.command('list-change-requests')
@click.pass_obj
def list_change_requests(config):
    '''List change requests'''
    api_url = config.get('deploy_api')
    url = '{}/change-requests'.format(api_url)
    response = request(requests.get, url)
    items = response.json()['items']
    rows = []
    for row in items:
        rows.append(row)
    print_table('id platform kind user executed'.split(), rows)


@cli.command('get-change-request')
@click.argument('change_request_id', nargs=-1)
@click.pass_obj
def get_change_request(config, change_request_id):
    '''Get one or more change requests'''
    api_url = config.get('deploy_api')
    for id_ in change_request_id:
        url = '{}/change-requests/{}'.format(api_url, id_)
        response = request(requests.get, url)
        data = response.json()
        print(yaml.safe_dump(data, default_flow_style=False))


@cli.command('approve-change-request')
@click.argument('change_request_id', nargs=-1)
@click.pass_obj
def approve_change_request(config, change_request_id):
    '''Approve one or more change requests'''
    for id_ in change_request_id:
        approve(config, id_)


@cli.command('list-approvals')
@click.argument('change_request_id')
@click.pass_obj
def list_approvals(config, change_request_id):
    '''Show approvals for given change request'''
    api_url = config.get('deploy_api')
    url = '{}/change-requests/{}/approvals'.format(api_url, change_request_id)
    response = request(requests.get, url)
    items = response.json()['items']
    rows = []
    for row in items:
        rows.append(row)
    print_table('user created_at'.split(), rows)


@cli.command('execute-change-request')
@click.argument('change_request_id', nargs=-1)
@click.pass_obj
def execute_change_request(config, change_request_id):
    '''Execute one or more change requests'''
    api_url = config.get('deploy_api')
    for id_ in change_request_id:
        execute(api_url, id_)


@cli.command('encrypt')
@click.pass_obj
def encrypt(config):
    '''Encrypt plain text (read from stdin) for deployment configuration'''
    plain_text = sys.stdin.read()
    api_url = config.get('deploy_api')
    url = '{}/secrets'.format(api_url)
    response = request(requests.post, url, json={'plaintext': plain_text})
    print(response.json()['data'])


def main():
    cli()
