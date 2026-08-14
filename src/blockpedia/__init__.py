"""Blockpedia local Index Studio core.

The package intentionally contains no command-line or HTTP adapter.  Those
entry points are owned by the next phase and call the application services
exposed here.
"""

from .stages import R2_STAGES, STUDIO_STAGES
from .config import AppConfig
from .paths import DataRoot, resolve_data_root
from .services import StudioService

__all__ = ["AppConfig", "DataRoot", "R2_STAGES", "STUDIO_STAGES", "StudioService", "resolve_data_root"]
