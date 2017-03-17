#!/usr/bin/env python

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


# Standard
import time
from datetime import datetime as dt
import sys
import json
import argparse
import os.path
import docker

# Globals
Past_cpu_stats = {}

# Identifies active containers and their parameters
def ActiveContainers(env, min_shares):
  hp_containers = []
  be_containers = []
  container_shares = {}
  hp_shares = 0
  be_shares = 0
  # attempt to get container list
  try:
    containers = env.containers.list()
  except docker.errors.APIError:
    print "Cannot communicate with docker daemon, terminating."
    sys.exit(-1)

  for cont in containers:
    # container class and shares
    # default class is high priority
    if 'wclass' in cont.attrs['Config']['Labels']:
      wclass = cont.attrs['Config']['Labels']['hyperpilot/wclass']
    else:
      wclass = 'HP'
    shares = cont.attrs['HostConfig']['CpuShares']
    if shares < min_shares:
      shares = min_shares
      cont.update(cpu_shares=shares)
    if wclass == 'BE':
      be_containers.append(cont)
      be_shares += shares
    else:
      hp_containers.append(cont)
      hp_shares += shares
    container_shares[cont.short_id] = shares

  return hp_containers, be_containers, container_shares, \
         hp_shares, be_shares


# Calculates CPU usage statistics for each container
def CpuStats(containers):
  container_cpu_percent = {}
  cpu_usage = 0.0
  for cont in containers:
    percent = 0.0
    new_stats = cont.stats(stream=False, decode=True)
    new_cpu_stats = new_stats['cpu_stats']
    past_cpu_stats = new_stats['precpu_stats']
    cpu_delta = float(new_cpu_stats['cpu_usage']['total_usage']) - \
                float(past_cpu_stats['cpu_usage']['total_usage'])
    system_delta = float(new_cpu_stats['system_cpu_usage']) - \
                    float(past_cpu_stats['system_cpu_usage'])
    # The percentages are system-wide, not scaled per core
    if (system_delta > 0.0) and (cpu_delta > 0.0):
      percent = (cpu_delta / system_delta) * 100.0
    container_cpu_percent[cont.short_id] = percent
    cpu_usage += percent
  if cpu_usage > 100.0:
    cpu_usage = 100.0
  return container_cpu_percent, cpu_usage


# Reads SLO slack
# Temp implementation (read file)
def SloSlack():
  with open('slo_slack.txt') as f:
    array = [[float(x) for x in line.split()] for line in f]
  return array[0][0]

# kills all BE workloads
def DisableBE(be_containers):
  for cont in be_containers:
    cont.kill()

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


def main():
  # configuration
  parser = argparse.ArgumentParser()
  parser.add_argument("-c", "--config", type=str, required=False, default="config.json",
                      help="configuration file (JSON)")
  parser.add_argument("-v", "--verbose", help="increase output verbosity", action="store_true")
  args = parser.parse_args()

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

  # simpler parameters
  print "Configuration:"
  for x in params:
    print "  ", x, params[x]
  print "\n"
  period = params['period']
  load_threshold_grow = params['load_threshold_grow']
  load_threshold_shrink = params['load_threshold_shrink']
  slack_threshold_shrink = params['slack_threshold_shrink']
  slack_threshold_grow = params['slack_threshold_grow']
  be_growth_rate = params['BE_growth_rate']
  be_shrink_rate = params['BE_shrink_rate']
  min_shares = params['min_shares']

  # init
  cycle = 0
  try:
    env = docker.from_env()
  except docker.errors.APIError:
    print "Cannot communicate with docker daemon, terminating."
    sys.exit(-1)

  # control loop
  while 1:

    # get active containers and their class
    (hp_containers, be_containers, container_shares, \
     hp_shares, be_shares) = ActiveContainers(env, min_shares)

    # get CPU stats
    (container_cpu_percent, cpu_usage) = CpuStats(hp_containers + be_containers)

    # check SLO slack from file
    slo_slack = SloSlack()

    # grow, shrink or disable control
    if slo_slack < 0.0:
      DisableBE(be_containers)
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

if __name__ == "__main__":
  main()
