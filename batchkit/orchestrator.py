# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import copy
import errno
import time
import traceback
import multiprocessing
from multiprocessing import Lock, RLock, Condition, Event, Queue
from multiprocessing.process import current_process
from threading import Thread
from typing import Dict, Optional, List
from pyinotify import ThreadedNotifier

from .batch_request import BatchRequest
from .batch_status import BatchStatusProvider, BatchStatusEnum
from .endpoint_manager import EndpointManager
from .endpoint_status import EndpointStatusChecker, UnknownEndpointStatusChecker
from .handlers import notify_file_modified
from .endpoint_config import load_configuration
from .utils import write_json_file_atomic, write_single_output_json, \
    current_threads_stacktrace, BatchNotFoundException, InvalidConfigurationError
from .logger import LogEventQueue
from .run_summarizer import BatchRunSummarizer
from .work_item import WorkItemResult, WorkItemRequest, SentinelWorkItemRequest, WorkItemQueue
from .work_item_processor import WorkItemProcessor, StubWorkItemProcessor
from .constants import ORCHESTRATOR_SCOPE_MAX_RETRIES, RUN_SUMMARY_LOOP_INTERVAL


class Orchestrator:
    def __init__(self, submission_queue: multiprocessing.Queue, status_provider: BatchStatusProvider,
                 config_file: str, strict_config: bool, log_folder: str,
                 cache_search_dirs: List[str], log_event_que: LogEventQueue,
                 debug_loop_interval: int,
                 singleton_run_summary_path: Optional[str] = None):

        self._submission_que: multiprocessing.Queue = submission_queue
        self._status_provider: BatchStatusProvider = status_provider
        self._config_file: str = config_file
        self._strict_config: bool = strict_config
        self._log_folder: str = log_folder
        self._cache_search_dirs = cache_search_dirs
        self._log_event_que = log_event_que
        self._debug_loop_interval: int = debug_loop_interval
        self._singleton_run_summary_path = singleton_run_summary_path
        self._on_batch_id = -1
        self._on_batch_type: type = type(None)

        self._master_thread = Thread(target=self._master_thread_loop,
                                     name="OrchestratorMasterThread",
                                     args=(()),
                                     daemon=True)

        self._run_summary_thread_gate = Event()
        self._run_summary_thread = Thread(target=self._run_summary_loop,
                                          name="OrchestratorRunSummaryThread",
                                          args=(()),
                                          daemon=True)

        self.__debug_loop_thread = Thread(target=self.__debug_loop,
                                          name="OrchestratorDebugLoop",
                                          args=(()),
                                          daemon=True)

        #TODO(andwald): The following hypothetical thread dynamically sets RTF and Concurrency of EndpointManagers
        #               according to its own decoupled logic. This will be nice and pluggable since EndpointManagers
        #               already adhere to whatever the dynamic settings are for the Atomic Shared Variables of
        #               RTF and Concurrency, which is what this thread will manipulate.
        # self._perf_thread = Thread(target=self.perf_thread_loop, name="OrchestratorPerfThread", args=(self,), daemon=True)

        self._file_queue: WorkItemQueue = WorkItemQueue(log_event_que)
        self._file_queue_size: int = 0
        self._in_progress: Dict[str, WorkItemRequest] = {}  # WorkItemRequest.filepath -> WorkItemRequest
        self._in_progress_owner: Dict[str, EndpointManager] = {}  # WorkItemRequest.filepath -> EndpointManager
        self._work_results: Dict[str, Optional[WorkItemResult]] = {}  # WorkItemRequest.filepath -> WorkItemResult
                                                                      # None value implies no attempt started.
        self._batch_completion_evt = Event()
        self._accounting_lock = RLock()
        self._file_queue_cond = Condition(self._accounting_lock)
        self._run_summary_lock = Lock()
        self._misc_lock = Lock()
        self._summarizer: BatchRunSummarizer = None
        self._stop_requested = False

        # Batchkit framework offers work items a global (Orchestrator-level) reentrant lock for
        # odd cases where particular applications require a global critical section(s) inside work items.
        self._global_workitem_lock = RLock()

        # Endpoint Managers state.
        self._endpoint_managers: List[EndpointManager] = []
        self._endpoint_generation = 0
        self._old_managers = set()  # Set[str], contains names of now-inactive endpoint managers
        self._config_notifier: ThreadedNotifier = \
            notify_file_modified(self._config_file, self.hotswap_endpoint_managers, self._log_event_que)

        self._start_time = time.time()
        self._creator_pid = current_process().pid
        self._log_event_que.info("Orchestrator created by process: {0}".format(self._creator_pid))
        self.__cnt_work_success_cb = 0
        self.__cnt_work_failure_cb = 0

        self._master_thread.start()
        self._run_summary_thread.start()

        # Enable to debug concurrency changes.
        if self._debug_loop_interval > 0:
            self.__debug_loop_thread.start()

    def is_alive(self):
        return self._master_thread.is_alive()

    def join(self):
        self._master_thread.join()

    def _run_summary_loop(self):
        while not self._stop_requested:
            # Prevent redundant updates when nothing can change.
            self._run_summary_thread_gate.wait()
            if self._stop_requested:
                return

            if self._on_batch_id > -1 and self._summarizer:
                try:
                    self.write_summary_information(write_run_summary=True, write_retries=5, log_conclusion_msg=False)

                # Don't ever let this thread die as it's too important.
                # Log and re-try. Repetitive failure loop will at least get logged.
                except Exception as e:
                    exception_details = traceback.format_exc()
                    self._log_event_que.error("Orchestrator: run_summary_thread in run_summary_loop(): "
                                              "Caught {0}, \nDetails: {1}".format(
                                                type(e).__name__, exception_details))

            time.sleep(RUN_SUMMARY_LOOP_INTERVAL)

    def __debug_loop(self):
        """
        This is only intended to be used during development and debugging. The reported numbers are not
        thread-safe so this is only intended to be used in a deadlock scenario.
        """
        def _check_lock_acq(lock):
            acquired = lock.acquire(block=False)
            if acquired:
                lock.release()
                return False
            # We weren't able to acquire, so it's taken
            return True

        # Loop forever. This is a daemonic thread and it will intentionally
        # only die when the process owning Orchestrator dies.
        last_cnt_work_success = 0
        logger = self._log_event_que
        while True:
            logger.debug("Stop requested: {0}".format(self._stop_requested))
            logger.debug("Batch que size: {0}".format(self._submission_que.qsize()))
            logger.debug("On batch id: {0}".format(self._on_batch_id))
            logger.debug("File queue size: {0}".format(self._file_queue_size))
            logger.debug("Num in progress: {0}".format(len(self._in_progress)))
            logger.debug("Orchestrator accounting lock taken: {0}".format(_check_lock_acq(self._accounting_lock)))
            logger.debug("Status provider accounting lock taken: {0}".format(_check_lock_acq(BatchStatusProvider.lock)))
            logger.debug("Notify work success callback entry count: {0}".format(self.__cnt_work_success_cb))
            logger.debug("Work items completed since last debug print: {0}".format(
                self.__cnt_work_success_cb - last_cnt_work_success))
            last_cnt_work_success = self.__cnt_work_success_cb
            logger.debug("Notify work failure callback entry count: {0}".format(self.__cnt_work_failure_cb))
            logger.debug("Run summary thread alive: {0}".format(self._run_summary_thread.is_alive()))
            logger.debug("Number of old endpoint managers: {0}".format(len(self._old_managers)))
            for epm in self._endpoint_managers:
                logger.debug("Endpoint manager: {0}".format(epm.name))
                logger.debug("   Current requests: {0}".format(epm._current_requests))
                logger.debug("   Current requests lock taken: {0}".format(_check_lock_acq(epm._current_requests_lock)))
                logger.debug("   Pool apply_async count: {0}".format(epm._cnt_apply_async))
                logger.debug("   Pool callback count: {0}".format(epm._cnt_pool_cb))
                logger.debug("   Pool callback returns count: {0}".format(epm._cnt_pool_cb_rets))
                logger.debug("   Stop requested: {0}".format(epm._stop_requested))
                logger.debug("   Now trying to steal work: {0}".format(epm._in_steal_work_fn))
            logger.debug("Stack frames of all threads:")
            logger.debug("\n*** STACKTRACE - START ***\n")
            current_threads_stacktrace(use_logger=True)
            logger.debug("\n*** STACKTRACE - END ***\n")
            time.sleep(self._debug_loop_interval)

    def write_summary_information(self,
                                  write_run_summary: bool = True,
                                  write_retries: int = 3,
                                  log_conclusion_msg: bool = False,
                                  allow_fail: bool = False):
        """
        Summarize individual file results, along with overall results,
        and write them to log and/or file. Also log a conclusion message
        if requested.
        :param write_run_summary: whether run summary (individual files + overall)
                                  should be written to file.
        :param write_retries: retries
        :param log_conclusion_msg: whether a conclusion message should be logged
                                   which includes final stats and lists failures.
        :param allow_fail: log failure to write run summary but do not raise exception.
        """
        # To ensure history serialization, we wrap this method
        # in its own lock that nobody else contends with except for
        # the threads that invoke this.
        with self._run_summary_lock:

            # Take a consistent snapshot and then report on the snapshot
            # without holding back forward progress.
            with self._accounting_lock:
                snap_work_results: Dict[str, Optional[WorkItemResult]] = copy.deepcopy(self._work_results)
                snap_file_queue_size: int = self._file_queue_size
                snap_num_running: int = len(self._in_progress)
                snap_run_summarizer: BatchRunSummarizer = self._summarizer
                snap_batch_id: int = self._on_batch_id

            summary_json = {}
            # It's uncommon that a run summarizer wouldn't be available yet but this could
            # happen for example by signaling early termination to the Orchestrator.
            if snap_run_summarizer:
                summary_json = snap_run_summarizer.run_summary(
                    snap_work_results, snap_file_queue_size,
                    snap_num_running, self._start_time, len(self._endpoint_managers),
                    log_conclusion_msg
                )

            # Write the summary json file
            if write_run_summary:
                try:
                    if self._singleton_run_summary_path:
                        self._log_event_que.debug(
                            "Updating singleton run_summary: {0}".format(self._singleton_run_summary_path))
                        write_json_file_atomic(
                            summary_json, self._singleton_run_summary_path, write_retries=write_retries)
                    else:
                        try:
                            self._status_provider.set_run_summary(snap_batch_id, summary_json)
                        except BatchNotFoundException:
                            # This is benign and means we caught a rare race condition
                            # in which the batch directory is very recently deleted.
                            pass
                    # Minimal throttle on file writes. We are under _run_summary_lock.
                    time.sleep(3)
                except Exception as e:
                    self._log_event_que.warning("Failed to write run_summary: {0}".format(str(e)))
                    if not allow_fail:
                        raise

    def request_stop(self):
        """
        Arrange for conditions that will lead to a fast conclusion
        of any ongoing batch without finishing whatever is remaining or
        in progress in this batch if any. This will also cause
        EndpointManagers to shut down. Orchestrator's join() is
        guaranteed to eventually return.
        """
        # Assume this might be called from a signal handler.
        # Instead of preventing child proc inheritance of signals,
        # we eliminate any leaky abstractions by permitting children
        # and those who spawn them to be completely blameless
        # for creating unexpected conditions.
        if current_process().pid != self._creator_pid:
            return

        with self._misc_lock:
            try:
                if self._config_notifier:
                    self._config_notifier.stop()
                    self._config_notifier = None
            except OSError as e:
                # ThreadedNotifier.stop() is not idempotent and gives
                # errno EBADF if it is already stopped.
                if e.errno != errno.EBADF:
                    raise

        # A couple facts about Python3 in case there is any concern
        # about being invoked by a signal handler.
        # 1 - Only the main thread of a process can handle
        # signals, so now we know we are the main thread of the
        # creator process in the signal case.
        # 2 - When running a signal handler, the main thread is
        # is still subject to preemption at tick and the GIL
        # can still be released for other threads. This means
        # that picking up the lock here cannot create deadlock,
        # unless the main thread itself was holding the lock before
        # the signal. That's why we use ReentrantLock.
        with self._accounting_lock:
            self._stop_requested = True
            while self._file_queue_size > 0:
                self._file_queue.get()
                self._file_queue_size -= 1
            self._submission_que.put(None)
            self._file_queue_cond.notify_all()
            self._batch_completion_evt.set()
            for m in self._endpoint_managers:
                m.request_stop()
            self._run_summary_thread_gate.set()

    def cancel_running_batch(self, batch_id: int) -> bool:
        """
        If `batch_id` is running, it will be finished prematurely
        with remaining work items skipped.
        """
        with self._accounting_lock:
            if self._on_batch_id != batch_id:
                return False
            # Drain the work item queue.
            while self._file_queue_size > 0:
                self._file_queue.get()
                self._file_queue_size -= 1
            self._file_queue_cond.notify_all()
            # Drain anything we were tracking as in progress.
            self._in_progress.clear()
            self._in_progress_owner.clear()
            # Have EndpointManagers cancel current work items (activate work item cancellation tokens).
            # These EndpointManagers will be terminally destroyed but would be re-created for a new batch.
            for m in self._endpoint_managers:
                m.request_stop()
                # Ignore any results that come back from EndpointManagers henceforth.
                self._old_managers.add(m.name)
            self._batch_completion_evt.set()
            return True

    def steal_work(self, manager: EndpointManager) -> WorkItemRequest:
        """
        :param manager: the EndpointManager who is trying to steal work.
        :returns str: audio file of work to do
        """
        sentinel = SentinelWorkItemRequest()

        # Classic consumer waiter pattern using condition variable.
        self._accounting_lock.acquire()
        while True:
            if manager.name in self._old_managers or self._stop_requested:
                work = sentinel
                break
            if self._file_queue_size > 0:
                work: WorkItemRequest = self._file_queue.get()
                self._file_queue_size -= 1

                # Eliminate this manager early if we detect a language mismatch.
                # It will be recreated on a new batch.
                if work.language and manager.endpoint_config["language"].lower() != work.language.lower():
                    self._file_queue.put(work)  # back on queue for someone qualified
                    self._file_queue_size += 1
                    self._file_queue_cond.notify()
                    work = sentinel
                    break

                # Got some work to do!
                self._in_progress[work.filepath] = work
                self._in_progress_owner[work.filepath] = manager
                break
            else:
                # Back to sleep because we got nothing.
                self._file_queue_cond.wait()  # implicit self.accounting_lock.release()
        self._accounting_lock.release()
        return work

    def _merge_results(self, filepath: str, result: WorkItemResult):
        with self._accounting_lock:
            if filepath not in self._work_results or not self._work_results[filepath]:
                self._work_results[filepath] = result
            else:
                prev_attempts = self._work_results[filepath].attempts
                result.attempts += prev_attempts
                self._work_results[filepath] = result

    def notify_work_success(self, filepath: str, manager: EndpointManager, result: WorkItemResult):
        with self._accounting_lock:
            self.__cnt_work_success_cb += 1
            if manager.name in self._old_managers:
                # The AudioFileWork item would already be back in pending
                # or running by someone else or finished. Covers an uncommon race.
                return
            if self._stop_requested:
                # It's too late for updating batch status and we're about to die.
                return
            del self._in_progress[filepath]
            del self._in_progress_owner[filepath]

            self._merge_results(filepath, result)

            # Did we just finish the batch?
            if self._file_queue_size == 0 and len(self._in_progress) == 0:
                self._batch_completion_evt.set()

    def notify_work_failure(self, filepath: str, manager: EndpointManager, result: WorkItemResult):
        with self._accounting_lock:
            self.__cnt_work_failure_cb += 1
            if manager.name in self._old_managers:
                # The WorkItemResult would already be back in pending
                # or running by someone else or finished. Covers an uncommon race.
                return
            if self._stop_requested:
                # It's too late for updating batch status and we're about to die.
                return

            self._merge_results(filepath, result)

            # Do we give it another chance?
            # Check retry-ability and num retries burned already.
            if result.can_retry and \
                    self._work_results[filepath].attempts - 1 < ORCHESTRATOR_SCOPE_MAX_RETRIES:
                self._log_event_que.debug("Placed work item {0} back into queue since retriable.".format(filepath))
                self._file_queue.put(self._in_progress[filepath])
                self._file_queue_size += 1
                self._file_queue_cond.notify()
            # Else no more retries.
            # Either way the item is no longer in progress.
            del self._in_progress[filepath]
            del self._in_progress_owner[filepath]

            # Did we just finish the batch? E.g. finally gave up on this work
            # item and that so happens to be the last in the batch.
            if self._file_queue_size == 0 and len(self._in_progress) == 0:
                self._batch_completion_evt.set()

    def hotswap_endpoint_managers(self):
        try:
            config_data = load_configuration(self._config_file, self._strict_config)
        except InvalidConfigurationError:
            self._log_event_que.error(
                "Invalid endpoint configuration file: {0}. Overwrite for another hot-swap.".format(self._config_file))
            return

        with self._accounting_lock:
            if self._stop_requested:
                return

            # Get the unique generation of these endpoint managers, which
            # is useful for both debugging and logging.
            gen = self._endpoint_generation
            self._endpoint_generation += 1

            # Get an EndpointStatusChecker and WorkItemProcessor for the type of the
            # BatchRequest that is currently being processed.
            ep_status_checker: EndpointStatusChecker
            work_item_processor: WorkItemProcessor
            if isinstance(None, self._on_batch_type):
                ep_status_checker = UnknownEndpointStatusChecker(self._log_event_que)
                work_item_processor = StubWorkItemProcessor()
            else:
                ep_status_checker = self._on_batch_type.get_endpoint_status_checker(self._log_event_que)
                work_item_processor = self._on_batch_type.get_work_item_processor()

            try:
                # Determine EndpointManagers that need to be deleted (modified, new,
                # or no longer exist). Do not touch EndpointManagers that have not changed unless they
                # now need a new WorkItemProcessor.
                new_em_objs: List[EndpointManager] = []
                # Start by assuming every EndpointManager needs to be deleted.
                deleted_managers: Dict[str, EndpointManager] = \
                    {em.endpoint_name: em for em in self._endpoint_managers}

                for endpoint_name, endpoint_config in config_data.items():
                    # If an existing endpoint is totally preserved in the new config, don't delete it.
                    # Also require that the endpoint's manager is not terminally stopped, and also require its
                    # WorkItemProcessor type shouldn't change, otherwise we need a new instance of it anyway.
                    if endpoint_name in deleted_managers and \
                      endpoint_config == deleted_managers[endpoint_name].endpoint_config and \
                      type(deleted_managers[endpoint_name].work_item_processor) == type(work_item_processor) and \
                      not deleted_managers[endpoint_name]._stop_requested:  # noqa
                        # Don't delete this EndpointManager and don't make a new one.
                        del deleted_managers[endpoint_name]
                        continue

                    new_em_objs.append(
                        EndpointManager(
                            "HotswapGen{0}_{1}".format(str(gen), endpoint_name),
                            endpoint_name,
                            endpoint_config,
                            self._log_folder,
                            self._log_event_que,
                            self._cache_search_dirs,
                            # on EndpointManager has capacity to steal work
                            self.steal_work,
                            # on EndpointManager reports success
                            self.notify_work_success,
                            # on EndpointManager reports failure
                            self.notify_work_failure,
                            ep_status_checker,
                            self._global_workitem_lock,
                            work_item_processor,
                        )
                    )
            # Validation of the config could fail or invalid yaml may have been given, etc.
            # We catch anything so that we may permit another attempt later with a proper config file.
            # We report it in the logs and somewhere else we will die if no forward progress for too long.
            except Exception as e:
                exception_details = traceback.format_exc()
                self._log_event_que.error("Caught Exception '{0}' reading config. Details: {1}\n{2}".format(
                    type(e).__name__, str(e), exception_details))
                # Don't proceed to stop the old EndpointManagers because they're all we've got to go on.
                return
            if self._stop_requested:
                return

            # Also swap the EndpointManagers under lock in case of race.
            # First stop the old EndpointManagers to be deleted.
            for m in self._endpoint_managers:
                if m.endpoint_name in deleted_managers:
                    self._old_managers.add(m.name)
                    m.request_stop()

            # Un-assign work in progress for deleted EndpointManagers.
            # Now anything the old managers might still callback
            # would be rejected so we can safely move in progress back to queue.
            work_in_progress = {k: v for k, v in self._in_progress.items()}  # shallow copy
            work_in_progress_owner = {k: v for k, v in self._in_progress_owner.items()}  # shallow copy
            for filepath, work_item in self._in_progress.items():
                owner_endpoint_name = self._in_progress_owner[filepath].endpoint_name
                # If the EndpointManager that owns this work item is being deleted,
                # free up the work item.
                if owner_endpoint_name in deleted_managers:
                    del work_in_progress[filepath]
                    del work_in_progress_owner[filepath]
                    self._file_queue.put(work_item)
                    self._file_queue_size += 1
            self._in_progress = work_in_progress
            self._in_progress_owner = work_in_progress_owner

            # We've potentially repopulated the file_queue, and old endpoint managers who were blocked waiting
            # for work should now be woken up to be told of their termination.
            self._file_queue_cond.notify_all()

            # Start the new EndpointManagers.
            for m in new_em_objs:
                m.start()

            # Record the latest set of all EndpointManagers.
            self._endpoint_managers = \
                [em for em in self._endpoint_managers
                 if em.endpoint_name not in deleted_managers] + \
                new_em_objs

            # Ensure that they are all using the correct type of EndpointStatusChecker
            # which depends on the subtype of BatchRequest we are currently processing.
            for m in self._endpoint_managers:
                m.set_endpoint_status_checker(ep_status_checker)

        self._log_event_que.info("Set new EndpointManagers after hot-swap: {0}".format(config_data))

    def _master_finalize(self):
        """
        Work to be done before Orchestrator's master thread exits.
        """
        # Log conclusion of run_summary information if at singleton level.
        if self._singleton_run_summary_path:
            self.write_summary_information(write_run_summary=False, log_conclusion_msg=True)

    def _master_thread_loop(self):

        # Keep doing batches until given a stop request.
        while True:
            # Starting a new batch.
            request: BatchRequest = self._submission_que.get()
            with self._accounting_lock:
                self._on_batch_type = type(request)

            # Ensure the batch was not canceled (deleted during waiting state).
            if request and self._status_provider.is_deleted(request.batch_id):
                self._log_event_que.info(
                    "Orchestrator: Skipping batch {0} because it was marked deleted.".format(request.batch_id))
                continue

            # Recreate the endpoints on start of a new batch in case
            # the last batch disabled endpoints, e.g. for mismatched
            # language or other reasons.
            self.hotswap_endpoint_managers()

            with self._accounting_lock:
                if self._stop_requested:
                    self._master_finalize()
                    return

                # Starting a new batch.
                # Reset record keeping if it's not singleton run summary.
                if self._singleton_run_summary_path is None:
                    self._work_results = {}
                self._summarizer = request.get_batch_run_summarizer()

                self._log_event_que.info("Orchestrator: Starting batch {0}".format(request.batch_id))
                self._on_batch_id = request.batch_id
                self._batch_completion_evt.clear()
                self._run_summary_thread_gate.set()
                assert len(self._in_progress) == 0
                assert self._file_queue_size == 0

                for work in request.make_work_items(
                        self._status_provider.batch_base_path(request.batch_id),
                        self._cache_search_dirs,
                        self._log_folder):
                    self._file_queue.put(work)
                    self._file_queue_size += 1
                    self._work_results[work.filepath] = None
                self._file_queue_cond.notify_all()

            # Ensure this batch has not since been canceled.
            canceled = False
            with self._status_provider.lock:
                if self._status_provider.is_deleted(request.batch_id):
                    canceled = True
                else:
                    self._status_provider.change_status_enum(request.batch_id, BatchStatusEnum.running)
            if canceled:
                self.cancel_running_batch(request.batch_id)

            # Wait for batch completion or early stop request. In both cases,
            # nothing is in progress and nothing is in queue when we're woken.
            self._batch_completion_evt.wait()

            # Check if the batch was completed due to deletion (canceled).
            canceled = self._status_provider.is_deleted(request.batch_id)
            if canceled:
                self._log_event_que.info("Orchestrator: Canceled processing batch: {0}".format(request.batch_id))
            else:
                self._log_event_que.info("Orchestrator: Completed batch {0}".format(request.batch_id))

            # Report per-batch final run_summary.
            if self._singleton_run_summary_path is None:
                self.write_summary_information(
                    write_run_summary=True, write_retries=10, log_conclusion_msg=True, allow_fail=True)
            # Even with singleton run_summary, we should update run_summary file
            # now but not log conclusion.
            else:
                self.write_summary_information(
                    write_run_summary=True, write_retries=10, log_conclusion_msg=False, allow_fail=True)

            # Concatenate batch-level results to single file.
            if not canceled and request.combine_results:
                self._log_event_que.info(
                    "Orchestrator: Concatenating batch results to single file (combine_results option).")
                write_single_output_json(
                    request.files,
                    self._status_provider.batch_base_path(request.batch_id)
                )

            # Intentionally change status enum last so that above results committed first
            # for any event-driven observers.
            with self._status_provider.lock:
                if self._status_provider.is_deleted(request.batch_id):
                    # Ensure we delete files that may have been created after deletion was requested.
                    self._status_provider.delete_batch(request.batch_id)
                else:
                    self._status_provider.change_status_enum(request.batch_id, BatchStatusEnum.done)
                    self._log_event_que.info("Orchestrator: Updated batch status to Done: {0}".format(request.batch_id))

            # As another batch may not show up for a while (or never), stop the periodic
            # run summary thread since no new information to report.
            self._run_summary_thread_gate.clear()
