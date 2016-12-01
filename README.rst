==================
Zalando Deploy CLI
==================

.. image:: https://travis-ci.org/zalando-incubator/zalando-deploy-cli.svg?branch=master
   :target: https://travis-ci.org/zalando-incubator/zalando-deploy-cli
   :alt: Build Status

.. image:: https://coveralls.io/repos/zalando-incubator/zalando-deploy-cli/badge.svg
   :target: https://coveralls.io/r/zalando-incubator/zalando-deploy-cli
   :alt: Code Coverage

.. image:: https://img.shields.io/pypi/dw/zalando-deploy-cli.svg
   :target: https://pypi.python.org/pypi/zalando-deploy-cli/
   :alt: PyPI Downloads

.. image:: https://img.shields.io/pypi/v/zalando-deploy-cli.svg
   :target: https://pypi.python.org/pypi/zalando-deploy-cli/
   :alt: Latest PyPI version

.. image:: https://img.shields.io/pypi/l/zalando-deploy-cli.svg
   :target: https://pypi.python.org/pypi/zalando-deploy-cli/
   :alt: License

This CLI provides an opinionated, high-level wrapper for the "Autobahn" deployment API:

* It only provides high-level commands

  * Only support what CI/CD pipelines need
  * Low-level access to Kubernetes provided by `zkubectl`_

* It uses Mustache_ for templating

  * Familiar to Zalando users: we already use it in Senza_
  * Language-agnostic: users could switch to other tools without changing their manifests

Steps required by CI/CD Pipeline
================================

* Apply stateful resources

  * Cloud Formation templates
  * Kubernetes manifests
  * Should be possible for a whole directory

* Create Kubernetes deployment
* Switch “traffic” / scale deployments pod by pod

  * Needs to check pod “readyness”

* Delete old deployments
* Scale deployment (manually triggered)

Why another CLI?
================

The CI/CD pipeline could also call the "Autobahn" deployment API directly, but:

* CI/CD pipeline (Jenkinsfile) would contain a lot of code and logic to interact with Autobahn API directly
* Hard to test interaction with Autobahn API without running CI/CD (Jenkins)
* No standard templating for Kubernetes manifests --- first approach relied on another 3rd party tool (sigil)
* Switching to another CI/CD would require reimplementing logic from Jenkinsfile

Usage
=====

All commands interacting with the "Autobahn" deployment API either need the ``--execute`` flag (for immediate approval and execution) or additional calls to ``approve`` and ``execute``.

.. code-block:: bash

    $ sudo pip3 install -U zalando-deploy-cli
    $ zdeploy configure \
        --deploy-api=https://deploy-api.example.org \
        --aws-account=aws:7.. \
        --aws-region=eu-central-1 \
        --kubernetes-cluster=aws:7..:kube-1
    $ zdeploy apply ./apply/my-service.yaml --execute \
        application=kio version=cd53 release=12
    $ zdeploy create-deployment deployment.yaml kio cd53 12 --execute
    $ zdeploy wait-for-deployment kio cd53 12
    $ zdeploy switch-deployment kio cd53 12 2/10 --execute
    $ zdeploy wait-for-deployment kio cd53 12
    $ zdeploy switch-deployment kio cd53 12 3/10 --execute
    $ # ..
    $ zdeploy switch-deployment kio cd53 12 10/10 --execute
    $ zdeploy delete-old-deployments kio cd53 12 --execute
    $ zdeploy scale-deployment kio cd53 12 15 --execute # manual scaling

You can also just use the Mustache_ template interpolation manually:

.. code-block:: bash

    $ zdeploy render-template my-manifest.yaml foo=bar var2=123


.. _zkubectl: https://github.com/zalando-incubator/zalando-kubectl
.. _Mustache: http://mustache.github.io/
.. _Senza: https://github.com/zalando-stups/senza
