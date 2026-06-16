import configparser
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INI_PATH = os.path.join(BASE_DIR, 'config', 'properties_dev.ini')

config = configparser.ConfigParser()
config.read(INI_PATH, encoding="utf8")

# [ALARM] 섹션이 없으면 기본값(local, None)을 가져옴
ENV = config.get('ALARM', 'ENV', fallback='local')
DISCORD_WEBHOOK_URL = config.get('ALARM', 'DISCORD_WEBHOOK_URL', fallback=None)

def is_production():
    return ENV == 'production'