from importlib.metadata import PackageNotFoundError, version

from .main import *


try:
    __version__ = version("geohexgrid")
except PackageNotFoundError:  # Package is not installed, e.g. running from source
    __version__ = "0.0.0"
