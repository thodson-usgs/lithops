#
# (C) Copyright Cloudlab URV 2020
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import pika
import os
import sys
import uuid
import json
import logging
import flask
import time
import requests
from functools import partial
from multiprocessing import Process, Value

from lithops.version import __version__
from lithops.utils import setup_lithops_logger, b64str_to_dict
from lithops.worker import function_handler
from lithops.worker.utils import get_runtime_metadata
from lithops.constants import JOBS_PREFIX
from lithops.storage.storage import InternalStorage

from lithops.serverless.backends.k8s import config

logger = logging.getLogger('lithops.worker')

proxy = flask.Flask(__name__)

JOB_INDEXES = {}


@proxy.route('/get-range/<jobkey>/<total_calls>/<chunksize>', methods=['GET'])
def get_range(jobkey, total_calls, chunksize):
    global JOB_INDEXES

    range_start = 0 if jobkey not in JOB_INDEXES else JOB_INDEXES[jobkey]
    range_end = min(range_start + int(chunksize), int(total_calls))
    JOB_INDEXES[jobkey] = range_end

    range = "-1" if range_start == int(total_calls) else f'{range_start}-{range_end}'
    remote_host = flask.request.remote_addr
    proxy.logger.info(f'Sending range "{range}" to Host {remote_host}')

    return range

def run_master_server():
    # Start Redis Server in the background
    logger.info("Starting redis server in Master Pod")
    os.system("redis-server --bind 0.0.0.0 --daemonize yes")
    logger.info("Redis server started")

    proxy.logger.setLevel(logging.DEBUG)
    proxy.run(debug=True, host='0.0.0.0', port=config.MASTER_PORT, use_reloader=False)

def extract_runtime_meta(payload):
    logger.info(f"Lithops v{__version__} - Generating metadata")

    runtime_meta = get_runtime_metadata()

    internal_storage = InternalStorage(payload)
    status_key = '/'.join([JOBS_PREFIX, payload['runtime_name'] + '.meta'])
    logger.info(f"Runtime metadata key {status_key}")
    dmpd_response_status = json.dumps(runtime_meta)
    internal_storage.put_data(status_key, dmpd_response_status)

def run_job_k8s(payload):
    logger.info(f"Lithops v{__version__} - Starting kubernetes execution")

    os.environ['__LITHOPS_ACTIVATION_ID'] = str(uuid.uuid4()).replace('-', '')[:12]
    os.environ['__LITHOPS_BACKEND'] = 'k8s'

    total_calls = payload['total_calls']
    job_key = payload['job_key']
    worker_processes = payload['worker_processes']
    chunksize = payload['chunksize']

    # Optimize chunksize to the number of processess if necessary
    chunksize = worker_processes if worker_processes > chunksize else chunksize

    call_ids = payload['call_ids']
    data_byte_ranges = payload['data_byte_ranges']

    master_ip = os.environ['MASTER_POD_IP']

    job_finished = False
    while not job_finished:
        call_ids_range = None

        while call_ids_range is None:
            try:
                server = f'http://{master_ip}:{config.MASTER_PORT}'
                url = f'{server}/get-range/{job_key}/{total_calls}/{chunksize}'
                res = requests.get(url)
                call_ids_range = res.text  # for example: 0-5
            except Exception:
                time.sleep(0.1)

        logger.info(f"Received range: {call_ids_range}")
        if call_ids_range == "-1":
            job_finished = True
            continue

        start, end = map(int, call_ids_range.split('-'))
        dbr = [data_byte_ranges[int(call_id)] for call_id in call_ids[start:end]]
        payload['call_ids'] = call_ids[start:end]
        payload['data_byte_ranges'] = dbr
        function_handler(payload)

    logger.info("Finishing kubernetes execution")

def run_job_k8s_rabbitmq(payload, job_index, running_jobs):
    logger.info(f"Lithops v{__version__} - Starting kubernetes execution")

    act_id = str(uuid.uuid4()).replace('-', '')[:12]
    os.environ['__LITHOPS_ACTIVATION_ID'] = act_id
    os.environ['__LITHOPS_BACKEND'] = 'k8s_rabbitmq'
    
    payload['call_ids']  = [payload['call_ids'][job_index]]
    payload['data_byte_ranges'] = [payload['data_byte_ranges'][job_index]]

    function_handler(payload)
    running_jobs.value -= 1

    logger.info("Finishing kubernetes execution")

# Function to calculate the number of executions of this pod
def calculate_executions(num_cpus_cluster, pod_cpus, range_ids_pod, total_functions):
    base_executions = total_functions // num_cpus_cluster
    remaining_executions = total_functions % num_cpus_cluster

    # Calculate the number of executions based on the pod CPUs and the number of executions
    pod_executions = pod_cpus * base_executions
    
    if range_ids_pod[0] <= remaining_executions <= range_ids_pod[1]:
        remaining_executions = remaining_executions - range_ids_pod[0]
        pod_executions = pod_executions + remaining_executions
        return pod_executions, base_executions
    
    if remaining_executions > range_ids_pod[0]:
        pod_executions = pod_executions + pod_cpus

    return pod_executions, base_executions

# Callback to receive the payload and run the jobs
def callback_run_jobs(ch, method, properties, body):
    global range_start, range_end, num_cpus_cluster
    payload = json.loads(body)

    total_calls = payload['total_calls']
    requested_cpus = 0

    logger.info(f"Call from lithops received.")

    try:
        # Calculate the number of executions of this pod
        if total_calls > range_start:
            if range_end > total_calls - 1:
                requested_cpus = total_calls - range_start
            else:
                requested_cpus = range_end - range_start + 1
        else:  # No more executions to do to this pod
            return

        pod_cpus = range_end - range_start + 1
        total_executions, bases_executions = calculate_executions(num_cpus_cluster, pod_cpus, [range_start, range_end], total_calls)
        
        logger.info(f"Total executions: {total_executions}")
        logger.info(f"Starting {requested_cpus} processes")

        running_jobs = Value('i', 0)  # Shared variable to track completed jobs

        # Start the first stack of processes
        num_processes = requested_cpus if total_executions == requested_cpus else pod_cpus
        for i in range(num_processes):
            running_jobs.value += 1
            p = Process(target=run_job_k8s_rabbitmq, args=(payload, range_start + i, running_jobs)).start()

        # Check and start if there is more stacks to run
        if total_executions != requested_cpus:
            total_executions -= pod_cpus
            
            for bases in range(bases_executions + 2):
                execution_id = 0
                while execution_id < pod_cpus and total_executions != 0:
                    if running_jobs.value < pod_cpus:
                        running_jobs.value += 1
                        p = Process(target=run_job_k8s_rabbitmq, args=(payload, (num_cpus_cluster * (bases + 1)) + range_start + execution_id, running_jobs)).start()

                        execution_id += 1
                        total_executions = total_executions - 1

        logger.info(f"All processes completed")
    except:
        # The IDs are not assigned yet
        pass

def start_rabbitmq_listening(payload):
    global range_start, range_end, num_cpus_cluster
    params = pika.URLParameters(payload['amqp_url'])
    connection = pika.BlockingConnection(params)
    channel = connection.channel()

    # Get the range of IDs of this pod
    range_start = payload['range_start']
    range_end = payload['range_end']
    num_cpus_cluster = payload['num_cpus_cluster']
    queue_name = payload['queue_name']

    # Declare and bind exchange to get the payload
    channel.exchange_declare(exchange='lithops', exchange_type='fanout', durable=True)

    # Use a durable queue with a unique name
    channel.queue_declare(queue=queue_name, durable=True)
    channel.queue_bind(exchange='lithops', queue=queue_name)

    # Start listening to the new job
    channel.basic_consume(queue=queue_name, on_message_callback=callback_run_jobs, auto_ack=True)

    logger.info(f"Listening to rabbitmq...")
    channel.start_consuming()

if __name__ == '__main__':
    action = sys.argv[1]
    encoded_payload = sys.argv[2]

    payload = b64str_to_dict(encoded_payload)
    setup_lithops_logger(payload.get('log_level', 'INFO'))

    switcher = {
        'get_metadata': partial(extract_runtime_meta, payload),
        'run_job': partial(run_job_k8s, payload),
        'run_master': run_master_server,
        'start_rabbitmq': partial(start_rabbitmq_listening, payload)
    }

    func = switcher.get(action, lambda: "Invalid command")
    func()
