#!/usr/bin/env python

"""
Dynamic CPU shares controller based on the Heracles design
"""

__author__ = "Christos Kozyrakis"
__email__ = "christos@hyperpilot.io"
__copyright__ = "Copyright 2017, HyperPilot Inc"


# Standard
import time
from datetime import datetime as dt
import docker
# External

# Globals
Past_cpu_stats = {}

# Identifies active containers and their parameters
def ActiveContainers(env):
  hp_containers = []
  be_containers = []
  container_shares = {}
  total_shares = 0
  hp_shares = 0
  be_shares = 0
  containers = env.containers.list()

  for cont in containers:
    # container class and shares
    wclass = cont.attrs['Config']['Labels']['wclass']
    shares = cont.attrs['HostConfig']['CpuShares']
    if shares < 0:
      shares = 0
    if wclass == 'BE':
      be_containers.append(cont)
      be_shares += shares
    else:
      hp_containers.append(cont)
      hp_shares += shares
    total_shares += shares
    container_shares[cont.short_id] = shares

  return hp_containers, be_containers, container_shares, \
         total_shares, hp_shares, be_shares


# Calculates CPU usage statistics for each container
def CpuStats(containers):
  container_cpu_percent = {}
  cpu_usage = 0.0
  for cont in containers:
    percent = 0
    new_stats = cont.stats(stream=False, decode=True)
    new_cpu_stats = new_stats['cpu_stats']
    past_cpu_stats = new_stats['precpu_stats']
    cpu_delta = float(new_cpu_stats['cpu_usage']['total_usage']) - \
                float(past_cpu_stats['cpu_usage']['total_usage'])
    system_delta = float(new_cpu_stats['system_cpu_usage']) - \
                    float(past_cpu_stats['system_cpu_usage'])
    if (system_delta > 0.0) and (cpu_delta > 0.0):
      percent = (cpu_delta / system_delta) * 100.0
    container_cpu_percent[cont.short_id] = percent
    cpu_usage += percent
  if cpu_usage > 100.0:
    cpu_usage = 100.0
  return container_cpu_percent, cpu_usage

# Reads SLO slack
def SloSlack():
  with open('slo_slack.txt') as f:
    array = [[float(x) for x in line.split()] for line in f]
  return array[0][0]

# kills all BE workloads
def DisableBE(be_containers):
  for cont in be_containers:
    cont.kill()

# grows number of shares for all BE workloads by 10%
# warning: it does not work if shares are 0 to begin with
def GrowBE(be_containers, be_shares):
  for cont in be_containers:
    old_shares = be_shares[cont.short_id]
    new_shares = int(1.1*old_shares)
    cont.update(cpu_shares=new_shares)
  return

# shrinks number of shares for all BE workloads by 10%
# warning: it does not work if shares are 0 to begin with
def ShrinkBE(be_containers, be_shares):
  for cont in be_containers:
    old_shares = be_shares[cont.short_id]
    new_shares = int(0.9*old_shares)
    if new_shares < 2: # docker limit
      new_shares = 2
    cont.update(cpu_shares=new_shares)
  return

def main():
  # init
  cycle = 0
  env = docker.from_env()

  # control loop
  while 1:

    # get active containers and their class
    (hp_containers, be_containers, container_shares, \
     total_shares, hp_shares, be_shares) = ActiveContainers(env)

    # get CPU stats
    (container_cpu_percent, cpu_usage) = CpuStats(hp_containers + be_containers)

    # check SLO slack from file
    slo_slack = SloSlack()
    print "HP containers ", len(hp_containers), ", BE containers ", len(be_containers)
    print "SLO slack ", slo_slack, ", Load ", cpu_usage
    print total_shares, hp_shares, be_shares

    # grow, shrink or disable control
    if slo_slack < 0.0:
      DisableBE(be_containers)
    elif slo_slack < 0.05:
      ShrinkBE(be_containers, container_shares)
    elif slo_slack > 0.1: # and cpu_usage < 90.0:
      GrowBE(be_containers, container_shares)

    print "CPU Shares control cycle ", cycle, " at ", dt.now(), "\n"
    cycle += 1
    time.sleep(2)

if __name__ == "__main__":
  main()
