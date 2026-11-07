"""Explicit per-host integration settings. Disabled features never open sidecars."""
from dataclasses import dataclass, field
from pathlib import Path
import os
import re
import stat
from urllib.parse import urlsplit

from .integration_storage import validate_private_path


def _flag(env, name):
    value = env.get(name, '').strip().lower()
    if value not in {'', 'false', 'true'}:
        raise ValueError('Настройка подключения должна быть true или false')
    return value == 'true'


def _secret(env, variable, *, needed):
    direct, filename = env.get(variable, '').strip(), env.get(variable + '_FILE', '')
    if direct and filename:
        raise ValueError('Для секрета выберите один защищённый источник')
    if not needed:
        return direct
    if filename:
        path = Path(filename)
        if not path.is_absolute() or '..' in path.parts:
            raise ValueError('Нужен абсолютный путь файла секрета')
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            if current.is_symlink():
                raise ValueError('Ссылки в пути секрета запрещены')
        info = path.stat()
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid not in {0, os.geteuid()} or info.st_nlink != 1 or info.st_size > 8192):
            raise ValueError('Нужен защищённый файл секрета 0600')
        direct = path.read_text().strip()
    if direct and (len(direct) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in direct)):
        raise ValueError('Неверный формат секрета подключения')
    return direct


@dataclass(frozen=True)
class IntegrationSettings:
    school_id: str
    request_ai_requested: bool = False
    ai_key: str = field(default='', repr=False)
    ai_model: str = ''
    ai_db: str = ''
    max_requested: bool = False
    max_send_requested: bool = False
    max_token: str = field(default='', repr=False)
    max_secret: str = field(default='', repr=False)
    max_username: str = ''
    max_webhook_url: str = ''
    max_integration_id: str = 'school-max'
    max_db: str = ''
    max_ca_file: str = ''
    staff_db: str = ''
    domain_db: str = ''
    agent_db: str = ''
    errors: tuple[str, ...] = ()
    max_schedule_notices_requested: bool = False
    max_update_mode: str = 'webhook'

    @classmethod
    def from_environment(cls, school_id, env=None):
        env = dict(os.environ if env is None else env)
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', school_id):
            raise ValueError('Нужна фиксированная школа подключения')
        errors = []
        try:
            ai = _flag(env, 'SHKOLA_REQUEST_AI_ENABLED')
            max_enabled = _flag(env, 'SHKOLA_MAX_ENABLED')
            sending = _flag(env, 'SHKOLA_MAX_SEND_ENABLED')
        except ValueError:
            ai = max_enabled = sending = False
            errors.append('Неверный переключатель подключения')
        try:
            schedule_notices = _flag(env, 'SHKOLA_MAX_SCHEDULE_NOTICES_ENABLED')
        except ValueError:
            schedule_notices = False
        update_mode = env.get('SHKOLA_MAX_UPDATE_MODE', 'webhook')
        if update_mode not in {'webhook', 'long_polling'}:
            errors.append('Неверный режим приёма MAX')
        try:
            ai_key = _secret(env, 'NANOGPT_API_KEY', needed=ai)
            token = _secret(env, 'SHKOLA_MAX_BOT_TOKEN', needed=max_enabled)
            secret = _secret(env, 'SHKOLA_MAX_WEBHOOK_SECRET', needed=max_enabled)
        except (ValueError, OSError, UnicodeError):
            ai_key = token = secret = ''
            errors.append('Не удалось безопасно прочитать секреты подключения')
        result = cls(school_id, ai, ai_key, env.get('NANOGPT_MODEL', '').strip(),
            env.get('SHKOLA_REQUEST_AI_DB_PATH', ''), max_enabled, sending,
            token, secret, env.get('SHKOLA_MAX_BOT_USERNAME', '').strip(),
            env.get('SHKOLA_MAX_WEBHOOK_URL', ''), env.get('SHKOLA_MAX_INTEGRATION_ID', 'school-max'),
            env.get('SHKOLA_MAX_DB_PATH', ''), env.get('SHKOLA_MAX_CA_FILE', ''),
            env.get('SHKOLA_STAFF_DB_PATH', ''), env.get('SHKOLA_DB_PATH', ''),
            env.get('AGENT_DB_PATH', ''), tuple(errors), max_schedule_notices_requested=schedule_notices,
            max_update_mode=update_mode)
        return result

    def _path_ready(self, path, other):
        if not path:
            return False
        try:
            validate_private_path(path, forbidden=[p for p in (other, self.domain_db, self.agent_db, self.staff_db) if p])
            return True
        except (ValueError, OSError):
            return False

    @property
    def ai_ready(self):
        return (self.request_ai_requested and not self.errors and bool(self.ai_key and self.ai_model)
                and self._path_ready(self.ai_db, self.max_db))

    @property
    def webhook_ready(self):
        try:
            from .max_transport import validate_webhook_url
            normalized = validate_webhook_url(self.max_webhook_url)
            parsed = urlsplit(self.max_webhook_url)
            return normalized == self.max_webhook_url and parsed.path == '/school-max/webhook'
        except ValueError:
            return False

    @property
    def max_config_ready(self):
        return (self.max_requested and not self.errors and bool(self.max_token)
            and bool(re.fullmatch(r'[A-Za-z0-9_-]{32,256}', self.max_secret))
            and bool(re.fullmatch(r'[A-Za-z0-9_]{3,80}', self.max_username))
            and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,80}', self.max_integration_id))
            and (self.max_update_mode == 'long_polling'
                 or self.max_update_mode == 'webhook' and self.webhook_ready)
            and self._path_ready(self.max_db, self.ai_db))

    def safe_status(self):
        ai_blockers = []
        if not self.request_ai_requested: ai_blockers.append('ИИ обращений выключен')
        if not self.ai_key: ai_blockers.append('Не задан ключ выбранного ИИ-провайдера')
        if not self.ai_model: ai_blockers.append('Не задана существующая выбранная модель')
        if not self._path_ready(self.ai_db, self.max_db): ai_blockers.append('Не задано отдельное хранилище результатов ИИ')
        max_blockers = []
        if not self.max_requested: max_blockers.append('Приём MAX выключен')
        if not self.max_token: max_blockers.append('Не задан токен бота MAX')
        if not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', self.max_secret): max_blockers.append('Не задан секрет webhook MAX')
        if not re.fullmatch(r'[A-Za-z0-9_]{3,80}', self.max_username): max_blockers.append('Не задано имя бота MAX')
        if self.max_update_mode not in {'webhook', 'long_polling'}: max_blockers.append('Неверный режим приёма MAX')
        if self.max_update_mode == 'webhook' and not self.webhook_ready: max_blockers.append('Не задан HTTPS-адрес приёма MAX')
        if not self._path_ready(self.max_db, self.ai_db): max_blockers.append('Не задано отдельное хранилище MAX')
        if not self.max_send_requested: max_blockers.append('Отправка MAX выключена отдельно')
        return {'schoolConfigured': True,
            'ai': {'enabled': bool(self.ai_ready), 'requested': self.request_ai_requested,
                'keyConfigured': bool(self.ai_key), 'modelConfigured': bool(self.ai_model),
                'providerTested': False, 'context': 'user_message_only', 'blockers': list(self.errors) + ai_blockers},
            'max': {'enabled': bool(self.max_config_ready), 'requested': self.max_requested,
                'updateMode': self.max_update_mode if self.max_update_mode in {'webhook', 'long_polling'} else 'invalid',
                'sendEnabled': bool(self.max_config_ready and self.max_send_requested),
                'tokenConfigured': bool(self.max_token), 'webhookImplemented': True,
                'networkSenderImplemented': True, 'webhookPublic': False,
                'subscriptionStatus': 'not_checked', 'providerTested': False,
                'blockers': list(self.errors) + max_blockers}}
