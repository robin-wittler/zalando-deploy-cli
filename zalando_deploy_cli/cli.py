import json
import os
import re
import string
import subprocess
import sys
import textwrap
import time
import urllib.parse
from pathlib import Path

import click
import pierone.api
import pystache
import requests
import stups_cli.config
import yaml
import zign.api
from clickclick import Action, AliasedGroup, error, info, print_table

CONTEXT_SETTINGS = dict(help_option_names=['-h', '--help'])

# NOTE: application-version-release will be used as Kubernetes resource name
# Kubernetes resource names must conform to DNS_SUBDOMAIN
# see https://github.com/kubernetes/kubernetes/blob/1dfd64f4378ad9dd974bbfbef8e90127dce6aafe/pkg/api/v1/types.go#L53
APPLICATION_PATTERN = re.compile('^[a-z][a-z0-9-]*$')
VERSION_PATTERN = re.compile('^[a-z0-9][a-z0-9.-]*$')

DEFAULT_HTTP_TIMEOUT = 30  # seconds

# EC2 instance memory in MiB
EC2_INSTANCE_MEMORY = {
    't2.nano': 500,
    't2.micro': 1000,
    't2.small': 2000,
    't2.medium': 4000,
    'm3.medium': 3750,
    'm4.large': 8000,
    'c4.large': 3750,
    'c4.xlarge': 7500
}


def find_latest_docker_image_version(image):
    docker_image = pierone.api.DockerImage.parse(image)
    if not docker_image.registry:
        error('Could not resolve "latest" tag for {}: missing registry.'.format(image))
        exit(2)
    token = zign.api.get_token('uid', ['uid'])
    latest_tag = pierone.api.get_latest_tag(docker_image, token)
    if not latest_tag:
        error('Could not resolve "latest" tag for {}'.format(image))
        exit(2)
    return latest_tag


def validate_pattern(pattern):
    def validate(ctx, param, value):
        if not pattern.match(value):
            raise click.BadParameter('does not match regular expression pattern "{}"'.format(pattern.pattern))
        return value
    return validate


application_argument = click.argument('application', callback=validate_pattern(APPLICATION_PATTERN))
version_argument = click.argument('version', callback=validate_pattern(VERSION_PATTERN))
release_argument = click.argument('release', callback=validate_pattern(VERSION_PATTERN))


def request(config: dict, method, path: str, headers=None, exit_on_error=True, **kwargs):
    token = zign.api.get_token('uid', ['uid'])
    if not headers:
        headers = {}
    headers['Authorization'] = 'Bearer {}'.format(token)
    if config.get('user'):
        headers['X-On-Behalf-Of'] = config['user']
    api_url = config.get('deploy_api')
    url = urllib.parse.urljoin(api_url, path)
    response = method(url, headers=headers, timeout=DEFAULT_HTTP_TIMEOUT, **kwargs)
    if exit_on_error:
        if not (200 <= response.status_code < 400):
            error('Server returned HTTP error {} for {}:\n{}'.format(response.status_code, url, response.text))
            exit(2)
    return response


def approve(config, change_request_id):
    path = '/change-requests/{}/approvals'.format(change_request_id)
    data = {}
    request(config, requests.post, path, json=data)


def execute(config, change_request_id):
    path = '/change-requests/{}/execute'.format(change_request_id)
    request(config, requests.post, path)


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

        if 'kind' in data:
            info('Applying Kubernetes manifest {}..'.format(path))
            cluster_id = config.get('kubernetes_cluster')
            namespace = config.get('kubernetes_namespace')
            path = '/kubernetes-clusters/{}/namespaces/{}/resources'.format(cluster_id, namespace)
            response = request(config, requests.post, path, json=data)
            change_request_id = response.json()['id']
        elif 'Resources' in data:
            info('Applying Cloud Formation template {}..'.format(path))
            aws_account = config.get('aws_account')
            aws_region = config.get('aws_region')
            stack_name = data.get('Metadata', {}).get('StackName')
            if not stack_name:
                error('Cloud Formation template requires Metadata/StackName property')
                raise click.Abort()
            path = '/aws-accounts/{}/regions/{}/cloudformation-stacks/{}'.format(
                aws_account, aws_region, stack_name)
            response = request(config, requests.put, path, json=data)
            change_request_id = response.json()['id']
        else:
            error('Neither a Kubernetes manifest nor a Cloud Formation template: {}'.format(path))
            raise click.Abort()

        if execute:
            approve_and_execute(config, change_request_id)
        else:
            print(change_request_id)


@cli.command('resolve-version')
@click.argument('template', type=click.File('r'))
@application_argument
@version_argument
@release_argument
@click.argument('parameter', nargs=-1)
@click.pass_obj
def resolve_version(config, template, application, version, release, parameter):
    '''Resolve "latest" version if needed'''
    if version != 'latest':
        # return fixed version unchanged,
        # nothing to resolve
        print(version)
        return
    context = parse_parameters(parameter)
    context['application'] = application
    context['version'] = version
    context['release'] = release
    data = _render_template(template, context)
    for container in data['spec']['template']['spec']['containers']:
        image = container['image']
        if image.endswith(':latest'):
            latest_version = find_latest_docker_image_version(image)
            print(latest_version)
            return
    error('Could not resolve "latest" version: No matching container found. Please choose a version != "latest".')
    exit(2)


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

    cluster_id = config.get('kubernetes_cluster')
    namespace = config.get('kubernetes_namespace')
    path = '/kubernetes-clusters/{}/namespaces/{}/resources'.format(cluster_id, namespace)
    response = request(config, requests.post, path, json=data)
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
    cluster_id = config.get('kubernetes_cluster')
    namespace = config.get('kubernetes_namespace')
    path = '/kubernetes-clusters/{}/namespaces/{}/resources'.format(cluster_id, namespace)

    resources_update = ResourcesUpdate()
    resources_update.set_label(deployment_name, 'stage', stage)
    response = request(config, requests.patch, path, json=resources_update.to_dict())
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

    cluster_id = config.get('kubernetes_cluster')
    namespace = config.get('kubernetes_namespace')
    path = '/kubernetes-clusters/{}/namespaces/{}/resources'.format(cluster_id, namespace)
    response = request(config, requests.patch, path, json=resources_update.to_dict())
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
        replicas += deployment.get('status', {}).get('replicas', 0)
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

    cluster_id = config.get('kubernetes_cluster')
    namespace = config.get('kubernetes_namespace')
    path = '/kubernetes-clusters/{}/namespaces/{}/resources'.format(cluster_id, namespace)
    response = request(config, requests.patch, path, json=resources_update.to_dict())
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

    cluster_id = config.get('kubernetes_cluster')
    namespace = config.get('kubernetes_namespace')
    path = '/kubernetes-clusters/{}/namespaces/{}/resources'.format(cluster_id, namespace)
    response = request(config, requests.post, path, json=data)
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
        cluster_id = config.get('kubernetes_cluster')
        namespace = config.get('kubernetes_namespace')
        path = '/kubernetes-clusters/{}/namespaces/{}/deployments/{}'.format(
            cluster_id, namespace, deployment_name)
        response = request(config, requests.delete, path)
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
    response = request(config, requests.get, '/change-requests')
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
    for id_ in change_request_id:
        path = '/change-requests/{}'.format(id_)
        response = request(config, requests.get, path)
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
    path = '/change-requests/{}/approvals'.format(change_request_id)
    response = request(config, requests.get, path)
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
    for id_ in change_request_id:
        execute(config, id_)


@cli.command('encrypt')
@click.pass_obj
def encrypt(config):
    '''Encrypt plain text (read from stdin) for deployment configuration'''
    plain_text = sys.stdin.read()
    api_url = config.get('deploy_api')
    url = '{}/secrets'.format(api_url)
    response = request(config, requests.post, url, json={'plaintext': plain_text})
    print("deployment-secret:{}".format(response.json()['data']))


def copy_template(template_path: Path, path: Path, variables: dict):
    for d in template_path.iterdir():
        target_path = path / d.relative_to(template_path)
        if d.is_dir():
            copy_template(d, target_path, variables)
        elif target_path.exists():
            # better not overwrite any existing files!
            raise click.UsageError('Target file "{}" already exists. Aborting!'.format(target_path))
        else:
            with Action('Writing {}..'.format(target_path)):
                target_path.parent.mkdir(parents=True, exist_ok=True)
                with d.open() as fd:
                    contents = fd.read()
                template = string.Template(contents)
                contents = template.safe_substitute(variables)
                with target_path.open('w') as fd:
                    fd.write(contents)


def read_senza_variables(fd):
    variables = {}
    data = yaml.safe_load(fd)

    senza_info = data.get('SenzaInfo')
    if not senza_info:
        raise click.UsageError('Senza file has not property "SenzaInfo"')

    variables['application'] = senza_info.get('StackName')

    components = data.get('SenzaComponents')
    if not components:
        raise click.UsageError('Senza file has no property "SenzaComponents"')

    for component in components:
        for name, definition in component.items():
            type_ = definition.get('Type')
            if not type_:
                raise click.UsageError('Missing "Type" property in Senza component "{}"'.format(name))
            if type_ == 'Senza::TaupageAutoScalingGroup':
                taupage_config = definition.get('TaupageConfig')
                if not taupage_config:
                    raise click.UsageError('Missing "TaupageConfig" property in Senza component "{}"'.format(name))
                # just assume half the main memory of the EC2 instance type
                variables['memory'] = '{}Mi'.format(round(
                                      EC2_INSTANCE_MEMORY.get(taupage_config.get('InstanceType'), 1000) * 0.5))
                variables['image'] = taupage_config.get('source', '').replace('{{Arguments.ImageVersion}}',
                                                                              '{{ version }}')
                variables['env'] = taupage_config.get('environment', {})
                application_id = taupage_config.get('application_id')
                if application_id:
                    # overwrites default StackName
                    variables['application'] = application_id
            elif type_ in ('Senza::WeightedDnsElasticLoadBalancer', 'Senza::WeightedDnsElasticLoadBalancerV2'):
                variables['port'] = definition.get('HTTPPort')
                variables['health_check_path'] = definition.get('HealthCheckPath') or '/health'
                main_domain = definition.get('MainDomain')
                if main_domain:
                    variables['dnsname'] = main_domain

    if 'dnsname' not in variables:
        variables['dnsname'] = '{{ application }}.foo.example.org'

    return variables


def prepare_variables(variables: dict):
    env = []
    for key, val in sorted(variables['env'].items()):
        env.append({'name': str(key), 'value': str(val)})
    # FIXME: the indent is hardcoded and depends on formatting of deployment.yaml :-(
    variables['env'] = textwrap.indent(yaml.dump(env, default_flow_style=False), ' ' * 12)
    return variables


@cli.command('init')
@click.argument('directory', nargs=-1)
@click.option('-t', '--template', help='Use a custom template (default: webapp)',
              metavar='TEMPLATE_ID', default='webapp')
@click.option('--from-senza', help='Convert Senza definition',
              type=click.File('r'), metavar='SENZA_FILE')
@click.option('--kubernetes-cluster')
@click.pass_obj
def init(config, directory, template, from_senza, kubernetes_cluster):
    '''Initialize a new deploy folder with Kubernetes manifests'''
    if directory:
        path = Path(directory[0])
    else:
        path = Path('.')

    if from_senza:
        variables = read_senza_variables(from_senza)
        template = 'senza'
    else:
        variables = {}

    if kubernetes_cluster:
        cluster_id = kubernetes_cluster
    else:
        info('Please select your target Kubernetes cluster')
        subprocess.call(['zkubectl', 'list-clusters'])
        cluster_id = ''
        while len(cluster_id.split(':')) != 4:
            cluster_id = click.prompt('Kubernetes Cluster ID to use')

    variables['cluster_id'] = cluster_id
    parts = cluster_id.split(':')
    variables['account_id'] = ':'.join(parts[:2])
    variables['region'] = parts[2]

    template_path = Path(__file__).parent / 'templates' / template
    variables = prepare_variables(variables)
    copy_template(template_path, path, variables)

    print()

    notes = path / 'NOTES.txt'
    with notes.open() as fd:
        print(fd.read())


def main():
    cli()
