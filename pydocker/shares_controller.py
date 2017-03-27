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
import docker
from kubernetes import client, config
from kubernetes.client.rest import ApiException

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


def ActiveContainers(denv, kenv, params):
  """ Identifies active containers in a docker environment.
  """
  min_shares = params['min_shares']
  active_containers = {}

  # read container list
  try:
    containers = denv.containers.list()
  except docker.errors.APIError:
    print "Cannot communicate with docker daemon, terminating."
    sys.exit(-1)

  for cont in containers:
    _ = Container()
    _.docker_id = cont.id
    _.docker_name = cont.name
    _.docker = cont
    # check container class
    if 'hyperpilot/class' in cont.attrs['Config']['Labels']:
      _.wclass = cont.attrs['Config']['Labels']['hyperpilot/class']
    # check container shares
    _.shares = cont.attrs['HostConfig']['CpuShares']
    if _.shares < min_shares:
      _.shares = min_shares
      cont.update(cpu_shares=_.shares)
    # append to dictionary of active containers
    active_containers[_.docker_id] = _

  # Check container class in K8S
  if params['mode'] == 'k8s':
    # get all best effort pods
    label_selector = 'hyperpilot/class = BE'
    try:
      pods = kenv.list_pod_for_all_namespaces(watch=False,\
                                            label_selector=label_selector)
      for pod in pods.items:
        if pod.spec.node_name == params['node']:
          for cont in pod.status.container_statuses:
            cid = cont.container_id[len('docker://'):]
            if cid in active_containers:
              active_containers[cid].wclass = 'BE'
              active_containers[cid].k8s_pod_name = pod.metadata.name
              active_containers[cid].k8s_namespace = pod.metadata.namespace
    except ApiException:
      print "Cannot talk to K8S API server, labels unknown."

  return active_containers


# Calculates CPU usage statistics for each container
def CpuStats(containers):
  cpu_usage = 0.0
  stats = ControllerStats()
  for _, cont in containers.items():
    percent = 0.0
    print "  2a", dt.now().strftime('%Y-%m-%d %H:%M:%S')
    new_stats = cont.docker.stats(stream=False, decode=True)
    print "  2b", dt.now().strftime('%Y-%m-%d %H:%M:%S')
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
    if cont.wclass == 'HP':
      stats.hp_cont += 1
      stats.hp_shares += cont.shares
      stats.hp_cpu_percent += percent
    else:
      stats.be_cont += 1
      stats.be_shares += cont.shares
      stats.be_cpu_percent += percent
  if cpu_usage > 100.0:
    cpu_usage = 100.0
  return cpu_usage, stats


# Reads SLO slack
# Temp implementation (read file)
def SloSlack():
  with open('slo_slack.txt') as _:
    array = [[float(x) for x in line.split()] for line in _]
  return array[0][0]

# kills all BE workloads
def DisableBE(kenv, params, containers):
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
        cont.docker.kill()

  # taint local node
  # will have to do it by invoking kubectl


# grows number of shares for all BE workloads by be_growth_rate
# assumption: non 0 shares
def GrowBE(active_containers, params):
  be_growth_rate = params['BE_growth_rate']
  for _, cont in active_containers.items():
    if cont.wclass == 'BE':
      new_shares = int(be_growth_rate*cont.shares)
      # if initial shares is very small, boost quickly
      if new_shares == cont.shares:
        new_shares = 2 * cont.shares
      cont.shares = new_shares
      cont.docker.update(cpu_shares=cont.shares)
  return

# shrinks number of shares for all BE workloads by be_shrink_rate
# warning: it does not work if shares are 0 to begin with
def ShrinkBE(active_containers, params):
  be_shrink_rate = params['BE_shrink_rate']
  min_shares = params['min_shares']
  for _, cont in active_containers.items():
    if cont.wclass == 'BE':
      new_shares = int(be_shrink_rate*cont.shares)
      if new_shares == cont.shares:
        new_shares = int(cont.shares/2)
      if new_shares < min_shares:
        new_shares = min_shares
      cont.shares = news_shares
      cont.docker.update(cpu_shares=cont.shares)
  return


def __init__():
  # argument parsing
  parser = argparse.ArgumentParser()
  parser.add_argument("-v", "--verbose", help="increase output verbosity", action="store_true")
  parser.add_argument("-c", "--config", type=str, required=False, default="config.json",
                      help="configuration file (JSON)")
  args = parser.parse_args()

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

  # initialize environment
  # Initialize K8S environment if needed
  if params['mode'] == 'k8s':
    try:
      config.load_incluster_config()
      kenv = client.CoreV1Api()
      print "K8S API initialized."
    except config.ConfigException:
      print "Cannot initialize K8S environment, terminating."
      sys.exit(-1)
    if os.getenv('MY_NODE_NAME') is None:
      print "Cannot get node name in K8S, terminating."
      sys.exit(-1)
    else:
      params['node'] = os.getenv('MY_NODE_NAME')
  else:
    kenv = client.apis.core_v1_api.CoreV1Api()
  # always initialize docker
  try:
    denv = docker.from_env()
    print "Docker API initialized."
  except docker.errors.APIError:
    print "Cannot communicate with docker daemon, terminating."
    sys.exit(-1)
  # controller cycle counter for controller
  cycle = 0

  # control loop
  while 1:

    # get active containers and their class
    print "  1", dt.now().strftime('%Y-%m-%d %H:%M:%S')
    active_containers = ActiveContainers(denv, kenv, params)
    # get CPU stats
    print "  2", dt.now().strftime('%Y-%m-%d %H:%M:%S')
    cpu_usage, stats = CpuStats(active_containers)

    # check SLO slack from file
    print "  3", dt.now().strftime('%Y-%m-%d %H:%M:%S')
    slo_slack = SloSlack()

    # grow, shrink or disable control
    print "  4", dt.now().strftime('%Y-%m-%d %H:%M:%S')
    if slo_slack < 0.0:
      DisableBE(kenv, params, active_containers)
    elif slo_slack < params['slack_threshold_shrink'] or cpu_usage > params['load_threshold_shrink']:
      ShrinkBE(active_containers, params)
      print "Shrinking"
    elif slo_slack > params['slack_threshold_grow'] and cpu_usage < params['load_threshold_grow']:
      GrowBE(active_containers, params)
      print "Growing"

    if args.verbose:
      print "CPU Shares control cycle ", cycle, " at ", dt.now().strftime('%Y-%m-%d %H:%M:%S')
      print " SLO slack ", slo_slack, ", CPU load ", cpu_usage
      print " HP (%d): %d load, %d shares" % (stats.hp_cont, stats.hp_cpu_percent, stats.hp_shares)
      print " BE (%d): %d load, %d shares" % (stats.be_cont, stats.be_cpu_percent, stats.be_shares)
    cycle += 1
    time.sleep(params['period'])

__init__()
