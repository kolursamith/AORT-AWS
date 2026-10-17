# Cloud deployment

The deployment guide is in [AWS_DEPLOYMENT.md](../AWS_DEPLOYMENT.md). The infrastructure code is in [cdk/](cdk/).

- `cloud/cdk/`: AWS CDK (Python) app. `app.py` is the entry point, `aorts_stack.py` defines the stack, and `lambda/` holds the EventBridge archive function.
- `cloud/docker/`: the image is built from the repository [Dockerfile](../Dockerfile) and orchestrated by [docker-compose.yml](../docker-compose.yml).
- `cloud/monitoring/`: alarm and metric definitions live in the CDK stack (see `alarm(...)` calls). [monitoring/README.md](monitoring/README.md) lists them.
