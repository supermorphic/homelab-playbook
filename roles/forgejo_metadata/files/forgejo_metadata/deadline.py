"""Leave time to verify writes while retaining the independent hard run limit."""
import time
from .model import MirrorError


class RunDeadline:
    def __init__(self, deadline, *, clock=None):
        self.clock = clock or time.monotonic
        self.deadline = deadline
        self.reserve = min(30, max(0, deadline - self.clock()) / 5)
        self.longest_write = 0

    def require_write(self):
        if self.deadline - self.clock() <= max(self.reserve, 2 * self.longest_write):
            raise MirrorError('run_budget_exhausted')

    def begin(self):
        self.require_write()
        return self.clock()

    def finish(self, started):
        self.longest_write = max(self.longest_write, self.clock() - started)
