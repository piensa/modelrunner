#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Script to migrate python pickled jobs to json based jobs
"""

from modelrunner import (
    config,
    Job
)

from modelrunner.settings import (
    initialize,
    redis_connection
)

from tornado.options import (
    parse_command_line,
    parse_config_file
)

import pickle

try:
    from cStringIO import StringIO
except ImportError:
    from StringIO import StringIO


# Prevents this script from failing when output is piped
# to another process
from signal import signal, SIGPIPE, SIG_DFL
signal(SIGPIPE, SIG_DFL)

# so we can load config via cmd line args
parse_command_line()
parse_config_file(config.options.config_file)

# initialize the global application settings
initialize(config.options.redis_url)


# <block> Code to map pickled classes to new namespace
renametable = {
    'job_manager': 'modelrunner.manager',
    'Job': 'Job',
    }


def mapname(name):
    if name in renametable:
        return renametable[name]
    return name


def mapped_load_instance(self):
    module = mapname(self.readline()[:-1])
    name = mapname(self.readline()[:-1])
    klass = self.find_class(module, name)
    self._instantiate(klass, self.marker())


def loads(str):
    file = StringIO(str)
    unpickler = pickle.Unpickler(file)
    unpickler.dispatch[pickle.INST] = mapped_load_instance
    return unpickler.load()
# </block> Code to map pickled classes to new namespace


pickled_objs = redis_connection.hgetall("modelrunner:jobs")
jobs = [loads(pobj[1]) for pobj in pickled_objs.items()]

# write them back as correct instance types
for job in jobs:
    Job[job.uuid] = job
