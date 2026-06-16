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
        # 섹션/키가 없어도(예: CI에서 properties_dev.ini 미존재) 예외 없이 default 반환
        return parser.get(section, property, fallback=default) or default
