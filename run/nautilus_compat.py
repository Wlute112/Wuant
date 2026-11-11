"""Compatibility repairs for the project's pinned Nautilus runtime."""
from bisect import insort_right

import nautilus_trader
import nautilus_trader.backtest.engine as engine_module


def repair_inflight_command_queue() -> None:
    """Keep 1.229's command queue sorted for its ``pop(0)`` consumer.

    SimulatedExchange inserts with a heap operation but drains with list
    pop(0), without restoring the heap. With mixed arrival times, a future
    command can hide a due command. The drain then clears the due timestamp's
    counter too early and later inserts reuse an existing key, causing Python
    to compare non-orderable TradingCommand objects.

    This module-level hook is used only by the inflight command queue; the
    data iterator uses heapq.heappush separately. Sorted insertion matches the
    existing drain and preserves (arrival time, sequence) ordering. It changes
    no installed files and must be revisited when the pinned version changes.
    """
    if nautilus_trader.__version__ == "1.229.0":
        engine_module.heappush = insort_right
