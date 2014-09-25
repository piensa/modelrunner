import tornado
import sys

# setup config options
import config
from tornado.options import parse_command_line, parse_config_file

import job_manager

if __name__ == "__main__":
    parse_config_file("config.ini")
    parse_command_line()
    # get the command_ keys
    command_dict = config.options.group_dict("model_command")

    jm = job_manager.JobManager(config.options.redis_url, 
                                config.options.primary_url, 
                                config.options.worker_url,
                                config.options.data_dir,
                                command_dict,
                                config.options.worker_is_primary)

    for uuid in [l.rstrip() for l in sys.stdin.readlines()]:
        jm.rdb.hdel("model_runner:jobs", uuid)
