#!/usr/bin/python
from multiprocessing.connection import Client
from time import sleep
from sys import exit as sysexit

address = '/tmp/sock-phoebe-player_dev'

conn = Client(address, authkey='phoebe')

try:
    conn.send(['play'])
except IOError:
    print "IO error encountered when attempting to send to player connection"

sleep(10)

try:
    conn.send(['seek', 10])
except IOError:
    print "IO error encountered when attempting to send to player connection"

try:
    print conn.recv()
except EOFError:
    print "EOF encountered when attempting to read from player connection"
    sysexit()

sleep(2)

try:
    conn.send(['getpos'])
except IOError:
    print "IO error encountered when attempting to send to player connection"

try:
    print conn.recv()
except EOFError:
    print "EOF encountered when attempting to read from player connection"
    sysexit()

sleep(2)

try:
    conn.send(['stop'])
except IOError:
    print "IO error encountered when attempting to send to player connection"


conn.close()
