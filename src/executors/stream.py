import time
from queue import Queue
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from traceback import print_stack
from functools import partial
from collections import deque
import sys
import threading
import itertools

class CancelledError(Exception):
    pass


class StreamExecutor(ThreadPoolExecutor):
    def map(self, fn, *iterables, timeout=None, chunksize=1, buffer_size=10):
        """Returns an iterator equivalent to map(fn, iter).

        Args:
            fn: A callable that will take as many arguments as there are
                passed iterables.
            timeout: The maximum number of seconds to wait. If None, then there
                is no limit on the wait time.
            chunksize: The size of the chunks the iterable will be broken into
                before being passed to a child process. This argument is only
                used by ProcessPoolExecutor; it is ignored by
                ThreadPoolExecutor.
            buffer_size: The maximum number of input items that may be
                stored at once; default is a small buffer; 0 for no limit. The
                drawback of using a large buffer is the possibility of wasted
                computation and memory (in case not all input is needed), as
                well as higher peak memory usage.
        Returns:
            An iterator equivalent to: map(func, *iterables) but the calls may
            be evaluated out-of-order.

        Raises:
            TimeoutError: If the entire result iterator could not be generated
                before the given timeout.
            Exception: If fn(*args) raises for any values.
        """
        if timeout is None:
            end_time = None
        else:
            end_time = timeout + time.time()

        if buffer_size is None:
            buffer_size = -1

        iterators = [iter(iterable) for iterable in iterables]

        # Set to True to gracefully terminate all producers
        cancel = False

        # Deadlocks on the two queues are avoided using the following rule.
        # The writer guarantees to place a sentinel value into the buffer
        # before exiting, and to write nothing after that; the reader
        # guarantees to read the queue until it encounters a sentinel value
        # and to stop reading after that. Any value of type BaseException is
        # treated as a sentinel.
        input_buffer = Queue(maxsize=buffer_size)
        futures_buffer = Queue(10)

        # This function will run in a separate thread.
        def consume_inputs():
            while True:
                if cancel:
                    input_buffer.put(CancelledError())
                    return
                try:
                    args = [next(iterator) for iterator in iterators]
                except BaseException as e:
                    # StopIteration represents exhausted input; any other
                    # exception is due to an error in the input generator. We
                    # forward the exception downstream so it can be raised
                    # when client iterates through the result of map.
                    input_buffer.put(e)
                    return
                else:
                    input_buffer.put(args)

        # This function will run in a separate thread.
        def submit_inputs():
            nonlocal cancel
            while True:
                if cancel:
                    futures_buffer.put(CancelledError())
                    break
                args = input_buffer.get()
                if isinstance(args, BaseException):
                    # Forward upstream exceptions downstream.
                    futures_buffer.put(args)
                    return
                try:
                    future = self.submit(fn, *args)
                    futures_buffer.put(future)
                except BaseException as e:
                    # Cancel upstream and forward the new exception downstream.
                    cancel = True
                    futures_buffer.put(e)
                    break
            while not isinstance(input_buffer.get(), BaseException):
                pass

        # This function will run in the main thread.
        def produce_results():
            def cleanup():
                nonlocal cancel
                cancel = True
                while True:
                    future = futures_buffer.get()
                    if isinstance(future, BaseException):
                        break
                    else:
                        future.cancel()
                raise exc

            # Ensure cleanup happens even if client never starts this generator.
            try:
                yield None
            except GeneratorExit as exc:
                cleanup()
            while True:
                future = futures_buffer.get()
                if isinstance(future, BaseException):
                    # Reraise upstream exceptions at the map call site.
                    raise future
                if end_time is None:
                    remaining_timeout = None
                else:
                    remaining_timeout = end_time - time.time()
                # Reraise new exceptions (errors in the callable fn, TimeOut,
                # GeneratorExit) at map call site, but also cancel upstream.
                try:
                    yield future.result(remaining_timeout)
                except BaseException as exc:
                    cleanup()


        # Reusing existing thread pool will cause livelock if <3 threads idle.
        admin_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix=self._thread_name_prefix + ': map admin')
        input_generator = admin_executor.submit(consume_inputs)
        futures_generator = admin_executor.submit(submit_inputs)
        # After map returns, admin_executor will be collected and shut down;
        # we don't care when since we never need to submit more tasks to it.
        result = produce_results()
        # Consume the dummy `None` result
        next(result)
        return result