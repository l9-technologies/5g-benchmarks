"""Read the POSIX CPU clock for a live Linux process, including its threads."""
import ctypes
import errno
import os
from pathlib import Path
import time

class ProcessClock:
    def __init__(self):
        self.get_clock=ctypes.CDLL(None).clock_getcpuclockid
        self.get_clock.argtypes=[ctypes.c_int,ctypes.POINTER(ctypes.c_int)]
        self.get_clock.restype=ctypes.c_int
        self.ids={}

    def read_ns(self,pid):
        if pid not in self.ids:
            clock=ctypes.c_int()
            error=self.get_clock(pid,ctypes.byref(clock))
            if error:raise OSError(error,os.strerror(error))
            self.ids[pid]=clock.value
        try:return time.clock_gettime_ns(self.ids[pid])
        except OSError as error:
            if error.errno==errno.EINVAL and not Path('/proc/'+str(pid)).exists():
                raise ProcessLookupError(errno.ESRCH,'Process ended before its CPU sample') from error
            raise
