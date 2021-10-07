import logging
import random
import string

from kubernetes_asyncio import client, config
from kubernetes_asyncio.client.api_client import ApiClient
from kubernetes_asyncio.client.rest import ApiException
from kubernetes_asyncio.config.config_exception import \
    ConfigException  # noqa: F401

from .utils import _make_spec_from_dict

log = logging.getLogger("owl.daemon.scheduler")


# Setup K8 configs
# config.load_kube_config()
# config.load_incluster_config()
# configuration = client.Configuration()
# api_instance = client.BatchV1Api()


def kube_delete_empty_pods(namespace=None, phase=None):
    """
    Pods are never empty, just completed the lifecycle.
    As such they can be deleted.
    Pods can be without any running container in 2 states:
    Succeeded and Failed. This call doesn't terminate Failed pods by default.
    """
    namespace = namespace or "default"
    phase = phase or "Succeeded"
    # The always needed object
    deleteoptions = client.V1DeleteOptions()
    # We need the api entry point for pods
    api_pods = client.CoreV1Api()
    # List the pods
    try:
        pods = api_pods.list_namespaced_pod(namespace, pretty=True, timeout_seconds=60)
    except ApiException as e:
        logging.error("Exception when calling CoreV1Api->list_namespaced_pod: %s\n" % e)

    for pod in pods.items:
        logging.debug(pod)
        podname = pod.metadata.name
        try:
            if pod.status.phase == phase:
                api_response = api_pods.delete_namespaced_pod(
                    podname, namespace, deleteoptions
                )
                logging.info("Pod: {} deleted!".format(podname))
                logging.debug(api_response)
            else:
                logging.info(
                    "Pod: {} still not done... Phase: {}".format(
                        podname, pod.status.phase
                    )
                )
        except ApiException as e:
            logging.error(
                "Exception when calling CoreV1Api->delete_namespaced_pod: %s\n" % e
            )

    return


def kube_create_job_object(
    name,
    container_image,
    command=None,
    namespace=None,
    container_name=None,
    env_vars=None,
    extraConfig=None,
    service_account_name=None,
):
    """
    Create a k8 Job Object
    Minimum definition of a job object:
    {'api_version': None, - Str
    'kind': None,     - Str
    'metadata': None, - Metada Object
    'spec': None,     -V1JobSpec
    'status': None}   - V1Job Status
    Docs: https://github.com/kubernetes-client/python/blob/master/kubernetes/docs/V1Job.md
    Docs2: https://kubernetes.io/docs/concepts/workloads/controllers/jobs-run-to-completion/#writing-a-job-spec
    Also docs are pretty pretty bad. Best way is to ´pip install kubernetes´ and go via the autogenerated code
    And figure out the chain of objects that you need to hold a final valid object So for a job object you need:
    V1Job -> V1ObjectMeta
          -> V1JobStatus
          -> V1JobSpec -> V1PodTemplate -> V1PodTemplateSpec -> V1Container

    Now the tricky part, is that V1Job.spec needs a .template, but not a PodTemplateSpec, as such
    you need to build a PodTemplate, add a template field (template.template) and make sure
    template.template.spec is now the PodSpec.
    Then, the V1Job.spec needs to be a JobSpec which has a template the template.template field of the PodTemplate.
    Failure to do so will trigger an API error.
    Also Containers must be a list!
    Docs3: https://github.com/kubernetes-client/python/issues/589
    """
    env_vars = env_vars or {}
    namespace = namespace or "default"
    command = command or "sleep 60"
    container_name = container_name or "jobcontainer"
    # Body is the object Body
    body = client.V1Job(api_version="batch/v1", kind="Job")
    # Body needs Metadata
    # Attention: Each JOB must have a different name!
    body.metadata = client.V1ObjectMeta(namespace=namespace, name=name)
    # And a Status
    body.status = client.V1JobStatus()
    # Now we start with the Template...
    template = client.V1PodTemplate()
    template.template = client.V1PodTemplateSpec()
    # Passing Arguments in Env:
    env_list = []
    for env_name, env_value in env_vars.items():
        env_list.append(client.V1EnvVar(name=env_name, value=str(env_value)))

    if (resources := extraConfig.resources) is not None:
        resourcesReq = client.V1ResourceRequirements(**resources)
        cpu_requests = resources["requests"]["cpu"]
        mem_requests = resources["requests"]["memory"]
        cpu_limits = resources["limits"]["cpu"]
        mem_limits = resources["limits"]["memory"]
        env_list.append(client.V1EnvVar(name="CPU_REQUESTS", value=str(cpu_requests)))
        env_list.append(client.V1EnvVar(name="MEM_REQUESTS", value=str(mem_requests)))
        env_list.append(client.V1EnvVar(name="CPU_LIMITS", value=str(cpu_limits)))
        env_list.append(client.V1EnvVar(name="MEM_LIMITS", value=str(mem_limits)))
    else:
        resourcesReq = None

    volume_mounts = []
    for vm in extraConfig.volumeMounts:
        volume_mounts.append(_make_spec_from_dict(vm, client.V1VolumeMount))

    if (security_context := extraConfig.securityContext) is not None:
        secReq = _make_spec_from_dict(security_context, client.V1SecurityContext)
    else:
        secReq = None

    container = client.V1Container(
        name=container_name,
        image=container_image,
        env=env_list,
        image_pull_policy="IfNotPresent",
        args=command.split(),
        resources=resourcesReq,
        volume_mounts=volume_mounts,
        security_context=secReq,
    )

    volumes = []
    for vol in extraConfig.volumes:
        volumes.append(_make_spec_from_dict(vol, client.V1Volume))

    if (pod_security_context := extraConfig.podSecurityContext) is not None:
        secReq = _make_spec_from_dict(pod_security_context, client.V1PodSecurityContext)
    else:
        secReq = None

    template.template.spec = client.V1PodSpec(
        containers=[container],
        restart_policy="Never",
        termination_grace_period_seconds=30,
        volumes=volumes,
        service_account_name=service_account_name,
        security_context=secReq,
        node_selector=extraConfig.node_selector,
    )

    # And finaly we can create our V1JobSpec!
    body.spec = client.V1JobSpec(
        ttl_seconds_after_finished=300, template=template.template, backoff_limit=2
    )
    return body


async def kube_create_job(
    name,
    image,
    command=None,
    namespace=None,
    extraConfig=None,
    env_vars=None,
    service_account_name=None,
):
    namespace = namespace or "default"
    # Create the job definition
    body = kube_create_job_object(
        name,
        image,
        env_vars=env_vars,
        namespace=namespace,
        command=command or "sleep 60",
        extraConfig=extraConfig or {},
        service_account_name=service_account_name,
    )

    config.load_incluster_config()
    async with ApiClient() as api:
        v1 = client.BatchV1Api(api)
        res = await v1.create_namespaced_job(namespace, body, pretty=True)
    return res


async def kube_delete_job(name, namespace=None):
    config.load_incluster_config()
    async with ApiClient() as api:
        v1 = client.BatchV1Api(api)
        res = await v1.delete_namespaced_job(
            name,
            namespace or "default",
            propagation_policy="Background",
            grace_period_seconds=0,
        )
    return res


def id_generator(size=12, chars=string.ascii_lowercase + string.digits):
    return "".join(random.choice(chars) for _ in range(size))


async def kube_test_credentials():
    """
    Testing function.
    If you get an error on this call don't proceed. Something is wrong on your connectivty to
    Google API.
    Check Credentials, permissions, keys, etc.
    Docs: https://cloud.google.com/docs/authentication/
    """
    config.load_incluster_config()

    async with ApiClient() as api:
        v1 = client.BatchV1Api(api)
        await v1.get_api_resources()
