#
# Copyright Cloudlab URV 2021
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

import signal
import logging
import time
import concurrent.futures as cf
from functools import partial
from lithops.utils import is_unix_system, timeout_handler, is_notebook, is_lithops_worker
from lithops.storage import InternalStorage
from lithops.monitor import JobMonitor
from types import SimpleNamespace

ALL_COMPLETED = 1
ANY_COMPLETED = 2
ALWAYS = 3

logger = logging.getLogger(__name__)


def wait(fs, internal_storage=None, throw_except=True, timeout=None,
         return_when=ALL_COMPLETED, download_results=False,
         THREADPOOL_SIZE=128, WAIT_DUR_SEC=1, job_monitor=None):
    """
    Wait for the Future instances (possibly created by different Executor instances)
    given by fs to complete. Returns a named 2-tuple of sets. The first set, named done,
    contains the futures that completed (finished or cancelled futures) before the wait
    completed. The second set, named not_done, contains the futures that did not complete
    (pending or running futures). timeout can be used to control the maximum number of
    seconds to wait before returning.

    :param fs: Futures list. Default None
    :param throw_except: Re-raise exception if call raised. Default True.
    :param return_when: One of `ALL_COMPLETED`, `ANY_COMPLETED`, `ALWAYS`
    :param download_results: Download results. Default false (Only get statuses)
    :param timeout: Timeout of waiting for results.
    :param THREADPOOL_SIZE: Number of threads to use. Default 64
    :param WAIT_DUR_SEC: Time interval between each check.

    :return: `(fs_done, fs_notdone)`
        where `fs_done` is a list of futures that have completed
        and `fs_notdone` is a list of futures that have not completed.
    :rtype: 2-tuple of list
    """
    if not fs:
        return

    if type(fs) != list:
        fs = [fs]

    if download_results:
        msg = 'ExecutorID {} - Getting results from functions'.format(fs[0].executor_id)
        fs_done = [f for f in fs if f.done]
        fs_not_done = [f for f in fs if not f.done]
        # fs_not_ready = [f for f in futures if not f.ready and not f.done]

    else:
        msg = 'ExecutorID {} - Waiting for functions to complete'.format(fs[0].executor_id)
        fs_done = [f for f in fs if f.ready or f.done]
        fs_not_done = [f for f in fs if not (f.ready or f.done)]
        # fs_not_ready = [f for f in futures if not f.ready and not f.done]

    logger.info(msg)

    if not fs_not_done:
        return fs_done, fs_not_done

    if is_unix_system() and timeout is not None:
        logger.debug('Setting waiting timeout to {} seconds'.format(timeout))
        error_msg = 'Timeout of {} seconds exceeded waiting for function activations to finish'.format(timeout)
        signal.signal(signal.SIGALRM, partial(timeout_handler, error_msg))
        signal.alarm(timeout)

    # Setup progress bar
    pbar = None
    if not is_lithops_worker() and logger.getEffectiveLevel() == logging.INFO:
        from tqdm.auto import tqdm
        if not is_notebook():
            print()
        pbar = tqdm(bar_format='  {l_bar}{bar}| {n_fmt}/{total_fmt}  ',
                    total=len(fs), disable=None)
        pbar.update(len(fs_done))

    try:
        jobs = _create_jobs_from_futures(fs, internal_storage)
        if not job_monitor:
            job_monitor = JobMonitor()
            for job_data in jobs:
                job_monitor.create(**job_data).start()

        if return_when == ALL_COMPLETED:
            while not _all_done(fs, download_results):
                for job_data in jobs:
                    _get_job_data(fs, job_data, pbar=pbar,
                                  throw_except=throw_except,
                                  download_results=download_results,
                                  threadpool_size=THREADPOOL_SIZE,
                                  job_monitor=job_monitor)
                time.sleep(WAIT_DUR_SEC)

        elif return_when == ANY_COMPLETED:
            while not _any_done(fs, download_results):
                for job_data in jobs:
                    _get_job_data(fs, job_data, pbar=pbar,
                                  throw_except=throw_except,
                                  download_results=download_results,
                                  threadpool_size=THREADPOOL_SIZE,
                                  job_monitor=job_monitor)
                time.sleep(WAIT_DUR_SEC)

        elif return_when == ALWAYS:
            for job_data in jobs:
                _get_job_data(fs, job_data, pbar=pbar,
                              throw_except=throw_except,
                              download_results=download_results,
                              threadpool_size=THREADPOOL_SIZE,
                              job_monitor=job_monitor)

    except KeyboardInterrupt as e:
        if download_results:
            not_dones_call_ids = [(f.job_id, f.call_id) for f in fs if not f.done]
        else:
            not_dones_call_ids = [(f.job_id, f.call_id) for f in fs if not f.ready and not f.done]
        msg = ('Cancelled - Total Activations not done: {}'.format(len(not_dones_call_ids)))
        if pbar:
            pbar.close()
            print()
        logger.info(msg)
        raise e

    except Exception as e:
        raise e

    finally:
        if is_unix_system():
            signal.alarm(0)
        if pbar and not pbar.disable:
            pbar.close()
            if not is_notebook():
                print()

    if download_results:
        fs_done = [f for f in fs if f.done]
        fs_notdone = [f for f in fs if not f.done]
    else:
        fs_done = [f for f in fs if f.ready or f.done]
        fs_notdone = [f for f in fs if not f.ready and not f.done]

    return fs_done, fs_notdone


def get_result(fs, throw_except=True, timeout=None,
               THREADPOOL_SIZE=128, WAIT_DUR_SEC=1,
               internal_storage=None):
    """
    For getting the results from all function activations

    :param fs: Futures list. Default None
    :param throw_except: Reraise exception if call raised. Default True.
    :param verbose: Shows some information prints. Default False
    :param timeout: Timeout for waiting for results.
    :param THREADPOOL_SIZE: Number of threads to use. Default 128
    :param WAIT_DUR_SEC: Time interval between each check.
    :return: The result of the future/s
    """
    if type(fs) != list:
        fs = [fs]

    fs_done, _ = wait(fs=fs, throw_except=throw_except,
                      timeout=timeout, download_results=True,
                      internal_storage=internal_storage,
                      THREADPOOL_SIZE=THREADPOOL_SIZE,
                      WAIT_DUR_SEC=WAIT_DUR_SEC)
    result = []
    fs_done = [f for f in fs_done if not f.futures and f._produce_output]
    for f in fs_done:
        result.append(f.result(throw_except=throw_except))

    logger.debug("ExecutorID {} - Finished getting results".format(fs[0].executor_id))

    return result


def _create_jobs_from_futures(fs, internal_storage):
    """
    Creates a dummy job necessary for the job monitor
    """
    jobs = []
    present_jobs = {f.job_key for f in fs}

    for job_key in present_jobs:
        job_data = {}
        job = SimpleNamespace()
        job.monitoring = 'Storage'
        job.futures = [f for f in fs if f.job_key == job_key]
        job.total_calls = len(job.futures)
        f = job.futures[0]
        job.executor_id = f.executor_id
        job.job_id = f.job_id
        job.job_key = f.job_key
        job_data['job'] = job

        if internal_storage and internal_storage.backend == f._storage_config['backend']:
            job_data['internal_storage'] = internal_storage
        else:
            job_data['internal_storage'] = InternalStorage(f._storage_config)

        jobs.append(job_data)

    return jobs


def _all_done(fs, download_results):
    """
    Checks if all futures are ready or done
    """
    if download_results:
        return all([f.done for f in fs])
    else:
        return all([f.ready or f.done for f in fs])


def _any_done(fs, download_results):
    """
    Checks if any futures irs ready or done
    """
    if download_results:
        return any([f.done for f in fs])
    else:
        return any([f.ready or f.done for f in fs])


def _get_job_data(fs, job_data, download_results, throw_except, threadpool_size, pbar, job_monitor):
    """
    Downloads all status/results from ready futures
    """
    job = job_data['job']
    internal_storage = job_data['internal_storage']

    callids_done = [(f.executor_id, f.job_id, f.call_id)
                    for f in job.futures if f._call_status_ready]

    if download_results:
        not_done_futures = [f for f in job.futures if not f.done]
    else:
        not_done_futures = [f for f in job.futures if not (f.ready or f.done)]

    not_done_call_ids = set([(f.executor_id, f.job_id, f.call_id) for f in not_done_futures])
    done_call_ids = not_done_call_ids.intersection(callids_done)

    fs_to_wait_on = []
    for f in job.futures:
        if (f.executor_id, f.job_id, f.call_id) in done_call_ids:
            fs_to_wait_on.append(f)

    def get_result(f):
        f.result(throw_except=throw_except, internal_storage=internal_storage)

    def get_status(f):
        f.status(throw_except=throw_except, internal_storage=internal_storage)

    pool = cf.ThreadPoolExecutor(max_workers=threadpool_size)
    if download_results:
        list(pool.map(get_result, fs_to_wait_on))
    else:
        list(pool.map(get_status, fs_to_wait_on))
    pool.shutdown()

    if pbar:
        for f in fs_to_wait_on:
            if (download_results and f.done) or \
               (not download_results and (f.ready or f.done)):
                pbar.update(1)
        pbar.refresh()

    # Check for new futures
    new_futures = [f.result() for f in fs_to_wait_on if f.futures]
    if new_futures:
        for futures in new_futures:
            job.futures.extend(futures)
            fs.extend(futures)
            if pbar:
                pbar.total = pbar.total + len(futures)
                pbar.refresh()
        if not job_monitor.is_alive(job.job_key):
            # this is only for storage monitor
            job_monitor.create(**job_data).start()
