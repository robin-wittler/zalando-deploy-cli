import json

import click
import pystache
import requests
import stups_cli.config
import yaml
import zign.api


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


def render_template(template, context):
    contents = template.read()
    rendered_contents = pystache.render(contents, context)
    data = yaml.safe_load(rendered_contents)
    return data


@click.group()
@click.pass_context
def cli(ctx):
    ctx.obj = stups_cli.config.load_config('zalando-deploy-cli')


@cli.command()
@click.option('--deploy-api')
@click.option('--aws-account')
@click.option('--aws-region')
@click.option('--kubernetes-cluster')
@click.option('--kubernetes-namespace')
@click.pass_obj
def configure(config, **kwargs):
    config.update(**kwargs)
    stups_cli.config.store_config(config, 'zalando-deploy-cli')


@cli.command()
@click.argument('template', type=click.File('r'))
@click.argument('parameter', nargs=-1)
@click.pass_obj
@click.option('--execute', is_flag=True)
def apply(config, template, parameter, execute):
    data = render_template(template, parse_parameters(parameter))

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


@cli.command('create-deployment')
@click.argument('template', type=click.File('r'))
@click.argument('application')
@click.argument('version')
@click.argument('release')
@click.argument('parameter', nargs=-1)
@click.pass_obj
@click.option('--execute', is_flag=True)
def create_deployment(config, template, application, version, release, parameter, execute):
    context = parse_parameters(parameter)
    context['application'] = application
    context['version'] = version
    context['release'] = release
    data = render_template(template, context)

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


def main():
    cli()
