"""Keep writable portable state outside the frozen application's resources."""
from pathlib import Path
import sys

RESOURCE_DIR = Path(__file__).resolve().parent
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else RESOURCE_DIR
