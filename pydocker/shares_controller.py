"""
Dynamic CPU shares controller based on the Heracles design

Current pitfalls:
- when shrinking, we penalize all BE containers instead of killing 1-2 of them

TODO
- validate CPU usage measurements

"""

__author__ = "Christos Kozyrakis"
__email__ = "christos@hyperpilot.io"
__copyright__ = "Copyright 2017, HyperPilot Inc"

import time
from datetime import datetime as dt
import sys
import json
import argparse
import os.path
import os
from io import BytesIO
import subprocess
import pycurl
import docker
from kubernetes import client, config
from kubernetes.client.rest import ApiException

# globals
verbose = False

class Container(object):
  """ A class for tracking active containers
  """
  def __init__(self):
    self.docker_name = ''
    self.k8s_pod_name = ''
    self.k8s_namespace = ''
    self.docker_id = 0
    self.wclass = 'HP'
    self.shares = 0
    self.docker = None
    self.cpu_percent = 0

  def __repr__(self):
    return "<Container:%s pod:%s class:%s shares:%d>" \
           % (self.docker_name, self.k8s_pod_name, self.wclass, self.shares)

  def __str__(self):
    return "<Container:%s pod:%s class:%s shares:%d>" \
           % (self.docker_name, self.k8s_pod_name, self.wclass, self.shares)


class ControllerStats(object):
  """ A class for tracking controller stats
  """
  def __init__(self):
    self.hp_cont = 0
    self.be_cont = 0
    self.hp_shares = 0
    self.be_shares = 0
    self.hp_cpu_percent = 0
    self.be_cpu_percent = 0


class NodeStats(object):
  """ A class for tracking node stats
  """
  def __init__(self):
    self.cpu = 0
    self.name = ''
    self.qos_app = ''


def ActiveContainers(denv, kenv, params, node):
  """ Identifies active containers in a docker environment.
  """
  min_shares = params['min_shares']
  active_containers = {}
  stats = ControllerStats()

  # read container list
  try:
    containers = denv.containers.list()
  except docker.errors.APIError:
    print "Cannot communicate with docker daemon, terminating."
    sys.exit(-1)

  for cont in containers:
    try:
      _ = Container()
      _.docker_id = cont.id
      _.docker_name = cont.name
      _.docker = cont
      # check container shares
      _.shares = cont.attrs['HostConfig']['CpuShares']
      if _.shares < min_shares:
        _.shares = min_shares
        cont.update(cpu_shares=_.shares)
      # check container class
      if 'hyperpilot.io/wclass' in cont.attrs['Config']['Labels']:
        _.wclass = cont.attrs['Config']['Labels']['hyperpilot.io/wclass']
      if _.wclass == 'HP':
        stats.hp_cont += 1
        stats.hp_shares += _.shares
      else:
        stats.be_cont += 1
        stats.be_shares += _.shares
      # append to dictionary of active containers
      active_containers[_.docker_id] = _
    except docker.errors.APIError:
      print "Problem with docker container"

  # Check container class in K8S
  if params['mode'] == 'k8s':
    # get all best effort pods
    label_selector = 'hyperpilot.io/wclass = BE'
    try:
      pods = kenv.list_pod_for_all_namespaces(watch=False,\
                                            label_selector=label_selector)
      for pod in pods.items:
        if pod.spec.node_name == node.name:
          for cont in pod.status.container_statuses:
            cid = cont.container_id[len('docker://'):]
            if cid in active_containers:
              if active_containers[cid].wclass == 'HP':
                active_containers[cid].wclass = 'BE'
                stats.be_cont += 1
                stats.be_shares += active_containers[cid].shares
                stats.hp_cont -= 1
                stats.hp_shares -= active_containers[cid].shares
              active_containers[cid].k8s_pod_name = pod.metadata.name
              active_containers[cid].k8s_namespace = pod.metadata.namespace
    except (ApiException, TypeError, ValueError):
      print "Cannot talk to K8S API server, labels unknown."
    # get all best effort pods
    label_selector = 'hyperpilot.io/qos=true'
    try:
      pods = kenv.list_pod_for_all_namespaces(watch=False,\
                                            label_selector=label_selector)
      if len(pods.items) > 1:
        print "Multiple QoS tracked workloads, ignoring all but first"
      node.qos_app = pods.items[0].status.container_statuses[0].name
    except (ApiException, TypeError, ValueError, IndexError):
      print "Cannot find QoS service name"

  return active_containers, stats


def CpuStatsDocker(containers):
  """Calculates CPU usage statistics for each container using Docker APIs
  """
  cpu_usage = 0.0
  for _, cont in containers.items():
    try:
      percent = 0.0
      new_stats = cont.docker.stats(stream=False, decode=True)
      new_cpu_stats = new_stats['cpu_stats']
      past_cpu_stats = new_stats['precpu_stats']
      cpu_delta = float(new_cpu_stats['cpu_usage']['total_usage']) - \
                  float(past_cpu_stats['cpu_usage']['total_usage'])
      system_delta = float(new_cpu_stats['system_cpu_usage']) - \
                     float(past_cpu_stats['system_cpu_usage'])
      # The percentages are system-wide, not scaled per core
      if (system_delta > 0.0) and (cpu_delta > 0.0):
        percent = (cpu_delta / system_delta) * 100.0
      cont.cpu_percent = percent
      cpu_usage += percent
    except docker.errors.APIError:
      print "Problem with docker container %s" % cont.docker_name
  return cpu_usage


def CpuStatsK8S(node):
  """Calculates CPU usage statistics using K8S APIs
  """
  try:
    _ = pycurl.Curl()
    data = BytesIO()
    _.setopt(_.URL, node.name + ':10255/stats/summary')
    _.setopt(_.WRITEFUNCTION, data.write)
    _.perform()
    output = json.loads(data.getvalue())
    usage_nano_cores = output['node']['cpu']['usageNanoCores']
    cpu_usage = usage_nano_cores / (node.cpu * 1E9)
    return cpu_usage
  except (ValueError, pycurl.error)  as e:
    print "Problem calculating CpuStatsK8S ", e
    return 100.0

def CpuStats(containers, params, node):
  """ Calculates CPU usage statistics
  """
  if params['mode'] == 'k8s':
    return CpuStatsK8S(node)
  else:
    return CpuStatsDocker(containers)


def SloSlackFile():
  """ Read SLO slack from local file
  """
  with open('slo_slack.txt') as _:
    array = [[float(x) for x in line.split()] for line in _]
  return array[0][0]


def SloSlackQoSDS(name):
  """ Read SLO slack from QoS data store
  """
  print "getting SLO for ", name
  try:
    _ = pycurl.Curl()
    data = BytesIO()
    _.setopt(_.URL, 'qos-data-store:7781/v1/apps/metrics')
    _.setopt(_.WRITEFUNCTION, data.write)
    _.perform()
    output = json.loads(data.getvalue())
    if output['error']:
      print "Problem accessing QoS data store"
      return 0.0
    if name not in output['data']:
      print "QoS datastore does not track workload ", name
    elif 'metrics' not in output['data'][name] or \
       'slack' not in output['data'][name]['metrics']:
      return 0.0
    else:
      return float(output['data'][name]['metrics']['slack'])
  except (ValueError, pycurl.error) as e:
    print "Problem accessing QoS data store ", e
    return 0.0

def SloSlack(name):
  """ Read SLO slack
  """
  return SloSlackQoSDS(name)


def EnableBE(params, node):
  """ enables BE workloads, locally
  """
  if params['mode'] == 'k8s':
    command = 'kubectl label --overwrite nodes ' + node.name + ' hyperpilot.io/be-enabled=true'
    process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, \
                               stderr=subprocess.STDOUT)
    _ = process.wait()


def DisableBE(kenv, params, containers, node):
  """ kills all BE workloads
  """
  if params['mode'] == 'k8s':
    body = client.V1DeleteOptions()
  # kill BE containers
  for _, cont in containers.items():
    if cont.wclass == 'BE':
      # K8s delete pod
      if params['mode'] == 'k8s':
        try:
          _ = kenv.delete_namespaced_pod(cont.k8s_pod_name, \
                  cont.k8s_namespace, body, grace_period_seconds=0, \
                  orphan_dependents=True)
        except ApiException as e:
          print "Cannot kill K8S BE pod: %s\n" % e
      else:
      # docker kill container
        try:
          cont.docker.kill()
        except docker.errors.APIError:
          print "Cannot kill container %s" % cont.name

  # taint local node
  if params['mode'] == 'k8s':
    command = 'kubectl label --overwrite nodes ' + node.name + ' hyperpilot.io/be-enabled=false'
    process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, \
                                 stderr=subprocess.STDOUT)
    _ = process.wait()


def GrowBE(active_containers, params):
  """ grows number of shares for all BE workloads by be_growth_rate
      assumption: non 0 shares
  """
  be_growth_rate = params['BE_growth_rate']
  for _, cont in active_containers.items():
    if cont.wclass == 'BE':
      new_shares = int(be_growth_rate*cont.shares)
      # if initial shares is very small, boost quickly
      if new_shares == cont.shares:
        new_shares = 2 * cont.shares
      cont.shares = new_shares
      try:
        cont.docker.update(cpu_shares=cont.shares)
      except docker.errors.APIError:
        print "Cannot update shares for container %s" % cont.name


def ShrinkBE(active_containers, params):
  """ shrinks number of shares for all BE workloads by be_shrink_rate
      warning: it does not work if shares are 0 to begin with
  """
  be_shrink_rate = params['BE_shrink_rate']
  min_shares = params['min_shares']
  for _, cont in active_containers.items():
    if cont.wclass == 'BE':
      new_shares = int(be_shrink_rate*cont.shares)
      if new_shares == cont.shares:
        new_shares = int(cont.shares/2)
      if new_shares < min_shares:
        new_shares = min_shares
      cont.shares = new_shares
      try:
        cont.docker.update(cpu_shares=cont.shares)
      except docker.errors.APIError:
        print "Cannot update shares for container %s" % cont.name


def ParseArgs():
  """ parse arguments and print config
  """
  global verbose
  # argument parsing
  parser = argparse.ArgumentParser()
  parser.add_argument("-v", "--verbose", help="increase output verbosity", action="store_true")
  parser.add_argument("-c", "--config", type=str, required=False, default="config.json",
                      help="configuration file (JSON)")
  args = parser.parse_args()
  if args.verbose:
    verbose = True

  # read configuration file
  if os.path.isfile(args.config):
    with open(args.config, 'r') as json_data_file:
      try:
        params = json.load(json_data_file)
      except ValueError:
        print "Error in reading configuration file ", args.config
        sys.exit(-1)
  else:
    print "Cannot read configuration file ", args.config
    sys.exit(-1)

  # print configuration parameters
  print "Configuration:"
  for _ in params:
    print "  ", _, params[_]
  print

  return params


def configDocker():
  """ configure Docker environment
      current version does not record node capacity
  """
  # always initialize docker
  try:
    denv = docker.from_env()
    print "Docker API initialized."
  except docker.errors.APIError:
    print "Cannot communicate with docker daemon, terminating."
    sys.exit(-1)
  return denv


def configK8S(params):
  """ configure K8S environment
  """
  node = NodeStats()
  EnableBE(params, node)
  if params['mode'] == 'k8s':
    try:
      config.load_incluster_config()
      kenv = client.CoreV1Api()
      print "K8S API initialized."
    except config.ConfigException:
      print "Cannot initialize K8S environment, terminating."
      sys.exit(-1)
    node.name = os.getenv('MY_NODE_NAME')
    if node.name is None:
      print "Cannot get node name in K8S, terminating."
      sys.exit(-1)
    # read node stats
    try:
      _ = kenv.read_node(node.name)
    except ApiException as e:
      print "Exception when calling CoreV1Api->read_node: %s\n" % e
      sys.exit(-1)
    node.cpu = int(_.status.capacity['cpu'])
  else:
    kenv = client.apis.core_v1_api.CoreV1Api()
  return kenv, node


def __init__():
  """ Main function of shares controller
  """
  # parse arguments
  params = ParseArgs()

  # initialize environment
  denv = configDocker()
  kenv, node = configK8S(params)

  # control loop
  cycle = 0
  while 1:

    # get active containers and their class
    active_containers, stats = ActiveContainers(denv, kenv, params, node)
    # get CPU stats
    cpu_usage = CpuStats(active_containers, params, node)

    # check SLO slack from file
    slo_slack = SloSlack(node.qos_app)

    # grow, shrink or disable control
    if slo_slack < 0.0:
      if verbose:
        print " Disabling phase"
      DisableBE(kenv, params, active_containers, node)
    elif slo_slack < params['slack_threshold_shrink'] or \
         cpu_usage > params['load_threshold_shrink']:
      if verbose:
        print " Shrinking phase"
      ShrinkBE(active_containers, params)
    elif slo_slack > params['slack_threshold_grow'] and \
         cpu_usage < params['load_threshold_grow']:
      if verbose:
        print " Growing phase"
      GrowBE(active_containers, params)
      EnableBE(params, node)
    else:
      EnableBE(params, node)

    if verbose:
      print "Shares controller ", cycle, " at ", dt.now().strftime('%H:%M:%S')
      print " Qos app ", node.qos_app, ", slack ", slo_slack, ", CPU load ", cpu_usage
      print " HP (%d): %d shares" % (stats.hp_cont, stats.hp_shares)
      print " BE (%d): %d shares" % (stats.be_cont, stats.be_shares)
    cycle += 1
    time.sleep(params['period'])

__init__()
