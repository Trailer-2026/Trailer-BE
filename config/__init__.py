import os
import logging
from pathlib import Path
from configparser import ConfigParser

logger = logging.getLogger(__name__)

this_dir = Path(__file__).parent

conf_dir = this_dir / 'properties_dev.ini'

parser = ConfigParser(os.environ)
logger.info(f"Loading config from: {conf_dir}")
parser.read(conf_dir, encoding="utf8")


class Config():
    @staticmethod
    def read(section, property, default=None):
        return parser.get(section, property) or default
