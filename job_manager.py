"""
Module for managing job running and sync between primary and workers
"""
import os, uuid, datetime
import redis
import re
import pickle
import logging
import urllib2
import threading, subprocess
import zipfile
from zipfile import ZipFile

# utility functions
def fetch_file_from_url(url, destination_dir):

    # http://stackoverflow.com/questions/22676/how-do-i-download-a-file-over-http-using-python
    file_name = url.split('/')[-1] 
    u = urllib2.urlopen(url)
    destination_file = os.path.join(destination_dir, file_name)
    f = open(destination_file, 'wb')
    meta = u.info()
    file_size = int(meta.getheaders("Content-Length")[0])
    logging.info("Downloading: %s Bytes: %s" % (file_name, file_size))

    file_size_dl = 0
    block_sz = 8192
    while True:
        buffer = u.read(block_sz)
        if not buffer:
            break

        file_size_dl += len(buffer)
        f.write(buffer)
        status = r"%10d  [%3.2f%%]" % (file_size_dl, file_size_dl * 100. / file_size)
        status = status + chr(8)*(len(status)+1)
        logging.info(status)

    f.close()

def zipdir(path, zip_file_name):

    output_zip = ZipFile(zip_file_name, 'w')
    for root, dirs, files in os.walk(path):
        for file in files:
            rel_path = os.path.relpath(os.path.join(root, file), path)
            output_zip.write(os.path.join(root, file), arcname=rel_path, compress_type=zipfile.ZIP_DEFLATED)

    output_zip.close()



class WaitForKill(threading.Thread):
    """
    Thread to wait for kill messages while process is running
    """

    def __init__(self, redis_obj, worker_queue, popen_proc):
        threading.Thread.__init__(self)
        self.redis_obj = redis_obj 
        self.worker_queue = worker_queue
        self.popen_proc = popen_proc

    def run(self):
        result = self.redis_obj.blpop(self.worker_queue)
        msg = result[1]
        logging.info("received msg %s" % msg)
        if(msg == "KILL"):
            logging.info("received kill msg on queue %s terminating pid %s" % (self.worker_queue, self.popen_proc.pid))
            self.popen_proc.terminate()


class JobManager:

    STATUS_CREATED   = "CREATED"
    STATUS_QUEUED    = "QUEUED"
    STATUS_RUNNING   = "RUNNING"
    STATUS_PROCESSED = "PROCESSED"
    STATUS_COMPLETE  = "COMPLETE"
    STATUS_FAILED    = "FAILED"

    """ Manage running and syncing job data between primary and workers """
    def __init__(self, redis_url, primary_url, worker_url, data_dir, model_commands, worker_is_primary=True):
        port_re = re.compile(r'(?<=:)\d+$')
        host_re = re.compile(r'^.*(?=:\d+$)')
        redis_host_match = host_re.search(redis_url)
        redis_port_match = port_re.search(redis_url)
        if(not redis_host_match or not redis_port_match):
            raise ValueError("invalid redis url: %s" % redis_url)

        self.rdb = redis.Redis(host=redis_host_match.group(0), 
                               port=redis_port_match.group(0))
        self.primary_url = primary_url
        self.worker_url = worker_url
        self.model_commands = model_commands
        self._worker_is_primary = worker_is_primary
        if(not os.path.exists(data_dir)):
            os.mkdir(data_dir)
        self.data_dir = data_dir

    # wrapper for redis to pickle input
    def hset(self, hash_name, key, obj):
        pickled_obj = pickle.dumps(obj)
        self.rdb.hset(hash_name, key, pickled_obj)

    # wrapper for redis to unpickle 
    def hget(self, hash_name, key):
        pickled_obj = self.rdb.hget(hash_name, key)
        return pickle.loads(pickled_obj)

    # wrapper for redis to unpickle all
    def hgetall(self, hash_name):
        pickled_objs = self.rdb.hgetall(hash_name)
        return [pickle.loads(pobj[1]) for pobj in pickled_objs.items()]

    def get_jobs(self):
        return self.hgetall("model_runner:jobs")

    def get_job(self, job_uuid):
        return self.hget("model_runner:jobs", job_uuid)

    def add_update_job_table(self, job):
        self.hset("model_runner:jobs", job.uuid, job)

    def enqueue(self, job, job_data_blob):
        """ 
        write job data to file and queue up for processing
        intended to be run from primary server
        job_data_blob is a blob of a zip file to be written to disk 
        """
        job_data_dir = os.path.join(self.data_dir, job.uuid)
        if(not os.path.exists(job_data_dir)):
            os.mkdir(job_data_dir)
        
        logging.info("writing input file for job %s to %s" % (job.uuid, job_data_dir))
        file_handle = open(os.path.join(job_data_dir, "input.zip"), 'wb')
        file_handle.write(job_data_blob)
        file_handle.close()

        # add to global job list then queue it to be run
        job.primary_url = self.primary_url
        job.data_dir = self.data_dir # so we know where to get output.zip from
        self.add_update_job_table(job)
        job_queue = "model_runner:queues:%s" % job.model

        logging.info("adding job %s to queue %s" % (job.uuid, job_queue))
        self.rdb.rpush(job_queue, job.uuid)

    def wait_for_new_jobs(self, model_name):
        """ 
        listen for jobs to run as they come in on the model based queue 
        This is meant to be called in an infinite loop as part of a worker.  
        It blocks on waiting for job and while command is being run 
        """
        job_queue = "model_runner:queues:%s" % model_name
        logging.info("waiting for job on queue %s" % job_queue)
        result = self.rdb.blpop(job_queue)
        uuid = result[1]
        job = self.hget("model_runner:jobs", uuid)

        job_data_dir = os.path.join(self.data_dir, job.uuid)
        input_dir = os.path.join(job_data_dir, "input")
        output_dir = os.path.join(job_data_dir, "output")
        # create job data dirs if they don't exist
        if(not os.path.exists(input_dir)):
            os.makedirs(input_dir)

        if(not os.path.exists(output_dir)):
            os.makedirs(output_dir)
 
        logging.info("preparing input for job %s" % job.uuid)
        self._prep_input(job)

        # setup subproc to run model command and output to local job log
        # AND the associated 'kill thread'
        job_data_log = open(os.path.join(job_data_dir, "job.log"), 'w')
        command = self.model_commands[model_name]
        logging.info("starting job %s" % job.uuid)
        # update job status
        job.status = JobManager.STATUS_RUNNING
        job.worker_url = self.worker_url
        self.add_update_job_table(job)

        # add the input and output dir to the command 
        command_args = command.split()
        input_dir = os.path.join(self.data_dir, job.uuid, "input")
        output_dir = os.path.join(self.data_dir, job.uuid, "output")
        command_args.append(os.path.realpath(input_dir))
        command_args.append(os.path.realpath(output_dir))
        command_str = subprocess.list2cmdline(command_args)
        logging.info("running command %s" % command_str)
        popen_proc = subprocess.Popen(command_str, shell=True, stdout=job_data_log, stderr=job_data_log)
        worker_queue = "model_runner:queues:" + self.worker_url
        wk = WaitForKill(self.rdb, worker_queue, popen_proc)
        wk.start()

        # wait for command to finish or for it to be killed
        return_code = popen_proc.wait()
        # close job log
        job_data_log.close()
        logging.info("finished job %s with return code %s" % (job.uuid, return_code))

        if (wk.isAlive()):
            # send a message to stop the wait for kill thread
            self.rdb.rpush(worker_queue, "END")

        logging.info("finished processing job and notifying primary server %s" % self.primary_url)
        primary_queue = "model_runner:queues:" + self.primary_url

        # update job status (use command return code for now)
        if(return_code == 0):  
            logging.info("zipping output of job %s" % job.uuid)
            self._prep_output(job)
            job.status = JobManager.STATUS_PROCESSED 
        else:
            job.status = JobManager.STATUS_FAILED

        self.hset("model_runner:jobs", job.uuid, job)

        # notify primary server job is done
        self.rdb.rpush(primary_queue, job.uuid)


    def wait_for_finished_jobs(self):
        """ 
        listen for jobs that have finished (by workers)
        This is meant to be called in an infinite loop as part of a primary server  
        It blocks while waiting for finished jobs
        """
        primary_queue = "model_runner:queues:" + self.primary_url
        logging.info("waiting for finished jobs on queue %s" % primary_queue)
        result = self.rdb.blpop(primary_queue)
        uuid = result[1]
        job = self.hget("model_runner:jobs", uuid)

        logging.info("job %s finished with status of %s" % (job.uuid, job.status))
        if(job.status == JobManager.STATUS_PROCESSED):
            if(not self.worker_is_primary()): # need to get output
                logging.info("retrieving output for job %s" % job.uuid)
                output_url = job.worker_url + "/" + self.data_dir + "/" + job.uuid + "/output.zip"
                job_data_dir = os.path.join(self.data_dir, job.uuid)
                if(not os.path.exists(job_data_dir)):
                    os.mkdir(job_data_dir)
         
                fetch_file_from_url(output_url, job_data_dir)
                
            job.status = JobManager.STATUS_COMPLETE
            self.hset("model_runner:jobs", job.uuid, job)

    def kill_job(self, job):
        """
        Notify job worker that the job should be killed
        """
        worker_queue = "model_runner:queues:" + job.worker_url
        logging.info("sending message to kill job on %s" % job.worker_url)
        self.rdb.rpush(worker_queue, "KILL")

    def _prep_input(self, job):
        """ fetch (if needed) and unzip data to appropriate dir """
        
        job_data_dir = os.path.join(self.data_dir, job.uuid)
        input_dir = os.path.join(job_data_dir, "input")

        input_zip = os.path.join(job_data_dir, "input.zip")
        if not self.worker_is_primary():
            # need to fetch
            input_url = job.primary_url + "/" + self.data_dir + "/" + job.uuid + "/input.zip"
            logging.info("fetching data from %s" % input_url)
            fetch_file_from_url(input_url, job_data_dir)

        # if we're here, we just need to unzip the input file
        with ZipFile(input_zip, 'r') as zip_file:
            zip_file.extractall(input_dir)

    def _prep_output(self, job):
        """ zip files in the output dir """
        
        job_data_dir = os.path.join(self.data_dir, job.uuid)
        output_zip_name = os.path.join(job_data_dir, "output.zip")

        output_dir = os.path.join(job_data_dir, "output")
        zipdir(output_dir, output_zip_name)


    # whether the machine this is running on is also primary  
    def worker_is_primary(self):
        return self._worker_is_primary

class Job:

    """ Maintain the state of a ModelRunner Job """
    def __init__(self, model):
        self.model = model
        self.uuid = str(uuid.uuid4())
        self.created = datetime.datetime.utcnow()
        self.status = JobManager.STATUS_CREATED


