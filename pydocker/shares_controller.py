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
    return "<Container:%s pod:%d class:%s shares:%d>" \
           % (self.docker_name, self.k8s_pod_name, self.wclass, self.shares)

  def __str__(self):
    return "<Container:%s pod:%d class:%s shares:%d>" \
           % (self.docker_name, self.k8s_pod_name, self.wclass, self.shares)


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
  for _ in containers:
    percent = 0.0
    new_stats = _.docker.stats(stream=False, decode=True)
    new_cpu_stats = new_stats['cpu_stats']
    past_cpu_stats = new_stats['precpu_stats']
    cpu_delta = float(new_cpu_stats['cpu_usage']['total_usage']) - \
                float(past_cpu_stats['cpu_usage']['total_usage'])
    system_delta = float(new_cpu_stats['system_cpu_usage']) - \
                    float(past_cpu_stats['system_cpu_usage'])
    # The percentages are system-wide, not scaled per core
    if (system_delta > 0.0) and (cpu_delta > 0.0):
      percent = (cpu_delta / system_delta) * 100.0
    _.cpu_percent = percent
    cpu_usage += percent
  if cpu_usage > 100.0:
    cpu_usage = 100.0
  return cpu_usage


# Reads SLO slack
# Temp implementation (read file)
def SloSlack():
  with open('slo_slack.txt') as _:
    array = [[float(x) for x in line.split()] for line in _]
  return array[0][0]

# kills all BE workloads
def DisableBE(kenv, params, containers):
  body = kenv.client.V1DeleteOptions()
  # kill BE containers
  for cont in containers:
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

# grows number of shares for all BE workloads by be_growth_rate
# assumption: non 0 shares
def GrowBE(be_containers, be_shares, be_growth_rate):
  for cont in be_containers:
    old_shares = be_shares[cont.short_id]
    new_shares = int(be_growth_rate*old_shares)
    cont.update(cpu_shares=new_shares)
  return

# shrinks number of shares for all BE workloads by be_shrink_rate
# warning: it does not work if shares are 0 to begin with
def ShrinkBE(be_containers, be_shares, be_shrink_rate, min_shares):
  for cont in be_containers:
    old_shares = be_shares[cont.short_id]
    new_shares = int(be_shrink_rate*old_shares)
    if new_shares < min_shares:
      new_shares = min_shares
    cont.update(cpu_shares=new_shares)
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
    active_containers = ActiveContainers(denv, kenv, params)
    # get CPU stats
    cpu_usage = CpuStats(active_containers)

    # check SLO slack from file
    slo_slack = SloSlack()

    # grow, shrink or disable control
    if slo_slack < 0.0:
      DisableBE(kenv, params, active_containers)
    elif slo_slack < slack_threshold_shrink or cpu_usage > load_threshold_shrink:
      ShrinkBE(be_containers, container_shares, be_shrink_rate, min_shares)
    elif slo_slack > slack_threshold_grow and cpu_usage < load_threshold_grow:
      GrowBE(be_containers, container_shares, be_growth_rate)

    if args.verbose:
      print "CPU Shares control cycle ", cycle, " at ", dt.now().strftime('%Y-%m-%d %H:%M:%S')
      print " HP %d (%d shares), BE %d (%d shares)" \
            %(len(hp_containers), hp_shares, len(be_containers), be_shares)
      print " SLO slack ", slo_slack, ", CPU load ", cpu_usage
    cycle += 1
    time.sleep(period)

__init__()
