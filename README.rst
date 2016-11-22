==================
Zalando Deploy CLI
==================

Usage
=====

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
