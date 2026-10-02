"""Optional GPU acceleration for research sweeps.

Nothing in the request path uses this: pages and APIs are served from cached
results on the CPU. It exists for the long jobs that try thousands of parameter
combinations, where one batched pass on a GPU replaces a Python loop.

Everything degrades to the CPU when CUDA is not available, so importing or
running it never requires a GPU.
"""

from atr.compute.device import device_info, get_device

__all__ = ["device_info", "get_device"]
