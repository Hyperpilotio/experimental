#!/usr/bin/env python

"""
An infininite loop program that continuously tests if some numbers
are prime and reports the time it took to calculate
"""

__author__ = "Christos Kozyrakis"
__email__ = "christos@hyperpilot.io"
__copyright__ = "Copyright 2017, HyperPilot Inc"


# Standard
import math
from datetime import datetime as dt
# External

N = 150

def IsPrime(number):
  if number == 2:
    return True
  if number % 2 == 0 or number <= 1:
    return False
  sqr = int(math.sqrt(number)) + 1
  for divisor in range(3, sqr, 2):
    if number % divisor == 0:
      return False
  return True

def main():
  while 1:
    time1 = dt.now()
    for _ in range(0, N):
      IsPrime(100000015021)
      IsPrime(100000015019)
      time2 = dt.now()
      delta = time2 - time1
    pps = float((N*2*1000000)) / float((delta.seconds*1000000 + delta.microseconds))
    print "Primes per second = %0.2f" %(pps)

if __name__ == "__main__":
  main()
