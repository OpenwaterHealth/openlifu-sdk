import struct
import json
from dataclasses import dataclass
from typing import Optional, Any, Dict
import logging

from ow_comms import UserConfig

logger = logging.getLogger(__name__)

# Constants from C code

class LIFUUserConfig(UserConfig):
        
    MAGIC = 0x4C494655      # 'LIFU'
    VERSION = 0x00010002    # v1.0.0